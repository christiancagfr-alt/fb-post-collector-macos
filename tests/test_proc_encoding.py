import os
import sys
import unittest
from unittest.mock import patch

from fb_collector.services.proc import hidden_popen_kwargs, run_hidden


class ProcessEncodingTests(unittest.TestCase):
    def test_overrides_inherited_gbk_without_changing_parent(self):
        with patch.dict(os.environ, {"PYTHONIOENCODING": "gbk"}):
            env = hidden_popen_kwargs()["env"]
            self.assertEqual(env["PYTHONUTF8"], "1")
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(os.environ["PYTHONIOENCODING"], "gbk")

    def test_python_help_characters_round_trip(self):
        result = run_hidden([sys.executable, "-c", "print(chr(0xbf))"],
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "¿")
