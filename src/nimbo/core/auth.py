"""Google Drive desktop OAuth with a local PKCE callback and in-memory access tokens."""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
import socket
import time
import webbrowser
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Event, Lock, Thread
from typing import cast
from urllib.parse import parse_qs, urlencode

import httpx

from .errors import AuthError
from .secrets import REFRESH_TOKEN_KEY, SecretStore

SCOPE = "https://www.googleapis.com/auth/drive"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_KEYRING_LABEL = "nimbo: Google Drive refresh token"
_REFRESH_MARGIN = 60.0

_SUCCESS_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>nimbo</title><p>Authorization received. You can close this tab.</p>
<p lang="es">Autorización recibida. Puedes cerrar esta pestaña.</p></html>""".encode()
_FAILURE_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>nimbo</title><p>Authorization was not accepted. You can close this tab.</p>
<p lang="es">No se aceptó la autorización. Puedes cerrar esta pestaña.</p></html>""".encode()


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """Google OAuth credentials for an installed desktop application."""

    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TokenSet:
    """An OAuth grant with expiration in epoch seconds and a space-separated scope."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float
    scope: str = field(repr=False)


def pkce_pair() -> tuple[str, str]:
    """Return a fresh URL-safe PKCE verifier and its unpadded S256 challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class _CallbackServer(ThreadingHTTPServer):
    # An idle browser preconnection may outlive the callback on another connection.
    daemon_threads = True

    def __init__(self, state: str, deadline: float) -> None:
        self.state = state.encode("ascii")
        self.deadline = deadline
        self.ready = Event()
        self.code: str | None = None
        self._claimed = False
        self._lock = Lock()
        self._connections: set[socket.socket] = set()
        super().__init__(("127.0.0.1", 0), _CallbackHandler)

    def claim(self, code: str | None) -> bool:
        """Record a code or denial on the first valid callback before the deadline."""
        with self._lock:
            if self._claimed or time.monotonic() >= self.deadline:
                return False
            self._claimed = True
            self.code = code
            return True

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(2.0)
        with self._lock:
            self._connections.add(connection)
        return connection, address

    def close_request(self, request: socket.socket | tuple[bytes, socket.socket]) -> None:
        connection = cast(socket.socket, request)  # This server handles TCP sockets.
        with self._lock:
            self._connections.discard(connection)
        super().close_request(request)

    def server_close(self) -> None:
        super().server_close()
        with self._lock:
            for connection in self._connections:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        """Suppress request-handler tracebacks, which can carry callback data."""


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        """Handle only a root callback with one matching state and one result."""
        # BaseHTTPRequestHandler normalizes // to / before setting self.path.
        path, _, query = self.requestline.split()[1].partition("?")
        if path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=32, errors="strict")
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        server = cast(_CallbackServer, self.server)
        states = params.get("state", [])
        codes = params.get("code", [])
        errors = params.get("error", [])
        if len(states) != 1 or not compare_digest(states[0].encode("utf-8"), server.state):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if len(codes) == 1 and codes[0] and not errors:
            code = codes[0]
        elif len(errors) == 1 and errors[0] and not codes:
            code = None
        else:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        if not server.claim(code):
            self.send_error(HTTPStatus.GONE)
            return
        try:
            if code is None:
                self.send_error(HTTPStatus.BAD_REQUEST)
            else:
                self._reply(HTTPStatus.OK, _SUCCESS_PAGE)
        finally:
            server.ready.set()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Send a fixed error page with no request data in its headers or body."""
        self._reply(code, _FAILURE_PAGE)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress HTTP logs because the request target can contain credentials."""

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _launch_browser(open_browser: Callable[[str], bool], url: str) -> None:
    # The URL is also available for manual sign-in if a browser launcher fails.
    with suppress(Exception):
        open_browser(url)


@contextmanager
def _callback_server(state: str, deadline: float) -> Iterator[_CallbackServer]:
    started: Future[_CallbackServer] = Future()

    def listen() -> None:
        try:
            server = _CallbackServer(state, deadline)
        except OSError:
            started.set_exception(AuthError(code="auth_network"))
            return
        except BaseException as error:  # noqa: BLE001 - hand startup failures to the waiter
            started.set_exception(error)
            return
        with server:
            started.set_result(server)
            server.serve_forever(poll_interval=0.05)

    listener = Thread(target=listen, daemon=True)
    listener.start()
    try:
        yield started.result()
    finally:
        if started.exception() is None:
            started.result().shutdown()
        listener.join()


def authorize_interactive(
    client: OAuthClient,
    http: httpx.Client,
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    on_url: Callable[[str], None] | None = None,
    timeout_s: float = 300.0,
    token_url: str = TOKEN_URL,
    auth_url: str = AUTH_URL,
    store: SecretStore | None = None,
) -> TokenSet:
    """Open the consent URL and exchange a valid callback for a Drive grant.

    The optional on_url hook publishes the URL before the browser launches.
    Supply a store to persist the refresh token before returning the grant.
    Callback and OAuth failures raise AuthError. Storage failures propagate.
    """
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    deadline = time.monotonic() + timeout_s
    with _callback_server(state, deadline) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        query = urlencode(
            {
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        url = f"{auth_url}?{query}"
        if on_url is not None:
            on_url(url)
        Thread(target=_launch_browser, args=(open_browser, url), daemon=True).start()
        if not server.ready.wait(max(0.0, deadline - time.monotonic())):
            raise AuthError(code="auth_timeout")

    code = server.code
    if code is None:
        raise AuthError(code="auth_denied")
    tokens = _exchange(
        client,
        http,
        token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        issued_at=time.time(),
        initial=True,
    )
    if store is not None:
        assert tokens.refresh_token is not None
        store.set(REFRESH_TOKEN_KEY, tokens.refresh_token, label=_KEYRING_LABEL)
    return tokens


def _post(http: httpx.Client, url: str, data: dict[str, str]) -> httpx.Response:
    try:
        # A redirect must not forward a form containing credentials to a different endpoint.
        return http.post(url, data=data, follow_redirects=False)
    except (httpx.RequestError, httpx.InvalidURL, OSError):
        pass
    # Keep the transport exception and its request body out of the exception chain.
    raise AuthError(code="auth_network")


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except (ValueError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _exchange(
    client: OAuthClient,
    http: httpx.Client,
    url: str,
    grant: dict[str, str],
    *,
    issued_at: float,
    initial: bool,
) -> TokenSet:
    response = _post(
        http, url, {"client_id": client.client_id, "client_secret": client.client_secret, **grant}
    )
    payload = _json_object(response)
    if not response.is_success or "error" in payload:
        if not initial and payload.get("error") == "invalid_grant":
            raise AuthError(code="reauth_required", needs_reconnect=True)
        raise AuthError()

    # RFC 6749 allows omitting an unchanged scope. Initial consent must verify it explicitly.
    scope = payload.get("scope", "" if initial else SCOPE)
    refresh = payload.get("refresh_token")
    refresh_token = refresh if isinstance(refresh, str) and refresh.strip() else None
    if (
        not isinstance(scope, str)
        or SCOPE not in scope.split()
        or (initial and refresh_token is None)
    ):
        raise AuthError(code="auth_scope_missing")
    access_token = payload.get("access_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not access_token.isascii()
        or any(char.isspace() or not char.isprintable() for char in access_token)
    ):
        raise AuthError()
    lifetime = payload.get("expires_in")
    if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or lifetime <= 0:
        raise AuthError()
    try:
        expires_at = issued_at + lifetime
    except OverflowError:
        expires_at = math.inf
    if not math.isfinite(expires_at):
        raise AuthError()
    return TokenSet(access_token, refresh_token, expires_at, scope)


class RefreshingTokenProvider:
    """Supply cached access tokens to Drive, refreshing through the secret store.

    Concurrent callers share a refresh in progress. Failed or revoked grants
    raise AuthError and keyring errors propagate from the store.
    """

    def __init__(
        self,
        client: OAuthClient,
        store: SecretStore,
        http: httpx.Client,
        *,
        clock: Callable[[], float] = time.time,
        token_url: str = TOKEN_URL,
    ) -> None:
        self._client = client
        self._store = store
        self._http = http
        self._clock = clock
        self._token_url = token_url
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._flight: Future[str] | None = None
        self._idle = Condition()

    def token(self) -> str:
        """Return an access token, refreshing within sixty seconds of expiration."""
        with self._idle:
            if self._access_token and self._clock() < self._expires_at - _REFRESH_MARGIN:
                return self._access_token
            flight = self._flight
            lead = flight is None
            if flight is None:
                self._flight = flight = Future()
        if not lead:
            return flight.result()

        try:
            tokens = self._refresh()
        except BaseException as error:
            with self._idle:
                flight.set_exception(error)
                self._flight = None
                self._idle.notify_all()
            raise
        with self._idle:
            self._access_token = tokens.access_token
            self._expires_at = tokens.expires_at
            flight.set_result(tokens.access_token)
            self._flight = None
            self._idle.notify_all()
        return tokens.access_token

    def invalidate(self) -> None:
        """Clear the cache after any active refresh so the next lookup refreshes."""
        with self._idle:
            if self._flight is not None:
                self._idle.wait_for(self._flight.done)
            self._access_token = None

    def _refresh(self) -> TokenSet:
        refresh = self._store.get(REFRESH_TOKEN_KEY)
        if not refresh:
            raise AuthError(code="reauth_required", needs_reconnect=True)
        tokens = _exchange(
            self._client,
            self._http,
            self._token_url,
            {"grant_type": "refresh_token", "refresh_token": refresh},
            issued_at=self._clock(),
            initial=False,
        )
        if tokens.refresh_token is not None and tokens.refresh_token != refresh:
            self._store.set(REFRESH_TOKEN_KEY, tokens.refresh_token, label=_KEYRING_LABEL)
        return tokens


class StaticTokenProvider:
    """Supply a fixed access token for a loopback test hook."""

    def __init__(self, token: str) -> None:
        self._token = token

    def token(self) -> str:
        """Return the configured test token."""
        return self._token

    def invalidate(self) -> None:
        """Keep the fixed token available after invalidation."""


def revoke(token: str, http: httpx.Client, *, revoke_url: str = REVOKE_URL) -> None:
    """Revoke an OAuth token, accepting an already-invalid token as revoked.

    Transport failures raise AuthError with code auth_network. Other endpoint
    errors raise AuthError with code auth_failed.
    """
    response = _post(http, revoke_url, {"token": token})
    if response.is_success:
        return
    if (
        response.status_code == HTTPStatus.BAD_REQUEST
        and _json_object(response).get("error") == "invalid_token"
    ):
        return
    raise AuthError()
