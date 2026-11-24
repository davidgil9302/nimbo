"""Exercise the OAuth callback, token exchange and cache against the Drive fake."""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from io import BytesIO
from queue import Queue
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs

import httpx
import pytest

from nimbo.core import auth
from nimbo.core.auth import (
    AUTH_URL,
    REVOKE_URL,
    SCOPE,
    TOKEN_URL,
    OAuthClient,
    RefreshingTokenProvider,
    StaticTokenProvider,
    TokenSet,
    authorize_interactive,
    pkce_pair,
    revoke,
)
from nimbo.core.drive import TokenProvider
from nimbo.core.errors import AuthError
from nimbo.core.secrets import REFRESH_TOKEN_KEY, MemorySecretStore
from tests.fakedrive import FakeDrive

CLIENT = OAuthClient("test-client", "sentinel-client-secret")
ACCESS = "sentinel-access-token"
REFRESH = "sentinel-refresh-token"
ABOUT = "https://www.googleapis.com/drive/v3/about?fields=user"


@pytest.fixture(params=["streams", "ipv4"])
def browser_http(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[httpx.Client]:
    """Exercise actual HTTP parsing, over IPv4 when the sandbox permits it."""
    if request.param == "ipv4":
        try:
            with socket.socket(socket.AF_INET) as probe:
                probe.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"IPv4 loopback unavailable: errno {exc.errno}")
        with httpx.Client(trust_env=False, timeout=2) as browser:
            yield browser
        return

    servers: list[auth._CallbackServer] = []
    stopped = threading.Event()
    daemon_servers: list[bool] = []

    class StreamServer(auth._CallbackServer):
        address_family = socket.AF_UNIX

        def server_bind(self) -> None:
            assert self.server_address == ("127.0.0.1", 0)
            assert threading.current_thread().daemon
            self.server_port = 8765
            servers.append(self)

        def server_activate(self) -> None:
            pass

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            daemon_servers.append(threading.current_thread().daemon)
            stopped.wait()

        def shutdown(self) -> None:
            stopped.set()

    def exchange(request: httpx.Request) -> httpx.Response:
        raw = f"{request.method} ".encode() + request.url.raw_path + b" HTTP/1.1\r\n"
        for name, value in request.headers.raw:
            raw += name + b": " + value + b"\r\n"
        incoming, outgoing = BytesIO(raw + b"\r\n"), BytesIO()

        class StreamHandler(auth._CallbackHandler):
            def setup(self) -> None:
                self.rfile, self.wfile = incoming, outgoing

            def finish(self) -> None:
                pass

        StreamHandler(cast(socket.socket, None), ("127.0.0.1", 0), servers[-1])
        stream = SimpleNamespace(makefile=lambda _mode: BytesIO(outgoing.getvalue()))
        with HTTPResponse(cast(socket.socket, stream)) as response:
            response.begin()
            return httpx.Response(
                response.status, headers=response.getheaders(), content=response.read()
            )

    monkeypatch.setattr(auth, "_CallbackServer", StreamServer)
    with httpx.Client(transport=httpx.MockTransport(exchange)) as browser:
        yield browser
    assert stopped.is_set()
    assert daemon_servers == [True]
    assert all(server.socket.fileno() == -1 for server in servers)


def _register(fake: FakeDrive) -> None:
    fake.oauth.register_client(CLIENT.client_id, CLIENT.client_secret)


def _start(browser: httpx.Client, url: str) -> str:
    """Open the loopback start URL like a browser and return the consent URL it redirects to."""
    start = httpx.URL(url)
    assert (start.scheme, start.host) == ("http", "127.0.0.1") and start.port
    assert start.path.startswith("/start/") and not start.query
    response = browser.get(url)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    return response.headers["location"]


def _authorize(fake: FakeDrive, url: str) -> tuple[str, str, str]:
    params = httpx.URL(url).params
    code = fake.oauth.authorize(
        params["client_id"],
        params["redirect_uri"],
        params["code_challenge"],
        params["state"],
        params["scope"],
    )
    return params["redirect_uri"], params["state"], code


class _RecordingStore(MemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.labels: dict[str, str] = {}

    def set(self, key: str, value: str, *, label: str) -> None:
        self.labels[key] = label
        super().set(key, value, label=label)


def test_pkce_pair_s256() -> None:
    """Use a fresh RFC 7636 verifier and an unpadded SHA-256 URL-safe challenge."""
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert all(char.isascii() and (char.isalnum() or char in "-._~") for char in verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.decode().rstrip("=")
    assert pkce_pair()[0] != verifier


def test_authorize_interactive_happy_path(browser_http: httpx.Client) -> None:
    """Exchange the fake's code using PKCE and store only the refresh token."""
    fake = FakeDrive()
    _register(fake)
    store = _RecordingStore()
    published: list[str] = []
    pages: Queue[httpx.Response] = Queue()

    consent: list[str] = []

    def open_browser(url: str) -> bool:
        consent.append(_start(browser_http, url))
        redirect, state, code = _authorize(fake, consent[-1])
        pages.put(browser_http.get(redirect, params={"code": code, "state": state}))
        return True

    with fake.client() as http:
        tokens = authorize_interactive(
            CLIENT,
            http,
            store=store,
            open_browser=open_browser,
            on_url=published.append,
            token_url=fake.TOKEN_URL,
            auth_url=fake.AUTH_URL,
            timeout_s=2,
        )
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        assert http.get(ABOUT, headers=headers).is_success

    page = pages.get(timeout=2)
    assert page.status_code == 200
    assert "close this tab" in page.text and "cerrar esta pestaña" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    # Only the single-use loopback start URL is published; the consent URL never is.
    (start,) = published
    assert httpx.URL(start).host == "127.0.0.1" and httpx.URL(start).path.startswith("/start/")
    (url,) = consent
    params = httpx.URL(url).params
    assert str(httpx.URL(url).copy_with(query=None)) == AUTH_URL
    assert dict(params) == {
        "client_id": CLIENT.client_id,
        "redirect_uri": params["redirect_uri"],
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": params["code_challenge"],
        "code_challenge_method": "S256",
        "state": params["state"],
    }
    assert len(base64.urlsafe_b64decode(params["state"] + "=" * (-len(params["state"]) % 4))) >= 32
    redirect = httpx.URL(params["redirect_uri"])
    assert (redirect.scheme, redirect.host, redirect.path) == ("http", "127.0.0.1", "/")
    assert redirect.port
    assert tokens.scope == SCOPE
    assert tokens.refresh_token and store.get(REFRESH_TOKEN_KEY) == tokens.refresh_token
    assert store.labels == {REFRESH_TOKEN_KEY: "nimbo: Google Drive refresh token"}


def test_state_mismatch_ignored_then_correct_callback_accepted(
    browser_http: httpx.Client, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ignore other paths, methods and malformed states, then accept a pasted URL."""
    fake = FakeDrive()
    _register(fake)
    published: Queue[str] = Queue()
    browser_called = threading.Event()
    marker = "sentinel-untrusted-callback"

    def open_browser(_url: str) -> bool:
        browser_called.set()
        return False

    with fake.client() as http, ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            authorize_interactive,
            CLIENT,
            http,
            on_url=published.put,
            open_browser=open_browser,
            timeout_s=5,
        )
        start = published.get(timeout=2)
        assert browser_called.wait(timeout=2)
        redirect, state, code = _authorize(fake, _start(browser_http, start))
        good = {"code": code, "state": state}
        assert browser_http.get(redirect + "favicon.ico", params=good).status_code == 404
        assert browser_http.get(redirect + "/", params=good).status_code == 404
        assert browser_http.post(redirect, params=good).status_code == 501
        wrong = browser_http.get(redirect, params={"code": marker, "state": "incorrecto-ñ"})
        assert wrong.status_code == 400
        assert marker not in wrong.text
        assert "close this tab" in wrong.text and "cerrar esta pestaña" in wrong.text
        ambiguous = [("code", code), ("state", state), ("state", marker)]
        assert browser_http.get(redirect, params=ambiguous).status_code == 400
        assert browser_http.get(redirect, params={"state": state}).status_code == 400
        assert browser_http.get(redirect, params=good).status_code == 200
        assert result.result(timeout=2).refresh_token
    stderr = capsys.readouterr().err
    assert marker not in stderr and code not in stderr and state not in stderr


def test_access_denied(browser_http: httpx.Client) -> None:
    """Report a matching denial and do not exchange a token."""
    pages: Queue[httpx.Response] = Queue()

    def denied(url: str) -> bool:
        params = httpx.URL(_start(browser_http, url)).params
        pages.put(
            browser_http.get(
                params["redirect_uri"],
                params={
                    "state": params["state"],
                    "error": "access_denied",
                    "error_description": ACCESS,
                },
            )
        )
        return True

    with (
        httpx.Client(transport=httpx.MockTransport(_unexpected_request)) as http,
        pytest.raises(AuthError, match=r"^auth_denied$"),
    ):
        authorize_interactive(CLIENT, http, open_browser=denied, timeout_s=2)
    assert ACCESS not in pages.get(timeout=2).text


def _unexpected_request(_request: httpx.Request) -> httpx.Response:
    pytest.fail("unexpected token endpoint request")


def test_timeout(browser_http: httpx.Client) -> None:
    """Time out and close the listener even when the browser launcher blocks."""
    release = threading.Event()
    published: list[str] = []

    def blocked(_url: str) -> bool:
        release.wait()
        return False

    with httpx.Client(transport=httpx.MockTransport(_unexpected_request)) as http:
        try:
            with pytest.raises(AuthError, match=r"^auth_timeout$"):
                authorize_interactive(
                    CLIENT, http, open_browser=blocked, on_url=published.append, timeout_s=0.05
                )
        finally:
            release.set()
    assert len(published) == 1


def test_loopback_bind_failure_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map a listener startup error to a safe auth error before opening the URL."""

    def cannot_bind(*_args: object) -> None:
        raise PermissionError(ACCESS)

    monkeypatch.setattr(auth, "_CallbackServer", cannot_bind)
    urls: list[str] = []
    with (
        httpx.Client(transport=httpx.MockTransport(_unexpected_request)) as http,
        pytest.raises(AuthError, match=r"^auth_network$") as caught,
    ):
        authorize_interactive(CLIENT, http, on_url=urls.append)
    assert not urls
    assert caught.value.__context__ is None


@pytest.mark.parametrize("missing", ["refresh_token", "scope", "different_scope"])
def test_missing_refresh_token_or_scope(browser_http: httpx.Client, missing: str) -> None:
    """Preserve the stored credential when initial authorization is incomplete."""
    store = MemorySecretStore()
    store.set(REFRESH_TOKEN_KEY, REFRESH, label="Test")
    payload = {"access_token": ACCESS, "refresh_token": "new", "expires_in": 3600, "scope": SCOPE}
    if missing == "different_scope":
        payload["scope"] = SCOPE + ".file"
    else:
        del payload[missing]

    def browser(url: str) -> bool:
        params = httpx.URL(_start(browser_http, url)).params
        browser_http.get(params["redirect_uri"], params={"state": params["state"], "code": "test"})
        return True

    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with (
        httpx.Client(transport=transport) as http,
        pytest.raises(AuthError, match=r"^auth_scope_missing$"),
    ):
        authorize_interactive(CLIENT, http, open_browser=browser, store=store, timeout_s=2)
    assert store.get(REFRESH_TOKEN_KEY) == REFRESH


@pytest.fixture
def refresh_session() -> Iterator[tuple[FakeDrive, RefreshingTokenProvider, list[float]]]:
    """Grant a refresh token using the fake without requiring a browser callback."""
    now = [1_000.0]
    fake = FakeDrive(clock=lambda: now[0])
    _register(fake)
    store = MemorySecretStore()
    redirect = "http://127.0.0.1:3210/"
    verifier, challenge = pkce_pair()
    code = fake.oauth.authorize(CLIENT.client_id, redirect, challenge, "state", SCOPE)
    with fake.client() as http:
        grant = http.post(
            fake.TOKEN_URL,
            data={
                "client_id": CLIENT.client_id,
                "client_secret": CLIENT.client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect,
                "code": code,
                "code_verifier": verifier,
            },
        )
        grant.raise_for_status()
        store.set(REFRESH_TOKEN_KEY, grant.json()["refresh_token"], label="Test")
        fake.calls.clear()
        yield fake, RefreshingTokenProvider(CLIENT, store, http, clock=fake.now), now


def test_provider_caches_until_expiry(
    refresh_session: tuple[FakeDrive, RefreshingTokenProvider, list[float]],
) -> None:
    """Refresh at the exact sixty-second margin using the fake clock."""
    fake, provider, now = refresh_session
    first = provider.token()
    now[0] += 3_539
    assert provider.token() == first
    assert len(fake.calls) == 1
    now[0] += 1
    assert provider.token() != first
    assert len(fake.calls) == 2


def test_provider_refreshes_after_invalidate(
    refresh_session: tuple[FakeDrive, RefreshingTokenProvider, list[float]],
) -> None:
    """Invalidate a cached token using the Drive client's protocol."""
    fake, refreshing, _ = refresh_session
    provider: TokenProvider = refreshing
    first = provider.token()
    provider.invalidate()
    assert provider.token() != first
    assert len(fake.calls) == 2


def test_invalid_grant_needs_reconnect(
    refresh_session: tuple[FakeDrive, RefreshingTokenProvider, list[float]],
) -> None:
    """Tell the caller to reconnect after the refresh grant has been revoked."""
    fake, provider, _ = refresh_session
    fake.oauth.fail_refresh_with = "invalid_grant"
    with pytest.raises(AuthError, match=r"^reauth_required$") as caught:
        provider.token()
    assert caught.value.needs_reconnect


def test_missing_stored_token_needs_reconnect() -> None:
    """Report the missing credential without contacting the token endpoint."""
    with httpx.Client(transport=httpx.MockTransport(_unexpected_request)) as http:
        provider = RefreshingTokenProvider(CLIENT, MemorySecretStore(), http)
        with pytest.raises(AuthError, match=r"^reauth_required$") as caught:
            provider.token()
    assert caught.value.needs_reconnect


@pytest.mark.parametrize("transport_error", [False, True])
def test_tokens_never_in_error_messages(
    caplog: pytest.LogCaptureFixture, transport_error: bool
) -> None:
    """Redact values in object representations, transport errors and server errors."""
    secrets = (ACCESS, REFRESH, CLIENT.client_secret)
    marker = " ".join(secrets)
    caplog.set_level("DEBUG")

    def fail(request: httpx.Request) -> httpx.Response:
        if transport_error:
            raise httpx.ConnectError(marker, request=request)
        return httpx.Response(400, json={"error": marker, "error_description": marker})

    store = MemorySecretStore()
    store.set(REFRESH_TOKEN_KEY, REFRESH, label="Test")
    static: TokenProvider = StaticTokenProvider(ACCESS)
    static.invalidate()
    assert static.token() == ACCESS
    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        provider = RefreshingTokenProvider(CLIENT, store, http)
        objects = [CLIENT, TokenSet(ACCESS, REFRESH, 5000, SCOPE), static, provider, store]
        for call in (provider.token, lambda: revoke(REFRESH, http)):
            with pytest.raises(AuthError) as caught:
                call()
            assert caught.value.code == ("auth_network" if transport_error else "auth_failed")
            assert caught.value.__context__ is None
            error_text = "".join(traceback.format_exception(caught.value))
            assert not any(secret in error_text for secret in secrets)
            objects.append(caught.value)
    representations = [text for obj in objects for text in (str(obj), repr(obj))]
    assert not any(secret in text for secret in secrets for text in representations)
    assert not any(secret in caplog.text for secret in secrets)


def test_provider_thread_safe_single_refresh(
    refresh_session: tuple[FakeDrive, RefreshingTokenProvider, list[float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coalesce ten simultaneous callers into a single refresh request."""
    fake, provider, _ = refresh_session
    start = threading.Barrier(10)
    exchange = fake.oauth._token

    def slow_exchange(request: httpx.Request) -> httpx.Response:
        time.sleep(0.025)
        return exchange(request)

    monkeypatch.setattr(fake.oauth, "_token", slow_exchange)

    def get(_index: int) -> str:
        start.wait(timeout=2)
        return provider.token()

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert len(set(pool.map(get, range(10), timeout=3))) == 1
    assert len(fake.calls) == 1
    assert (fake.calls[0].method, fake.calls[0].path) == ("POST", httpx.URL(TOKEN_URL).path)


def test_refresh_rotation_and_implicit_scope() -> None:
    """Save a rotated credential and inherit the grant scope on omitted scope."""
    store = _RecordingStore()
    store.set(REFRESH_TOKEN_KEY, REFRESH, label="Test")
    rotated = "rotated-test-secret"

    def refresh(request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode())["refresh_token"] == [REFRESH]
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": rotated,
                "expires_in": 3600,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(refresh)) as http:
        assert RefreshingTokenProvider(CLIENT, store, http).token() == ACCESS
    assert store.get(REFRESH_TOKEN_KEY) == rotated
    assert store.labels == {REFRESH_TOKEN_KEY: "nimbo: Google Drive refresh token"}


def test_refresh_rejects_narrowed_scope() -> None:
    """Reject an explicit reduced scope before storing any replacement credential."""
    store = MemorySecretStore()
    store.set(REFRESH_TOKEN_KEY, REFRESH, label="Test")

    def refresh(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": "rotated-test-secret",
                "scope": SCOPE + ".readonly",
                "expires_in": 3600,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(refresh)) as http:
        provider = RefreshingTokenProvider(CLIENT, store, http)
        with pytest.raises(AuthError, match=r"^auth_scope_missing$"):
            provider.token()
    assert store.get(REFRESH_TOKEN_KEY) == REFRESH


def test_revoke() -> None:
    """Revoke via form POST and tolerate a 400 invalid_token reply."""
    forms: list[dict[str, list[str]]] = []

    def server(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == REVOKE_URL
        forms.append(parse_qs(request.content.decode()))
        if len(forms) > 1:
            return httpx.Response(400, json={"error": "invalid_token"})
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        revoke(REFRESH, http)
        revoke(REFRESH, http)
    assert forms == [{"token": [REFRESH]}, {"token": [REFRESH]}]


def test_tokens_not_forwarded_on_redirect() -> None:
    """Disable HTTP redirects even when enabled on the caller's HTTP client."""
    visited: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://redirect.example.test/"})

    with (
        httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as http,
        pytest.raises(AuthError, match=r"^auth_failed$"),
    ):
        revoke(REFRESH, http)
    assert visited == [REVOKE_URL]
