import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fb_collector import db
from fb_collector.services import sheets


class ProjectCopyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "collector.sqlite3"
        self.db_patch = mock.patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_copy_project_copies_fields_and_resets_resume(self):
        project_id = db.create_project("原始项目", "post")
        db.update_project(
            project_id,
            {
                "spreadsheet_id": "sheet-1",
                "worksheet_name": "Sheet1",
                "link_column": "A",
                "resume_after_row": 12,
            },
        )
        db.update_project_fields(
            project_id,
            [
                {"field_key": "post_url", "enabled": True, "field_label": "贴文链接", "write_column": "B"},
            ],
        )
        new_id = db.copy_project(project_id)
        copied = db.get_project(new_id)
        source = db.get_project(project_id)
        self.assertEqual(copied["name"], "原始项目 副本")
        self.assertEqual(copied["spreadsheet_id"], "sheet-1")
        self.assertEqual(copied["worksheet_name"], "Sheet1")
        self.assertEqual(copied["resume_after_row"], 0)
        self.assertEqual(source["resume_after_row"], 12)
        copied_url = next(field for field in copied["fields"] if field["field_key"] == "post_url")
        self.assertEqual(copied_url["write_column"], "B")


class GoogleCredentialInspectTests(unittest.TestCase):
    def test_inspect_service_account_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sa.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "service_account",
                        "client_email": "bot@example.iam.gserviceaccount.com",
                        "private_key": "x",
                    }
                ),
                encoding="utf-8",
            )
            info = sheets.inspect_credentials_file(path)
            self.assertEqual(info["type"], "service_account")
            self.assertEqual(info["email"], "bot@example.iam.gserviceaccount.com")


if __name__ == "__main__":
    unittest.main()
