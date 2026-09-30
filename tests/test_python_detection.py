import subprocess
import unittest
from unittest.mock import patch

from fb_collector.services import component_installer as installer


class PythonDetectionTests(unittest.TestCase):
    def test_store_alias_is_not_a_candidate(self):
        with patch.object(installer.sys, "frozen", True, create=True), \
             patch.object(installer.shutil, "which", return_value=r"C:\Users\test\AppData\Local\Microsoft\WindowsApps\python.exe"), \
             patch.object(installer.Path, "exists", return_value=False):
            self.assertEqual(installer.python_candidates(), [])

    def test_probe_rejects_stub_and_timeout(self):
        with patch.object(installer, "run_hidden", return_value=subprocess.CompletedProcess([], 9009, "Python was not found")):
            self.assertFalse(installer.usable_python(["python"]))
        with patch.object(installer, "run_hidden", side_effect=subprocess.TimeoutExpired("python", 15)):
            self.assertFalse(installer.usable_python(["python"]))

    def test_probe_accepts_valid_runtime(self):
        with patch.object(installer, "run_hidden", return_value=subprocess.CompletedProcess([], 0, "FB_PYTHON_OK\n")):
            self.assertTrue(installer.usable_python(["python"]))

    def test_invalid_candidate_does_not_hide_valid_one(self):
        with patch.object(installer, "python_candidates", return_value=["bad", "good"]), \
             patch.object(installer, "usable_python", side_effect=[False, True]), \
             patch.object(installer, "log_install"):
            self.assertEqual(installer.find_usable_python(), ["good"])

    def test_missing_python_installs_then_redetects(self):
        with patch.object(installer, "find_usable_python", side_effect=[None, ["new-python"]]), \
             patch.object(installer, "winget_install", return_value=subprocess.CompletedProcess([], 0)) as install, \
             patch.object(installer, "log_install"):
            self.assertEqual(installer.ensure_python(), ["new-python"])
            install.assert_called_once_with("Python.Python.3.12")

    def test_unavailable_winget_has_actionable_error(self):
        with patch.object(installer, "find_usable_python", return_value=None), \
             patch.object(installer, "winget_install", side_effect=RuntimeError("no winget")), \
             patch.object(installer, "download_file", side_effect=RuntimeError("offline")), \
             patch.object(installer, "log_install"):
            with self.assertRaisesRegex(RuntimeError, "python.org"):
                installer.ensure_python()

    def test_no_python_or_winget_uses_official_installer(self):
        with patch.object(installer, "find_usable_python", side_effect=[None, None, ["python312"]]), \
             patch.object(installer, "winget_install", side_effect=RuntimeError("missing")), \
             patch.object(installer, "download_file") as download, \
             patch.object(installer, "run_command_logged", return_value=subprocess.CompletedProcess([], 0, "")) as run, \
             patch.object(installer, "log_install"):
            self.assertEqual(installer.ensure_python(), ["python312"])
            self.assertEqual(download.call_args.args[0], installer.PYTHON_SETUP_URL)
            self.assertIn("InstallAllUsers=0", run.call_args.args[0])
            self.assertIn("Include_pip=1", run.call_args.args[0])
