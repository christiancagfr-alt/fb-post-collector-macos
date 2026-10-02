import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from fb_collector import credential_store as store, db, config
from fb_collector.services import drive_storage


class Vault:
    key = None

    def get_password(self, service, username):
        return self.key

    def set_password(self, service, username, value):
        self.key = value


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.vault = Vault()
        for patcher in (patch.object(store, "_backend", return_value=self.vault),
                        patch.object(db, "DB_PATH", self.path / "test.sqlite"),
                        patch.object(config, "TOKEN_DIR", self.path)):
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        drive_storage.init_storage()

    def test_authenticated_roundtrip_and_tamper(self):
        value = store.protect("test-only-secret-中文")
        self.assertNotIn("test-only-secret", value)
        self.assertEqual(store.reveal(value), "test-only-secret-中文")
        with self.assertRaises(RuntimeError):
            store.reveal(value[:-5] + "AAAAA")

    def test_missing_key_does_not_replace_existing_ciphertext(self):
        encrypted = store.protect("test-secret")
        self.vault.key = None
        with self.assertRaises(RuntimeError):
            store.reveal(encrypted)
        self.assertIsNone(self.vault.key)

    def test_database_writes_ciphertext(self):
        db.setting_set("groq_api_key", "test-secret")
        self.assertEqual(db.setting_get("groq_api_key"), "test-secret")
        with db.connect() as conn:
            raw = conn.execute("SELECT value FROM app_settings WHERE key='groq_api_key'").fetchone()[0]
        self.assertTrue(raw.startswith(store.PREFIX))
        self.assertNotIn("test-secret", raw)

    def test_gyazo_not_echoed_and_blank_preserves_key(self):
        from fb_collector.app import create_app
        db.setting_set("gyazo_access_token", "test-secret-not-for-browser")
        client = create_app().test_client()
        response = client.get("/settings")
        self.assertNotIn(b"test-secret-not-for-browser", response.data)
        client.post("/settings", data={"gyazo_access_token": ""})
        self.assertEqual(db.setting_get("gyazo_access_token"), "test-secret-not-for-browser")
        client.post("/settings", data={"clear_gyazo_access_token": "1"})
        self.assertEqual(db.setting_get("gyazo_access_token"), "")

    def seed_legacy(self):
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('groq_api_key','old-key')")
            conn.execute("INSERT INTO drive_accounts(identity,email,name,token) VALUES('id','test@example.com','test','old-token')")
        (self.path / "drive_client.json").write_text('{"installed":{}}', encoding="utf-8")

    def test_legacy_migration_is_idempotent(self):
        self.seed_legacy()
        store.migrate_credentials()
        first = (self.path / "drive_client.json").read_text(encoding="utf-8")
        self.assertTrue(first.startswith(store.PREFIX))
        self.assertEqual(store.read_secret_file(self.path / "drive_client.json"), '{"installed":{}}')
        self.assertEqual(db.setting_get("groq_api_key"), "old-key")
        self.assertEqual(drive_storage._account(1)["token"], "old-token")
        with db.connect() as conn:
            self.assertTrue(conn.execute("SELECT token FROM drive_accounts").fetchone()[0].startswith(store.PREFIX))
        store.migrate_credentials()
        self.assertEqual((self.path / "drive_client.json").read_text(encoding="utf-8"), first)

    def test_vault_failure_preserves_legacy_data(self):
        self.seed_legacy()
        with patch.object(store, "_backend", side_effect=RuntimeError("locked vault")):
            with self.assertRaises(RuntimeError):
                store.migrate_credentials()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT value FROM app_settings WHERE key='groq_api_key'").fetchone()[0], "old-key")
        self.assertEqual((self.path / "drive_client.json").read_text(encoding="utf-8"), '{"installed":{}}')

    def test_mixed_migration_missing_key_does_not_generate_replacement(self):
        self.seed_legacy()
        db.setting_set("gemini_api_key", "encrypted-secret")
        self.vault.key = None
        with self.assertRaises(RuntimeError):
            store.migrate_credentials()
        self.assertIsNone(self.vault.key)

    def test_atomic_file_write_preserves_previous_on_failure(self):
        path = self.path / "secret.json"
        path.write_text("original", encoding="utf-8")
        with patch.object(store.os, "replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                store.write_secret_file(path, "replacement")
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.path.glob(".credential-*")), [])
