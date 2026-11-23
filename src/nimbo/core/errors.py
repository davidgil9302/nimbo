"""Exception hierarchy.

Every error carries a stable machine-readable ``code`` (used for i18n lookups,
run summaries and exit codes) and a free-text ``detail`` for logs. Messages shown
to people are built from the code, never from ``detail``.
"""

from __future__ import annotations


class NimboError(Exception):
    code: str = "error"

    def __init__(self, detail: str = "", *, code: str | None = None) -> None:
        super().__init__(detail or self.code)
        self.detail = detail
        if code is not None:
            self.code = code


class ConfigError(NimboError):
    code = "config_invalid"


class LockedError(NimboError):
    code = "already_running"


class SafetyError(NimboError):
    code = "preflight_failed"


class SecretStoreError(NimboError):
    code = "secret_store_unavailable"


class AuthError(NimboError):
    code = "auth_failed"

    def __init__(
        self, detail: str = "", *, needs_reconnect: bool = False, code: str | None = None
    ) -> None:
        super().__init__(detail, code=code)
        self.needs_reconnect = needs_reconnect


class DriveError(NimboError):
    """An error answered by (or on the way to) the Drive API."""

    code = "drive_error"
    retryable: bool = False

    def __init__(
        self, detail: str = "", *, status: int = 0, reason: str = "", code: str | None = None
    ) -> None:
        super().__init__(detail, code=code)
        self.status = status
        self.reason = reason


class TransportFailed(DriveError):
    code = "network_failed"
    retryable = True


class RateLimited(DriveError):
    code = "rate_limited"
    retryable = True


class ServerError(DriveError):
    code = "server_error"
    retryable = True


class Unauthorized(DriveError):
    code = "unauthorized"


class BadRequest(DriveError):
    code = "bad_request"


class Forbidden(DriveError):
    code = "forbidden"


class NotFound(DriveError):
    code = "not_found"


class Conflict(DriveError):
    code = "conflict"


class StorageQuotaExceeded(DriveError):
    code = "storage_quota_exceeded"


class UploadLimitExceeded(DriveError):
    code = "upload_limit_exceeded"


class SessionExpired(DriveError):
    code = "upload_session_expired"


class FileChanged(NimboError):
    """The local file changed (size/mtime) while it was being read."""

    code = "file_changed"


class Md5Mismatch(NimboError):
    code = "md5_mismatch"


class Stopped(NimboError):
    """Raised at a safe point after a stop was requested."""

    code = "stopped"
