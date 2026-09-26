from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from src.main.python.sheng_wen.workbench.credential_store import CredentialStore, dpapi
from src.main.python.sheng_wen.workbench.connections import Connection
from src.main.python.sheng_wen.workbench.service import migrate_legacy_connection
from src.main.python.sheng_wen.workbench.paths import default_export_dir
from src.main.python.sheng_wen.workbench.models import _sha256_stream


class FakeKeyring:
    def __init__(self):
        self.values = {}
        self.locked = False

    def get_password(self, service, account):
        if self.locked:
            raise RuntimeError("locked")
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        if self.locked:
            raise RuntimeError("locked")
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class FakeConfig:
    def __init__(self):
        self.data = {
            "whisper": {
                "bilibili_sessdata": "FAKE_LEGACY_SECRET",
                "bilibili_cookie_string": "",
            }
        }
        self.fail_write = False

    def get_raw_config(self):
        return self.data

    def update_section(self, section, patch):
        if self.fail_write:
            raise OSError("read only")
        self.data[section].update(patch)


class CrossPlatformCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "credentials.json"

    def connection(self, platform="linux", environment=None, backend=None):
        store = CredentialStore(
            self.path,
            platform=platform,
            environ=environment or {},
            keyring_factory=lambda *_: backend,
        )
        return Connection(self.path, storage=store)

    def test_macos_keychain_roundtrip_without_secret_in_file(self):
        backend = FakeKeyring()
        first = self.connection("darwin", backend=backend)
        status = first.import_value("FAKE_MAC_SECRET")
        self.assertEqual(status["storage_mode"], "macos_keychain")
        self.assertTrue(status["storage_persistent"])
        self.assertNotIn("FAKE_MAC_SECRET", self.path.read_text())
        self.assertEqual(
            self.connection("darwin", backend=backend).cookies()["SESSDATA"],
            "FAKE_MAC_SECRET",
        )

    def test_linux_secret_service_roundtrip(self):
        backend = FakeKeyring()
        first = self.connection("linux", backend=backend)
        first.import_value("FAKE_LINUX_SECRET")
        self.assertEqual(first.status()["storage_mode"], "linux_secret_service")
        self.assertEqual(
            self.connection("linux", backend=backend).cookies()["SESSDATA"],
            "FAKE_LINUX_SECRET",
        )

    def test_headless_linux_session_import_does_not_fail_or_write_plaintext(self):
        first = self.connection()
        result = first.import_value("FAKE_SESSION_SECRET")
        self.assertFalse(result["storage_persistent"])
        self.assertEqual(result["storage_mode"], "session")
        self.assertTrue(result["storage_warning"])
        self.assertFalse(self.path.exists())
        self.assertEqual(first.cookies()["SESSDATA"], "FAKE_SESSION_SECRET")

    def test_missing_native_backend_falls_back_without_error(self):
        def unavailable(*_):
            raise ImportError("keyring unavailable")

        store = CredentialStore(
            self.path, platform="darwin", environ={}, keyring_factory=unavailable
        )
        self.assertEqual(store.mode, "session")
        self.assertFalse(
            Connection(self.path, storage=store).import_value("FAKE")[
                "storage_persistent"
            ]
        )

    def test_encrypted_file_survives_restart_with_same_key(self):
        environment = {"SHENGWEN_SECRET_KEY": Fernet.generate_key().decode()}
        first = self.connection(environment=environment)
        self.assertTrue(first.import_value("FAKE_SERVER_SECRET")["storage_persistent"])
        self.assertNotIn(b"FAKE_SERVER_SECRET", self.path.read_bytes())
        self.assertEqual(
            self.connection(environment=environment).cookies()["SESSDATA"],
            "FAKE_SERVER_SECRET",
        )

    def test_wrong_key_preserves_file_and_does_not_crash_startup(self):
        first_key = {"SHENGWEN_SECRET_KEY": Fernet.generate_key().decode()}
        self.connection(environment=first_key).import_value("FAKE_SERVER_SECRET")
        original = self.path.read_bytes()
        other = self.connection(
            environment={"SHENGWEN_SECRET_KEY": Fernet.generate_key().decode()}
        )
        self.assertTrue(other.status()["storage_warning"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(other.data["cookies"])

    def test_explicit_reimport_preserves_unreadable_encrypted_original(self):
        self.path.write_bytes(b"foreign-windows-credential")
        env = {"SHENGWEN_SECRET_KEY": Fernet.generate_key().decode()}
        connection = self.connection(environment=env)
        connection.import_value("NEW_FAKE_VALUE")
        backups = list(self.path.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"foreign-windows-credential")

    def test_foreign_windows_blob_is_not_deleted_on_unix(self):
        self.path.write_bytes(b"foreign-windows-credential")
        connection = self.connection("linux")
        self.assertTrue(connection.status()["storage_warning"])
        connection.import_value("SESSION_FAKE")
        self.assertEqual(self.path.read_bytes(), b"foreign-windows-credential")

    def test_env_verification_never_attempts_secret_storage(self):
        connection = self.connection()
        with (
            patch.dict(os.environ, {"BILIBILI_SESSDATA": "FAKE_ENV_SECRET"}),
            patch(
                "bilibili_api.Credential.check_valid",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "bilibili_api.user.get_self_info",
                new_callable=AsyncMock,
                return_value={"name": "Test", "mid": 123},
            ),
            patch.object(connection.storage, "save") as save,
        ):
            result = asyncio.run(connection.verify())
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["source"], "env")
        save.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_session_connection_verification_remains_available(self):
        connection = self.connection()
        connection.import_value("FAKE_SESSION_SECRET")
        with (
            patch(
                "bilibili_api.Credential.check_valid",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "bilibili_api.user.get_self_info",
                new_callable=AsyncMock,
                return_value={"name": "Test", "mid": 123},
            ),
        ):
            self.assertEqual(asyncio.run(connection.verify())["status"], "connected")
        self.assertFalse(self.path.exists())

    def test_disconnect_persists_on_headless_without_encryption_key(self):
        connection = self.connection()
        connection.import_value("FAKE_SESSION_SECRET")
        connection.disconnect()
        with patch.dict(os.environ, {"BILIBILI_SESSDATA": "FAKE_ENV_SECRET"}):
            restarted = self.connection()
            self.assertFalse(restarted.cookies())
            self.assertTrue(restarted.data["user_disconnected"])
        self.assertNotIn("SECRET", self.path.read_text())

    def test_disconnect_is_not_undone_by_legacy_migration(self):
        connection = self.connection()
        connection.disconnect()
        cfg = FakeConfig()
        migrate_legacy_connection(
            cfg, self.connection(), SimpleNamespace(_bilibili_sessdata="old")
        )
        self.assertEqual(cfg.data["whisper"]["bilibili_sessdata"], "")
        self.assertFalse(self.connection().cookies())

    def test_unavailable_storage_does_not_destroy_legacy_configuration(self):
        cfg = FakeConfig()
        connection = self.connection()
        migrate_legacy_connection(
            cfg, connection, SimpleNamespace(_bilibili_sessdata="old")
        )
        self.assertEqual(cfg.data["whisper"]["bilibili_sessdata"], "FAKE_LEGACY_SECRET")
        self.assertEqual(connection.cookies()["SESSDATA"], "FAKE_LEGACY_SECRET")

    def test_successful_secure_migration_clears_legacy_configuration(self):
        cfg = FakeConfig()
        connection = self.connection(backend=FakeKeyring())
        migrate_legacy_connection(
            cfg, connection, SimpleNamespace(_bilibili_sessdata="old")
        )
        self.assertEqual(cfg.data["whisper"]["bilibili_sessdata"], "")
        self.assertTrue(connection.persisted)

    def test_locked_keyring_does_not_clear_old_config(self):
        backend = FakeKeyring()
        backend.locked = True
        cfg = FakeConfig()
        connection = self.connection(backend=backend)
        migrate_legacy_connection(
            cfg, connection, SimpleNamespace(_bilibili_sessdata="old")
        )
        self.assertEqual(cfg.data["whisper"]["bilibili_sessdata"], "FAKE_LEGACY_SECRET")
        self.assertFalse(connection.persisted)

    def test_legacy_config_write_failure_does_not_stop_startup(self):
        cfg = FakeConfig()
        cfg.fail_write = True
        connection = self.connection(backend=FakeKeyring())
        migrate_legacy_connection(
            cfg, connection, SimpleNamespace(_bilibili_sessdata="old")
        )
        self.assertTrue(connection.persisted)
        self.assertTrue(connection.storage.warning)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows DPAPI compatibility")
    def test_existing_windows_dpapi_remains_readable(self):
        data = {
            "cookies": {"SESSDATA": "FAKE_OLD_WINDOWS_SECRET"},
            "status": "unverified",
            "use_env": True,
        }
        self.path.write_bytes(dpapi(json.dumps(data).encode()))
        connection = self.connection("win32")
        self.assertEqual(connection.cookies()["SESSDATA"], "FAKE_OLD_WINDOWS_SECRET")
        self.assertTrue(connection.persisted)

    def test_default_export_directory_uses_user_home(self):
        with patch("pathlib.Path.home", return_value=Path(self.temp.name)):
            self.assertEqual(
                default_export_dir(),
                str(Path(self.temp.name) / "Documents" / "ShengWen" / "exports"),
            )

    def test_model_hash_works_without_python311_file_digest(self):
        self.assertEqual(
            _sha256_stream(io.BytesIO(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


if __name__ == "__main__":
    unittest.main()
