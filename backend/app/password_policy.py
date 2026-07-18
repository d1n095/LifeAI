import re

# NIST 800-63B favors length over forced complexity, plus a check against known-weak
# passwords, over arbitrary symbol/uppercase rules that mostly just push people toward
# predictable substitutions (e.g. "Password1!"). This is a small local denylist, not a
# breach-corpus lookup — this deployment has no outbound call to a third-party password API.
MIN_LENGTH = 12
MAX_LENGTH = 256

_COMMON_WEAK_PASSWORDS = {
    "password1234", "password123456", "123456789012", "1234567890123",
    "qwertyuiop123", "welcome1234567", "letmein1234567", "administrator1",
    "changeme12345", "iloveyou123456", "dragon12345678", "monkey123456789",
    "football12345", "baseball1234567", "trustno112345", "sunshine12345",
    "princess12345", "starwars123456", "superman123456", "whatever123456",
    "abcdefghijkl", "aaaaaaaaaaaa", "123123123123", "qazwsxedc1234",
}


def validate_password(password: str, email: str | None = None) -> None:
    """Raises ValueError with a Swedish, user-facing message on the first violated rule."""
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Lösenordet måste vara minst {MIN_LENGTH} tecken.")
    if len(password) > MAX_LENGTH:
        raise ValueError("Lösenordet är för långt.")
    if not re.search(r"[A-Za-zÅÄÖåäö]", password):
        raise ValueError("Lösenordet måste innehålla minst en bokstav.")
    if not re.search(r"\d", password):
        raise ValueError("Lösenordet måste innehålla minst en siffra.")
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        raise ValueError("Lösenordet är för vanligt och lätt att gissa. Välj ett annat.")
    if email:
        local_part = email.split("@")[0].lower()
        if local_part and local_part in password.lower():
            raise ValueError("Lösenordet får inte innehålla din e-postadress.")
