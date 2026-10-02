"""Run tests with isolated app data and an in-memory (test-only) key vault."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[service, username] = password


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="fb-security-tests-") as folder:
        os.environ["FB_COLLECTOR_DATA_DIR"] = folder
        from fb_collector import credential_store
        with patch.object(credential_store, "_backend", return_value=MemoryVault()):
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(not result.wasSuccessful())
