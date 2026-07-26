from app.storage.base import StorageBackend, StorageError, StorageIntegrityError, StoredBlob
from app.storage.local_fs import LocalFilesystemStorage

__all__ = [
    "StorageBackend",
    "StorageError",
    "StorageIntegrityError",
    "StoredBlob",
    "LocalFilesystemStorage",
    "get_storage",
]

_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Process-wide storage backend singleton. Only a LocalFilesystemStorage is implemented
    today (see StorageBackend's own docstring for why the abstraction exists ahead of a
    second implementation) — swapping it later (e.g. an object-storage backend) is a matter
    of changing what this function constructs, not touching any caller."""
    global _storage
    if _storage is None:
        from app.config import get_settings

        _storage = LocalFilesystemStorage(get_settings().storage_root)
    return _storage
