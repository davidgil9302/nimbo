"""Store credentials in the desktop Secret Service or in test-only memory."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from threading import Lock
from typing import Any, Protocol

import secretstorage
from jeepney.wrappers import DBusErrorResponse

from .errors import SecretStoreError

REFRESH_TOKEN_KEY = "refresh_token"  # noqa: S105 - Secret Service lookup key


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


def _attributes(key: str) -> dict[str, str]:
    return {"application": "nimbo", "key": key}


class SecretServiceStore:
    """Store UTF-8 secrets in the default Secret Service collection via D-Bus.

    The optional connect factory supplies a fresh SecretStorage-compatible
    D-Bus connection per operation. Operations raise SecretStoreError when the
    collection cannot be unlocked or the backend cannot complete the request.
    """

    def __init__(self, *, connect: Callable[[], Any] | None = None) -> None:
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
        """Return the newest matching item's UTF-8 value, or None if absent."""
        with self._collection() as collection:
            newest = max(
                collection.search_items(_attributes(key)),
                key=lambda item: item.get_created(),
                default=None,
            )
            return newest.get_secret().decode("utf-8") if newest is not None else None

    def set(self, key: str, value: str, *, label: str) -> None:
        """Write the replacement, then remove any remaining items for the same key."""
        attributes = _attributes(key)
        with self._collection() as collection:
            previous = list(collection.search_items(attributes))
            current = collection.create_item(label, attributes, value.encode("utf-8"), replace=True)
            for item in previous:
                if item.item_path != current.item_path:
                    self._delete_item(item)

    def delete(self, key: str) -> None:
        """Delete every matching item in the default collection."""
        with self._collection() as collection:
            for item in list(collection.search_items(_attributes(key))):
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
