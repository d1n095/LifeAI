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

        try:
            _storage = LocalFilesystemStorage(get_settings().storage_root)
        except OSError as exc:
            # A misconfigured or unwritable STORAGE_ROOT used to escape as a raw OSError from
            # a request handler, producing an opaque 500 (observed: PermissionError on the
            # production default /var/lib/lifeai while running locally). It is a dependency
            # being unavailable, never a bug in the request — report it as such, and never
            # include the filesystem path or the OS error text in the message a caller sees.
            raise StorageError(
                "Lagringsutrymmet är inte tillgängligt. Kontrollera att STORAGE_ROOT pekar på "
                "en katalog som applikationen får skriva till."
            ) from exc
    return _storage
