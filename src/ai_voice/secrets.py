"""Manage project keys without exposing values in process arguments or output."""
import subprocess
import sys

KEYCHAIN_SERVICE_PREFIX = "ai-voice"
KEYCHAIN_ACCOUNT = "default"


def _credential_api():
    import ctypes
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(Credential)
    api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api, Credential


def load_key(name: str = "FISH_API_KEY") -> str | None:
    if sys.platform == "win32":
        import ctypes
        api, credential_type = _credential_api()
        credential = ctypes.POINTER(credential_type)()
        if not api.CredReadW(f"{KEYCHAIN_SERVICE_PREFIX}/{name}", 1, 0, ctypes.byref(credential)):
            return None
        try:
            value = ctypes.string_at(credential.contents.CredentialBlob,
                                     credential.contents.CredentialBlobSize)
            return value.decode("utf-16-le") or None
        finally:
            api.CredFree(credential)
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
    if sys.platform == "win32":
        import ctypes
        api, credential_type = _credential_api()
        credential = ctypes.POINTER(credential_type)()
        if not api.CredReadW(f"{KEYCHAIN_SERVICE_PREFIX}/{name}", 1, 0, ctypes.byref(credential)):
            return False
        api.CredFree(credential)
        return True
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
    if sys.platform == "win32":
        import ctypes
        api, credential_type = _credential_api()
        blob = value.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob)
        credential = credential_type()
        credential.Type = 1  # CRED_TYPE_GENERIC
        credential.TargetName = f"{KEYCHAIN_SERVICE_PREFIX}/{name}"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = KEYCHAIN_ACCOUNT
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise RuntimeError("Could not save key in Credential Manager.")
        return
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
    if sys.platform == "win32":
        import ctypes
        api, _ = _credential_api()
        if not api.CredDeleteW(f"{KEYCHAIN_SERVICE_PREFIX}/{name}", 1, 0):
            if ctypes.get_last_error() != 1168:  # ERROR_NOT_FOUND
                raise RuntimeError("Could not delete key from Credential Manager.")
        return
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
    if sys.platform == "win32":
        return False
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
