"""Errors the CLI handles. cli catches only AgyswapError: it prints one line (the error envelope with --json) and exits 1."""


class AgyswapError(Exception):
    pass


class ConfigError(AgyswapError):
    pass


class AccountNotFoundError(AgyswapError):
    pass


class CredentialError(AgyswapError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class TransferError(AgyswapError):
    """export, import and import-usage: an unusable file or account (same name as cswap's)."""


def error_envelope(exc: Exception) -> dict:
    """Error envelope for --json, same as cswap: stdout still holds exactly one JSON object."""
    return {"schemaVersion": 1, "error": {"type": type(exc).__name__, "message": str(exc)}}
