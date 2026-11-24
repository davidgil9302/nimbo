"""Exercise keyring operations with the SecretStorage API and an opt-in real service."""

from __future__ import annotations

import os
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
import secretstorage

from nimbo.core.errors import SecretStoreError
from nimbo.core.secrets import MemorySecretStore, SecretServiceStore

PROFILE = "0123456789abcdef0123456789abcdef"


@dataclass
class _Item:
    collection: _Collection
    item_path: str
    attributes: dict[str, str]
    label: str
    secret: bytes = field(repr=False)
    created: int

    def get_secret(self) -> bytes:
        return self.secret

    def get_created(self) -> int:
        return self.created

    def get_label(self) -> str:
        return self.label

    def get_attributes(self) -> dict[str, str]:
        return dict(self.attributes)

    def delete(self) -> None:
        self.collection.items.remove(self)


class _Collection:
    def __init__(self) -> None:
        self.items: list[_Item] = []
        self.serial = 0
        self.locked = False
        self.refuse_unlock = False
        self.unlock_calls = 0

    def is_locked(self) -> bool:
        return self.locked

    def unlock(self) -> bool:
        self.unlock_calls += 1
        self.locked = self.refuse_unlock
        return self.refuse_unlock

    def search_items(self, attributes: dict[str, str]) -> Iterator[_Item]:
        # Deliberately yield the oldest items first.
        return (item for item in self.items if attributes.items() <= item.attributes.items())

    def create_item(
        self, label: str, attributes: dict[str, str], secret: bytes, replace: bool = False
    ) -> _Item:
        if replace:
            for item in self.search_items(attributes):
                if item.attributes == attributes:
                    item.label, item.secret = label, secret
                    return item
        self.serial += 1
        item = _Item(self, f"/item/{self.serial}", attributes, label, secret, self.serial)
        self.items.append(item)
        return item


class _Bus:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[SecretServiceStore, _Collection]]:
    """Use a fresh instrumented connection for each Secret Service operation."""
    collection = _Collection()
    connections: list[_Bus] = []

    def connect() -> _Bus:
        bus = _Bus()
        connections.append(bus)
        return bus

    def default(bus: _Bus) -> _Collection:
        assert bus is connections[-1] and not bus.closed
        return collection

    monkeypatch.setattr(secretstorage, "get_default_collection", default)
    yield SecretServiceStore(profile=PROFILE, connect=connect), collection
    assert all(bus.closed for bus in connections)


def test_memory_store_crud() -> None:
    """Memory storage replaces values and keeps its representation free of secrets."""
    store = MemorySecretStore()
    assert store.get("key") is None
    store.set("key", "sentinel-first", label="Test")
    store.set("key", "sentinel-second", label="Test")
    assert store.get("key") == "sentinel-second"
    assert "sentinel-second" not in repr(store)
    assert "sentinel-second" not in str(store)
    store.delete("key")
    store.delete("key")
    assert store.get("key") is None


def test_secret_service_store_with_fake_backend(
    keyring: tuple[SecretServiceStore, _Collection],
) -> None:
    """Select the newest secret, replace duplicates, and preserve unrelated items."""
    store, collection = keyring
    attributes = {"application": "nimbo", "key": "key", "profile": PROFILE}
    unrelated = collection.create_item("Other", {**attributes, "application": "other"}, b"other")
    other_profile = collection.create_item("Profile", {**attributes, "profile": "x"}, b"x")
    assert store.get("key") is None
    collection.create_item("Old", attributes, b"old")
    collection.create_item("New", {**attributes, "extra": "value"}, "más reciente".encode())
    assert store.get("key") == "más reciente"

    store.set("key", "nuevo valor", label="Nimbo test")
    (item,) = collection.search_items(attributes)
    assert item.attributes == attributes
    assert item.label == "Nimbo test"
    assert item.secret == b"nuevo valor"
    assert store.get("key") == "nuevo valor"
    collection.create_item("Duplicate", attributes, b"duplicate")
    store.delete("key")
    store.delete("key")
    assert store.get("key") is None
    assert collection.items == [unrelated, other_profile]


def test_locked_collection_unlock_success(keyring: tuple[SecretServiceStore, _Collection]) -> None:
    """Retry a locked default collection after its unlock prompt succeeds."""
    store, collection = keyring
    collection.locked = True
    store.set("key", "value", label="Test")
    assert store.get("key") == "value"
    assert collection.unlock_calls == 1


def test_locked_collection_unlock_refused_raises(
    keyring: tuple[SecretServiceStore, _Collection],
) -> None:
    """Return the locked code when the default collection stays locked."""
    store, collection = keyring
    collection.locked = collection.refuse_unlock = True
    with pytest.raises(SecretStoreError) as caught:
        store.get("key")
    assert caught.value.code == "secret_store_locked"
    assert collection.unlock_calls == 1


def test_dbus_unavailable_raises() -> None:
    """Sanitize D-Bus failures, including their original exception context."""

    def connect() -> _Bus:
        raise secretstorage.SecretServiceNotAvailableException("sentinel-secret")

    store = SecretServiceStore(profile=PROFILE, connect=connect)
    with pytest.raises(SecretStoreError) as caught:
        store.get("key")
    assert caught.value.code == "secret_store_unavailable"
    assert caught.value.__context__ is None
    assert "sentinel-secret" not in "".join(traceback.format_exception(caught.value))


def test_failed_replacement_preserves_existing_secret(
    keyring: tuple[SecretServiceStore, _Collection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the existing credential when the keyring refuses the replacement."""
    store, collection = keyring
    store.set("key", "previous", label="Test")

    def fail(*_args: object, **_kwargs: object) -> _Item:
        raise OSError("sentinel-secret")

    monkeypatch.setattr(collection, "create_item", fail)
    with pytest.raises(SecretStoreError, match=r"^secret_store_unavailable$"):
        store.set("key", "sentinel-secret", label="Test")
    assert store.get("key") == "previous"


@pytest.mark.skipif(
    os.environ.get("NIMBO_TEST_KEYRING") != "1", reason="requires an opted-in keyring"
)
def test_real_secret_service_roundtrip() -> None:
    """Round-trip a dedicated disposable key in the real default collection."""
    store = SecretServiceStore(profile=f"test-{uuid4().hex}")
    key = f"test-{uuid4().hex}"
    try:
        assert store.get(key) is None
        store.set(key, "test only", label="nimbo test")
        assert store.get(key) == "test only"
    finally:
        store.delete(key)
    assert store.get(key) is None
