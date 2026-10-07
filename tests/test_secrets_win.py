import ctypes
import sys
import uuid
from unittest.mock import Mock

import pytest

from ai_voice import secrets

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_secret_roundtrip_win(monkeypatch):
    name = f"TEST_{uuid.uuid4().hex}"
    api, credential_type = secrets._credential_api()
    entries = {}
    allocations = []

    def write(pointer, flags):
        credential = pointer._obj
        entries[credential.TargetName] = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize)
        assert credential.Type == 1
        assert credential.Persist == 2
        assert credential.UserName == "default"
        return 1

    def read(target, kind, flags, output):
        if target not in entries:
            return 0
        blob = ctypes.create_string_buffer(entries[target])
        credential = credential_type()
        credential.CredentialBlobSize = len(entries[target])
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        allocations.append((blob, credential))
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(credential_type)))[0] = ctypes.pointer(credential)
        return 1

    def delete(target, kind, flags):
        entries.pop(target, None)
        return 1

    fake = Mock(CredWriteW=Mock(side_effect=write), CredReadW=Mock(side_effect=read),
                CredDeleteW=Mock(side_effect=delete), CredFree=Mock())
    monkeypatch.setattr(secrets, "_credential_api", lambda: (fake, credential_type))
    try:
        secrets.save_key(name, "test-only-value-\u00e9")
        assert secrets.load_key(name) == "test-only-value-\u00e9"
        assert secrets.has_key(name)
        secrets.delete_key(name)
        assert secrets.load_key(name) is None
        assert not secrets.has_key(name)
        fake.CredFree.assert_called()
    finally:
        secrets.delete_key(name)


def test_migrate_legacy_noop_win(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Windows migration must not call anything")
    monkeypatch.setattr(secrets, "load_key", unexpected)
    monkeypatch.setattr(secrets, "save_key", unexpected)
    monkeypatch.setattr(secrets.subprocess, "run", unexpected)
    monkeypatch.setattr(secrets, "_credential_api", unexpected)
    assert secrets.migrate_legacy() is False


def test_credential_failures_win(monkeypatch):
    _, credential_type = secrets._credential_api()
    fake = Mock(CredWriteW=Mock(return_value=0), CredDeleteW=Mock(return_value=0))
    monkeypatch.setattr(secrets, "_credential_api", lambda: (fake, credential_type))
    with pytest.raises(RuntimeError, match="Could not save key"):
        secrets.save_key("TEST_FAILURE", "test-only-value")
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    with pytest.raises(RuntimeError, match="Could not delete key"):
        secrets.delete_key("TEST_FAILURE")
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1168)
    secrets.delete_key("TEST_FAILURE")


def test_credential_read_frees_on_decode_error_win(monkeypatch):
    _, credential_type = secrets._credential_api()
    blob = ctypes.create_string_buffer(b"x")
    credential = credential_type()
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.CredentialBlobSize = 1

    def read(target, kind, flags, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(credential_type)))[0] = ctypes.pointer(credential)
        return 1

    fake = Mock(CredReadW=Mock(side_effect=read), CredFree=Mock())
    monkeypatch.setattr(secrets, "_credential_api", lambda: (fake, credential_type))
    with pytest.raises(UnicodeDecodeError):
        secrets.load_key("TEST_INVALID")
    fake.CredFree.assert_called_once()
