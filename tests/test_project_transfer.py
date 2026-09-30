import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fb_collector import db
from fb_collector.app import create_app
from fb_collector.project_transfer import export_project


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "test.sqlite3")
        self.patcher.start()
        self.client = create_app().test_client()
        self.project_id = db.create_project("共享配置", "post")

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_round_trip_creates_new_project_without_credentials(self):
        db.update_project(self.project_id, {"start_row": 1301, "end_row": 1400, "resume_after_row": 1350, "browser_profile_path": "secret-path"})
        response = self.client.get(f"/projects/{self.project_id}/export")
        payload = response.get_json()
        self.assertNotIn("secret-path", response.get_data(as_text=True))
        self.assertNotIn("resume_after_row", payload["project"])
        response = self.client.post("/projects/import", data={"project_file": (io.BytesIO(json.dumps(payload).encode()), "project.json")})
        self.assertEqual(response.status_code, 302)
        new_id = int(response.location.rsplit("/", 1)[-1])
        self.assertNotEqual(new_id, self.project_id)
        project = db.get_project(new_id)
        self.assertEqual(project["start_row"], 1301)
        self.assertEqual(project["end_row"], 1400)
        self.assertEqual(project["resume_after_row"], 0)
        self.assertFalse(project["browser_account_id"])
        self.assertEqual(len(project["fields"]), len(payload["fields"]))

    def test_malformed_import_does_not_create_project(self):
        before = len(db.list_projects())
        self.client.post("/projects/import", data={"project_file": (io.BytesIO(b'{"format":"wrong"}'), "bad.json")})
        self.assertEqual(len(db.list_projects()), before)

    def test_export_whitelist(self):
        payload = export_project({"name": "demo", "api_key": "secret", "browser_account": {"token": "secret"}})
        self.assertNotIn("secret", json.dumps(payload))
