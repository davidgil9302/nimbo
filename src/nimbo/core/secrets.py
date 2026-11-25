"""Store credentials in the desktop Secret Service or in test-only memory."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

import secretstorage
from jeepney.wrappers import DBusErrorResponse

from .errors import SecretStoreError

REFRESH_TOKEN_KEY = "refresh_token"  # noqa: S105 - Secret Service lookup key
_PROFILE = "profile"


def profile_id(config_dir: Path) -> str:
    """Return the keyring profile for a configuration directory.

    Each NIMBO_CONFIG_DIR is a separate installation, so its credential lives in
    a separate Secret Service item identified by a hash of the resolved path.
    """
    resolved = str(config_dir.expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8", "surrogateescape")).hexdigest()[:32]


class SecretStore(Protocol):
    """Read, replace and delete secrets by application key."""

    def get(self, key: str) -> str | None:
        """Return the stored secret or None if the key is absent."""

    def set(self, key: str, value: str, *, label: str) -> None:
        """Replace the secret for the key with the given display label."""

    def delete(self, key: str) -> None:
        """Remove all stored secrets matching the key, if any."""


class MemorySecretStore:
    """Keep secrets in memory for tests and loopback test hooks."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = Lock()

    def get(self, key: str) -> str | None:
        """Return the secret for the key or None if absent."""
        with self._lock:
            return self._values.get(key)

    def set(self, key: str, value: str, *, label: str) -> None:
        """Store the value under the key. The memory store has no display labels."""
        with self._lock:
            self._values[key] = value

    def delete(self, key: str) -> None:
        """Discard the value for the key, if present."""
        with self._lock:
            self._values.pop(key, None)


def _attributes(key: str, profile: str | None = None) -> dict[str, str]:
    attributes = {"application": "nimbo", "key": key}
    if profile is not None:
        attributes[_PROFILE] = profile
    return attributes


class SecretServiceStore:
    """Store UTF-8 secrets for one profile in the default Secret Service collection.

    Items carry the profile attribute (see profile_id), and every lookup,
    replacement and deletion names it, so installations never read, replace or
    delete each other's credentials. The optional connect factory supplies a
    fresh SecretStorage-compatible D-Bus connection per operation. Operations
    raise SecretStoreError when the collection cannot be unlocked or the backend
    cannot complete the request.
    """

    def __init__(self, *, profile: str, connect: Callable[[], Any] | None = None) -> None:
        if not profile:
            raise ValueError("A keyring profile is required")
        self._profile = profile
        self._connect = connect if connect is not None else secretstorage.dbus_init
        self._lock = Lock()

    @contextmanager
    def _collection(self) -> Iterator[secretstorage.Collection]:
        try:
            with self._lock, closing(self._connect()) as connection:
                collection = secretstorage.get_default_collection(connection)
                if collection.is_locked():
                    collection.unlock()
                    if collection.is_locked():
                        raise SecretStoreError(code="secret_store_locked")
                yield collection
        except (secretstorage.LockedException, secretstorage.PromptDismissedException):
            code = "secret_store_locked"
        except DBusErrorResponse as error:
            code = (
                "secret_store_locked"
                if error.name == "org.freedesktop.Secret.Error.IsLocked"
                else "secret_store_unavailable"
            )
        except (secretstorage.SecretStorageException, OSError, RuntimeError, ValueError):
            code = "secret_store_unavailable"
        else:
            return
        # Raise outside the handler to avoid retaining backend errors containing secrets.
        raise SecretStoreError(code=code)

    def get(self, key: str) -> str | None:
        """Return the profile's newest UTF-8 value, adopting a lone legacy item if absent.

        Items written before profiles existed carry no profile attribute. When
        the profile has no item and exactly one such legacy item exists, it is
        copied into the profile's item and the legacy item is deleted. With
        several legacy items the owner is ambiguous, so none is adopted.
        """
        attributes = _attributes(key, self._profile)
        with self._collection() as collection:
            newest = max(
                collection.search_items(attributes),
                key=lambda item: item.get_created(),
                default=None,
            )
            if newest is not None:
                return newest.get_secret().decode("utf-8")
            legacy = [
                item
                for item in collection.search_items(_attributes(key))
                if _PROFILE not in item.get_attributes()
            ]
            if len(legacy) != 1:
                return None
            (item,) = legacy
            secret = item.get_secret()
            collection.create_item(item.get_label(), attributes, secret, replace=True)
            self._delete_item(item)
            return secret.decode("utf-8")

    def set(self, key: str, value: str, *, label: str) -> None:
        """Write the replacement, then remove the profile's other items for the key."""
        attributes = _attributes(key, self._profile)
        with self._collection() as collection:
            previous = list(collection.search_items(attributes))
            current = collection.create_item(label, attributes, value.encode("utf-8"), replace=True)
            for item in previous:
                if item.item_path != current.item_path:
                    self._delete_item(item)

    def delete(self, key: str) -> None:
        """Delete every item of this profile for the key in the default collection."""
        with self._collection() as collection:
            for item in list(collection.search_items(_attributes(key, self._profile))):
                self._delete_item(item)

    @staticmethod
    def _delete_item(item: secretstorage.Item) -> None:
        try:
            item.delete()
        except secretstorage.PromptDismissedException:
            raise
        except secretstorage.ItemNotFoundException:
            # Replacement or another client may have already removed the item.
            pass
