"""Drive OAuth form exchanges and authentication with the resulting tokens."""

from __future__ import annotations

import base64
import hashlib

import httpx

from . import FakeDrive
from .test_fake_contract import ABOUT, Clock

CLIENT = {"client_id": "demo-client", "client_secret": "demo-secret"}
REDIRECT = "http://127.0.0.1:43210/"
VERIFIER = "v" * 43
SCOPE = "https://www.googleapis.com/auth/drive"


def challenge(verifier: str) -> str:
    """Return an unpadded S256 PKCE challenge."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def authorization(fake: FakeDrive) -> dict[str, str]:
    """Register a client and construct the token form for a fresh code."""
    fake.oauth.register_client(CLIENT["client_id"], CLIENT["client_secret"])
    code = fake.oauth.authorize(CLIENT["client_id"], REDIRECT, challenge(VERIFIER), "state", SCOPE)
    return {
        **CLIENT,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }


def refresh(fake: FakeDrive, client: httpx.Client, token: str) -> httpx.Response:
    """Exchange a refresh token using the registered client's credentials."""
    return client.post(
        fake.TOKEN_URL,
        data={**CLIENT, "grant_type": "refresh_token", "refresh_token": token},
    )


def test_code_exchange_with_pkce() -> None:
    fake = FakeDrive()
    data = authorization(fake)
    with fake.client() as client:
        assert (
            client.post(
                fake.TOKEN_URL, data={**data, "redirect_uri": REDIRECT + "wrong"}
            ).status_code
            == 400
        )
        response = client.post(fake.TOKEN_URL, data=data)
        response.raise_for_status()
        tokens = response.json()
        assert tokens["token_type"] == "Bearer"
        assert tokens["scope"] == SCOPE
        assert tokens["refresh_token"]
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.get(ABOUT, headers=headers).status_code == 200
        assert client.post(fake.TOKEN_URL, data=data).json()["error"] == "invalid_grant"


def test_wrong_verifier_rejected() -> None:
    fake = FakeDrive()
    data = {**authorization(fake), "code_verifier": "x" * 43}
    with fake.client() as client:
        response = client.post(fake.TOKEN_URL, data=data)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"


def test_refresh_and_expiry() -> None:
    clock = Clock()
    fake = FakeDrive(clock=clock)
    with fake.client() as client:
        first = client.post(fake.TOKEN_URL, data=authorization(fake)).json()
        clock.advance(first["expires_in"])
        headers = {"Authorization": f"Bearer {first['access_token']}"}
        assert client.get(ABOUT, headers=headers).status_code == 401
        response = refresh(fake, client, first["refresh_token"])
        response.raise_for_status()
        second = response.json()["access_token"]
        assert second != first["access_token"]
        assert client.get(ABOUT, headers={"Authorization": f"Bearer {second}"}).status_code == 200


def test_invalid_grant_switch() -> None:
    fake = FakeDrive()
    with fake.client() as client:
        first = client.post(fake.TOKEN_URL, data=authorization(fake)).json()
        fake.oauth.fail_refresh_with = "invalid_grant"
        response = refresh(fake, client, first["refresh_token"])
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        fake.oauth.fail_refresh_with = None
        assert refresh(fake, client, first["refresh_token"]).status_code == 200


def test_revoke() -> None:
    fake = FakeDrive()
    with fake.client() as client:
        tokens = client.post(fake.TOKEN_URL, data=authorization(fake)).json()
        other = refresh(fake, client, tokens["refresh_token"]).json()["access_token"]
        response = client.post(fake.REVOKE_URL, data={"token": tokens["refresh_token"]})
        assert response.status_code == 200
        assert refresh(fake, client, tokens["refresh_token"]).json()["error"] == "invalid_grant"
        for token in (tokens["access_token"], other):
            assert (
                client.get(ABOUT, headers={"Authorization": f"Bearer {token}"}).status_code == 401
            )
