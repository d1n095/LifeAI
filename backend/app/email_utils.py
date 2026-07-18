import re
import unicodedata

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    """NFKC-normalizes (collapses visually-identical Unicode variants that could otherwise
    register as "different" emails and defeat the uniqueness check) and lowercases. Every
    write path (registration, bootstrap admin) must go through this so the unique index on
    users.email actually enforces one account per real-world address."""
    return unicodedata.normalize("NFKC", email).strip().lower()
