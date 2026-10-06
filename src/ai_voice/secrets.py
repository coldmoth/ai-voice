"""Manage project keys without exposing values in process arguments or output."""
import subprocess

KEYCHAIN_SERVICE_PREFIX = "ai-voice"
KEYCHAIN_ACCOUNT = "default"


def load_key(name: str = "FISH_API_KEY") -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT,
             "-s", f"{KEYCHAIN_SERVICE_PREFIX}/{name}", "-w"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def has_key(name: str = "FISH_API_KEY") -> bool:
    """Presence check without -w: the key value never enters this process."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT,
             "-s", f"{KEYCHAIN_SERVICE_PREFIX}/{name}"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def save_key(name: str, value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Key must be nonempty and contain no newline.")
    service = f"{KEYCHAIN_SERVICE_PREFIX}/{name}".replace("\\", "\\\\").replace('"', '\\"')
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    command = (f'add-generic-password -U -a {KEYCHAIN_ACCOUNT} -s "{service}" '
               f'-w "{escaped_value}"\n')
    try:
        result = subprocess.run(
            ["security", "-i"], input=command,
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Could not save key in Keychain.") from None
    if result.returncode != 0:
        raise RuntimeError("Could not save key in Keychain.") from None


def delete_key(name: str) -> None:
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT,
             "-s", f"{KEYCHAIN_SERVICE_PREFIX}/{name}"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Could not delete key from Keychain.") from None
    if result.returncode not in (0, 44):
        raise RuntimeError("Could not delete key from Keychain.") from None


def migrate_legacy() -> bool:
    if load_key() is not None:
        return False
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "ai-discord-voice/FISH_API_KEY", "-w"],  # publish-allow
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    value = result.stdout.strip()
    if not value:
        return False
    try:
        save_key("FISH_API_KEY", value)
    except (ValueError, RuntimeError):
        return False
    return True
