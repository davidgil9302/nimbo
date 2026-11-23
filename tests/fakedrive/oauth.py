"""In-memory Google OAuth code, refresh and revocation flows."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from hmac import compare_digest
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx

from .state import DriveState

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_ACCESS_TTL = 3600
_CODE_TTL = 600
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class _Authorization:
    client_id: str
    redirect_uri: str
    challenge: str
    scope: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Grant:
    client_id: str
    scope: str


@dataclass(frozen=True, slots=True)
class _Access:
    expires_at: float | None
    refresh_token: str | None = None


class FakeOAuth:
    """Issue and revoke tokens sharing the Drive fake's lock and clock."""

    def __init__(self, state: DriveState) -> None:
        self._state = state
        self._clients: dict[str, str] = {}
        self._codes: dict[str, _Authorization] = {}
        self._refresh: dict[str, _Grant] = {}
        self._access: dict[str, _Access] = {}
        self._fail_refresh_with: str | None = None

    @property
    def fail_refresh_with(self) -> str | None:
        """Return the forced refresh error, or None for normal behavior."""
        with self._state.lock:
            return self._fail_refresh_with

    @fail_refresh_with.setter
    def fail_refresh_with(self, reason: str | None) -> None:
        with self._state.lock:
            self._fail_refresh_with = reason

    def register_client(self, client_id: str, secret: str) -> None:
        """Register a client ID and its credential for the token endpoint."""
        with self._state.lock:
            if not client_id:
                raise ValueError("Client ID is required")
            self._clients[client_id] = secret

    def add_access_token(self, token: str, *, expires_in: float | None = None) -> str:
        """Register and return a bearer token, optionally expiring after expires_in seconds."""
        with self._state.lock:
            if not token or any(char.isspace() for char in token):
                raise ValueError("Expected a bearer token without whitespace")
            expiry = None if expires_in is None else self._state.now() + expires_in
            self._access[token] = _Access(expiry)
            return token

    def authorize(
        self, client_id: str, redirect_uri: str, code_challenge: str, state: str, scope: str
    ) -> str:
        """Issue a code for a registered client and S256 challenge, or raise ValueError."""
        with self._state.lock:
            try:
                url = httpx.URL(redirect_uri)
            except httpx.InvalidURL as err:
                raise ValueError("Invalid redirect URI") from err
            if client_id not in self._clients:
                raise ValueError("Unknown OAuth client")
            if url.scheme not in {"http", "https"} or not url.host or url.fragment:
                raise ValueError("Invalid redirect URI")
            if not _CHALLENGE.fullmatch(code_challenge):
                raise ValueError("Invalid S256 code challenge")
            code = uuid4().hex
            self._codes[code] = _Authorization(
                client_id, redirect_uri, code_challenge, scope, self._state.now() + _CODE_TTL
            )
            return code

    def _accepts(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return False
        access = self._access.get(token.strip())
        return access is not None and (
            access.expires_at is None or self._state.now() < access.expires_at
        )

    def _authorization(self, request: httpx.Request, redirect_base: str | None) -> httpx.Response:
        params = request.url.params
        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256":
            return _error("invalid_request", "Expected a code request with PKCE S256.")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        try:
            destination = httpx.URL(redirect_uri)
            if redirect_base is not None and not redirect_uri.startswith(redirect_base + "/"):
                return _error("invalid_request", "The redirect URI must target the fake server.")
            code = self.authorize(
                params.get("client_id", ""),
                redirect_uri,
                params.get("code_challenge", ""),
                state,
                params.get("scope", ""),
            )
        except (ValueError, httpx.InvalidURL):
            return _error("invalid_request", "Invalid OAuth authorization request.")
        location = destination.copy_merge_params({"code": code, "state": state})
        return httpx.Response(302, headers={"Location": str(location)})

    def _token(self, request: httpx.Request) -> httpx.Response:
        try:
            data = _form(request)
        except ValueError:
            return _error("invalid_request", "Expected a URL-encoded token request.")
        client_id = data.get("client_id", "")
        secret = self._clients.get(client_id)
        if secret is None or not compare_digest(
            secret.encode(), data.get("client_secret", "").encode()
        ):
            return _error("invalid_client", "The OAuth client credentials are invalid.", 401)
        grant_type = data.get("grant_type")
        if grant_type == "authorization_code":
            return self._exchange_code(data, client_id)
        if grant_type == "refresh_token":
            if self._fail_refresh_with is not None:
                return _error(self._fail_refresh_with, "The refresh request was rejected.")
            token = data.get("refresh_token", "")
            grant = self._refresh.get(token)
            if grant is None or grant.client_id != client_id:
                return _error("invalid_grant", "The refresh token is invalid or revoked.")
            return httpx.Response(200, json=self._new_access(token, grant.scope))
        return _error("unsupported_grant_type", "The grant type is unsupported.")

    def _exchange_code(self, data: dict[str, str], client_id: str) -> httpx.Response:
        code = data.get("code", "")
        authorization = self._codes.get(code)
        verifier = data.get("code_verifier", "")
        if (
            authorization is None
            or authorization.client_id != client_id
            or authorization.redirect_uri != data.get("redirect_uri")
            or self._state.now() >= authorization.expires_at
            or not _VERIFIER.fullmatch(verifier)
            or not compare_digest(authorization.challenge, _s256(verifier))
        ):
            return _error("invalid_grant", "The authorization code or verifier is invalid.")
        del self._codes[code]
        refresh = f"fake-refresh-{uuid4().hex}"
        self._refresh[refresh] = _Grant(client_id, authorization.scope)
        result = self._new_access(refresh, authorization.scope)
        result["refresh_token"] = refresh
        return httpx.Response(200, json=result)

    def _new_access(self, refresh: str, scope: str) -> dict[str, str | int]:
        access = f"fake-access-{uuid4().hex}"
        self._access[access] = _Access(self._state.now() + _ACCESS_TTL, refresh)
        return {
            "access_token": access,
            "expires_in": _ACCESS_TTL,
            "token_type": "Bearer",
            "scope": scope,
        }

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        try:
            token = (
                request.url.params.get("token")
                if not request.content
                else _form(request).get("token")
            )
        except ValueError:
            return _error("invalid_request", "Expected a URL-encoded revocation request.")
        if not token:
            return _error("invalid_request", "A token is required.")
        access = self._access.pop(token, None)
        refresh = token if access is None else access.refresh_token
        if refresh is not None and refresh in self._refresh:
            del self._refresh[refresh]
            self._access = {
                key: item for key, item in self._access.items() if item.refresh_token != refresh
            }
        elif access is None:
            return _error("invalid_token", "The token is invalid or revoked.")
        return httpx.Response(200)


def _form(request: httpx.Request) -> dict[str, str]:
    if request.headers.get("Content-Type", "").partition(";")[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        raise ValueError("Expected a form body")
    pairs = parse_qsl(
        request.content.decode("utf-8"),
        keep_blank_values=True,
        strict_parsing=True,
        errors="strict",
    )
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Repeated form parameters")
    return dict(pairs)


def _error(reason: str, message: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": reason, "error_description": message})


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
