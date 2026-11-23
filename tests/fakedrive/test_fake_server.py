"""Use the same Drive and OAuth routes over a real loopback HTTP connection."""

from __future__ import annotations

import json
import socket
from http.client import HTTPResponse
from io import BytesIO
from socketserver import BaseServer
from types import SimpleNamespace
from typing import cast

import httpx

from . import FakeDrive
from .server import _RequestHandler
from .test_fake_contract import ABOUT, AUTH, FILES
from .test_fake_oauth import CLIENT, SCOPE, VERIFIER, challenge
from .test_fake_uploads import UPLOAD, put, start


def test_http_server_mode_roundtrip() -> None:
    fake = FakeDrive()
    fake.oauth.add_access_token("test-token")
    with fake.serve() as base, httpx.Client(base_url=base, headers=AUTH, trust_env=False) as client:
        assert base.startswith("http://127.0.0.1:")
        url = start(client, 3)
        assert url.startswith(base + "/")
        assert put(client, url, "bytes */3").status_code == 308
        finished = put(client, url, "bytes 0-2/3", b"abc")
        assert finished.status_code == 200
        file_id = finished.json()["id"]
        assert client.get(f"{FILES}/{file_id}", params={"alt": "media"}).content == b"abc"
        assert fake.file(file_id).content == b"abc"
        fake.oauth.register_client(CLIENT["client_id"], CLIENT["client_secret"])
        redirect = base + "/callback"
        auth = client.get(
            "/o/oauth2/v2/auth",
            params={
                "client_id": CLIENT["client_id"],
                "code_challenge": challenge(VERIFIER),
                "code_challenge_method": "S256",
                "response_type": "code",
                "redirect_uri": redirect,
                "state": "test-state",
                "scope": SCOPE,
            },
        )
        assert auth.status_code == 302
        assert auth.headers["Location"].startswith(redirect + "?")
        query = httpx.URL(auth.headers["Location"]).params
        assert query["state"] == "test-state"
        tokens = client.post(
            "/token",
            data={
                **CLIENT,
                "grant_type": "authorization_code",
                "redirect_uri": redirect,
                "code": query["code"],
                "code_verifier": VERIFIER,
            },
        ).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.get(ABOUT, headers=headers).status_code == 200
        assert client.post("/revoke", data={"token": tokens["access_token"]}).status_code == 200
        assert client.get(ABOUT, headers=headers).status_code == 401


def test_http_adapter_handles_chunked_requests_and_resumable_location() -> None:
    fake = FakeDrive()
    fake.oauth.add_access_token("test-token")
    base = "http://127.0.0.1:8765"
    server = cast(BaseServer, SimpleNamespace(base=base, route=fake._handle))

    def exchange(raw: bytes) -> tuple[int, httpx.Headers, bytes]:
        with BytesIO(raw) as incoming, BytesIO() as outgoing:

            class StreamHandler(_RequestHandler):
                def setup(self) -> None:
                    self.rfile, self.wfile = incoming, outgoing

                def finish(self) -> None:
                    pass

            StreamHandler(cast(socket.socket, None), ("127.0.0.1", 0), server)
            stream = SimpleNamespace(makefile=lambda _mode: BytesIO(outgoing.getvalue()))
            with HTTPResponse(cast(socket.socket, stream)) as response:
                response.begin()
                return response.status, httpx.Headers(response.getheaders()), response.read()

    status, headers, body = exchange(
        f"POST {UPLOAD}?uploadType=resumable&fields=size HTTP/1.1\r\n".encode()
        + b"Host: 127.0.0.1\r\nAuthorization: Bearer test-token\r\n"
        + b"Content-Type: application/json\r\nX-Upload-Content-Length: 3\r\n"
        + b"Transfer-Encoding: chunked\r\n\r\n1\r\n{\r\n1\r\n}\r\n0\r\n\r\n"
    )
    assert status == 200 and body == b""
    assert headers["Location"].startswith(base + UPLOAD)
    url = httpx.URL(headers["Location"])
    status, _, body = exchange(
        b"PUT " + url.raw_path + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\nAuthorization: Bearer test-token\r\n"
        b"Content-Range: bytes 0-2/3\r\nContent-Length: 3\r\n\r\nabc"
    )
    assert status == 200
    assert json.loads(body) == {"size": "3"}
