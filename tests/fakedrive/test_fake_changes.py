"""Check change snapshots, cursors, removal and projection."""

from __future__ import annotations

import httpx

from . import FakeDrive
from .test_fake_contract import FILES, connect

CHANGES = "/drive/v3/changes"


def token(client: httpx.Client) -> str:
    """Return the current starting cursor for later changes."""
    return str(client.get(f"{CHANGES}/startPageToken").json()["startPageToken"])


def test_start_token_and_incremental_changes() -> None:
    fake = FakeDrive()
    fake.add_folder("before")
    with connect(fake) as client:
        cursor = token(client)
        file_id = fake.add_file("original", fake.root_id, b"abc")
        client.patch(f"{FILES}/{file_id}", json={"name": "renamed"}).raise_for_status()
        params = {
            "pageToken": cursor,
            "spaces": "drive",
            "fields": "newStartPageToken,changes(changeType,fileId,removed,file(name,size),time)",
        }
        payload = client.get(CHANGES, params=params).json()
        new_cursor = token(client)
        assert payload["newStartPageToken"] == new_cursor
        assert new_cursor.isdecimal()
        for change, name in zip(payload["changes"], ("original", "renamed"), strict=True):
            assert change == {
                "changeType": "file",
                "fileId": file_id,
                "removed": False,
                "file": {"name": name, "size": "3"},
                "time": change["time"],
            }
            assert change["time"].endswith("Z")
        assert client.get(CHANGES, params={"pageToken": new_cursor}).json()["changes"] == []


def test_changes_pagination() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        cursor = token(client)
        ids = [fake.add_folder(str(i)) for i in range(4)]
        params: dict[str, str | int] = {"pageToken": cursor, "pageSize": 2}
        first = client.get(CHANGES, params=params).json()
        assert [item["fileId"] for item in first["changes"]] == ids[:2]
        assert "newStartPageToken" not in first
        assert first["nextPageToken"].isdecimal()
        params["pageToken"] = first["nextPageToken"]
        second = client.get(CHANGES, params=params).json()
        assert [item["fileId"] for item in second["changes"]] == ids[2:]
        assert "nextPageToken" not in second
        assert second["newStartPageToken"] == token(client)


def test_removed_changes() -> None:
    fake = FakeDrive()
    parent = fake.add_folder("parent")
    child = fake.add_file("child", parent, b"")
    with connect(fake) as client:
        cursor = token(client)
        response = client.delete(f"{FILES}/{parent}")
        assert response.status_code == 204
        assert client.get(f"{FILES}/{child}").status_code == 404
        params = {"pageToken": cursor, "fields": "newStartPageToken,changes(fileId,removed,file)"}
        payload = client.get(CHANGES, params=params).json()
        assert {item["fileId"] for item in payload["changes"]} == {parent, child}
        assert all(item["removed"] is True and "file" not in item for item in payload["changes"])
        hidden = client.get(CHANGES, params={**params, "includeRemoved": "false"}).json()
        assert hidden == {"changes": [], "newStartPageToken": token(client)}


def test_invalid_token_400() -> None:
    with connect(FakeDrive()) as client:
        for params in ({}, {"pageToken": "-1"}, {"pageToken": "abc"}, {"pageToken": "99999"}):
            assert client.get(CHANGES, params=params).status_code == 400
