"""Dependency wiring and isolation of local integration hooks."""

from contextlib import ExitStack

import httpx
import pytest

from nimbo.app import context
from nimbo.core.config import Config, OAuthConfig, SourceConfig, TargetConfig
from nimbo.core.model import RunResult
from nimbo.core.paths import Paths
from nimbo.core.safety import write_sentinel
from tests.fakedrive import FakeDrive


@pytest.mark.parametrize(
    ("base", "accepted"),
    [
        ("http://127.0.0.1:8765", True),
        ("http://localhost:8765", True),
        ("https://localhost:8765", False),
        ("http://localhost", False),
        ("http://localhost:65536", False),
        ("http://localhost:8765@elsewhere.example", False),
        ("http://127.0.0.1:8765/path", False),
        ("http://127.0.0.2:8765", False),
        ("http://[::1]:8765", False),
        ("http://elsewhere.example:8765", False),
        ("http://localhost:8765\n", False),
    ],
)
def test_hooks_only_with_loopback(base, accepted, caplog):
    env = {
        "NIMBO_API_BASE": base,
        "NIMBO_TEST_TOKEN": "unit-value",
        "NIMBO_OAUTH_BASE": "http://127.0.0.1:8765",
    }
    hooks = context.test_hooks(env)
    assert (hooks.api_base, hooks.token, hooks.oauth_base) == (
        (base, "unit-value", env["NIMBO_OAUTH_BASE"]) if accepted else (None, None, None)
    )
    assert bool(caplog.records) is not accepted
    assert "unit-value" not in caplog.text


def test_oauth_hook_requires_a_loopback_origin_even_with_api_hook():
    assert (
        context.test_hooks(
            {
                "NIMBO_API_BASE": "http://localhost:8765",
                "NIMBO_OAUTH_BASE": "https://elsewhere.example",
            }
        ).oauth_base
        is None
    )


@pytest.mark.parametrize("http_mode", [False, True], ids=["transport", "http"])
def test_build_engine_wires_everything(tmp_path, monkeypatch, http_mode):
    fake = FakeDrive()
    fake.oauth.add_access_token("unit-value")
    target = fake.add_folder("Mirror")
    source = tmp_path / "source"
    (source / "Projects").mkdir(parents=True)
    (source / "Projects" / "note.txt").write_text("hello", encoding="utf-8")
    write_sentinel(source, "unit-root")
    cfg = Config(
        SourceConfig(source, ("Projects",), require_mount=False),
        TargetConfig(target),
        OAuthConfig("unit.apps.googleusercontent.com", "unit-secret"),
    )
    paths = Paths(tmp_path / "config", tmp_path / "state")
    if http_mode:
        with ExitStack() as stack:
            try:
                base = stack.enter_context(fake.serve())
            except PermissionError as exc:
                pytest.skip(f"loopback HTTP is unavailable: {exc}")
            _run_wired(paths, cfg, base)
    else:
        client_factory = context.new_http_client

        def client(*, loopback=False):
            original = client_factory(loopback=loopback)
            with original:
                assert original.timeout == httpx.Timeout(connect=30, read=300, write=300, pool=60)
                assert original.headers["User-Agent"].startswith("nimbo/")
                return httpx.Client(
                    transport=httpx.MockTransport(
                        lambda req: fake._handle(req, "http://127.0.0.1:8765")
                    ),
                    timeout=original.timeout,
                )

        monkeypatch.setattr(context, "new_http_client", client)
        _run_wired(paths, cfg, "http://127.0.0.1:8765")
    assert fake.find(fake.find(target, "Projects")[0].id, "note.txt")[0].content == b"hello"


def _run_wired(paths, cfg, base):
    class Notifier:
        def run_finished(self, summary, *, manual):
            self.summary = summary
            assert manual

    notifier = Notifier()
    engine, close = context.build_engine(
        paths,
        cfg,
        env={"NIMBO_API_BASE": base, "NIMBO_TEST_TOKEN": "unit-value"},
        notifier=notifier,
    )
    try:
        assert engine.preview().plan.stats.uploads == 1
        summary = engine.run(trigger="manual")
        assert summary.result is RunResult.OK
        assert notifier.summary is summary
    finally:
        close()
