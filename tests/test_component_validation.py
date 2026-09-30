import subprocess
import sys
import unittest
from unittest.mock import patch

from fb_collector.services import component_installer as installer
from fb_collector.services import environment


class ComponentValidationTests(unittest.TestCase):
    def test_nonzero_exit_is_unavailable(self):
        with patch.object(environment, "run_hidden", return_value=subprocess.CompletedProcess([], 1, "", "broken")):
            self.assertFalse(environment.command_available(sys.executable)["available"])

    def test_timeout_is_unavailable(self):
        with patch.object(environment, "run_hidden", side_effect=subprocess.TimeoutExpired("test", 10)):
            self.assertFalse(environment.command_available(sys.executable)["available"])

    def test_whisper_uses_help(self):
        with patch.object(environment.Path, "exists", return_value=True), \
             patch.object(environment, "run_hidden", return_value=subprocess.CompletedProcess([], 0, "usage: whisper", "")) as run:
            self.assertTrue(environment.command_available("whisper.exe")["available"])
            self.assertEqual(run.call_args.args[0][-1], "--help")

    def test_silent_command_has_real_timeout(self):
        with patch.object(installer, "log_install"):
            with self.assertRaises(subprocess.TimeoutExpired):
                installer.run_command_logged([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.3)

    def test_tesseract_official_asset(self):
        if sys.platform == "darwin":
            self.assertEqual(installer.TESSERACT_MANUAL_LINKS[0]["url"], "https://brew.sh")
            return
        self.assertEqual(installer.TESSERACT_MANUAL_LINKS[0]["url"], installer.TESSERACT_SETUP_URLS[0])
        self.assertIn("github.com/tesseract-ocr/tesseract/releases/download/5.5.3/", installer.TESSERACT_SETUP_URLS[0])
