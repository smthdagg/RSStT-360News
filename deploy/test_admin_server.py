import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from admin_server import AdminState, UserStore, hash_password, verify_password


class AdminSecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        encoded = hash_password("correct horse")
        self.assertNotEqual(encoded, hash_password("correct horse"))
        self.assertTrue(verify_password("correct horse", encoded))
        self.assertFalse(verify_password("wrong", encoded))

    def test_state_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.mkdir()
            (config / ".env").write_text("TOKEN=secret\nMANAGER=123\n", encoding="utf-8")
            state = AdminState(config)
            public = state.snapshot()
            self.assertEqual(public["configured"], True)
            self.assertNotIn("secret", json.dumps(public))
            self.assertEqual(public["config_dir"], str(config))

    def test_user_store_add_disable_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UserStore(Path(tmp) / "users.json", "admin", hash_password("root"))
            self.assertTrue(store.authenticate("admin", "root")["is_admin"])
            store.add("reader", "read-pass-1", "operator")
            self.assertIsNotNone(store.authenticate("reader", "read-pass-1"))
            store.set_active("reader", False)
            self.assertIsNone(store.authenticate("reader", "read-pass-1"))
            store.remove("reader")
            self.assertNotIn("reader", [u["username"] for u in store.list_public()])


if __name__ == "__main__":
    unittest.main()
