import base64
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from fb_collector import db, post_lookup, runner
from fb_collector.app import create_app
from fb_collector.services.errors import SheetWriteError, UserVisibleError
from fb_collector.services.facebook_graphql import extract_story_fields, timeline_edges_and_page_info
from fb_collector.services.sheets import SheetsClient
from fb_collector.services.task_control import check_cancelled


def config():
    return {"id": 1, "project_type": "post_lookup", "spreadsheet_id": "book",
            "page_source_worksheet_name": "专页列表", "post_id_worksheet_name": "贴文id",
            "page_output_worksheet_name": "采集结果", "lookup_page_limit": 2,
            "write_start_column": "E", "fields": [
                {"field_key": "post_id", "field_label": "贴文ID", "enabled": 1, "write_column": "E"}]}


def story(post_id="122122129083377821", owner="123"):
    return {"post_id": post_id, "author_id": owner, "author_name": "测试专页", "post_text": "内容"}


class LookupIdentityTests(unittest.TestCase):
    def test_requires_exact_owner_and_id(self):
        self.assertEqual(post_lookup.verified_post_id(story(), "123"), "122122129083377821")
        self.assertEqual(post_lookup.verified_post_id(story(), "999"), "")
        self.assertEqual(post_lookup.verified_post_id(story("123_456"), "123"), "456")
        self.assertEqual(post_lookup.verified_post_id(story("999_456"), "123"), "")
        encoded = base64.b64encode(b"S:_I123:VK:456").decode()
        self.assertEqual(post_lookup.verified_post_id(story(encoded), "123"), "456")

    def test_exact_returned_permalink_no_substring_match(self):
        value = dict(story("opaque"), post_url="https://www.facebook.com/123/posts/456extra")
        self.assertEqual(post_lookup.verified_post_id(value, "123"), "")
        value["post_url"] = "https://facebook.com/123/posts/456/"
        self.assertEqual(post_lookup.verified_post_id(value, "123"), "456")
        value["post_url"] = "https://facebook.com.evil.test/123/posts/456"
        self.assertEqual(post_lookup.verified_post_id(value, "123"), "")

    def test_shared_video_id_is_not_outer_post_identity(self):
        value = dict(story("456"), lookup_post_id="", lookup_post_url="")
        self.assertEqual(post_lookup.verified_post_id(value, "123"), "")
        extracted = extract_story_fields({"actors": [{"id": "123"}], "tracking": json.dumps({"top_level_post_id": "789"}),
                                          "attached_story": {"post_id": "456", "url": "https://facebook.com/123/posts/456"}})
        self.assertEqual(post_lookup.verified_post_id(extracted, "123"), "789")

    def test_nested_timeline_pagination(self):
        info = {"end_cursor": "next", "has_next_page": True}
        _, actual = timeline_edges_and_page_info([{"data": {"node": {"timeline_list_feed_units": {"edges": [], "page_info": info}}}}])
        self.assertEqual(actual, info)

    def test_config_prevents_input_overwrite_and_reserved_columns(self):
        project = config()
        post_lookup.validate_lookup_config(project, project["fields"])
        project["page_output_worksheet_name"] = "贴文id"
        with self.assertRaises(ValueError):
            post_lookup.validate_lookup_config(project, project["fields"])
        project = config()
        project["fields"][0]["write_column"] = "D"
        with self.assertRaises(ValueError):
            post_lookup.validate_lookup_config(project, project["fields"])


class LookupExecutionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.project = config()
        self.counts = dict(total=0, success=0, failed=0, skipped=0)
        self.sheets = MagicMock()
        self.sheets.read_column.side_effect = lambda book, name, col, start: (
            [{"row_number": 2, "url": "122122129083377821"}, {"row_number": 3, "url": "122122129083377821"}]
            if name == "贴文id" else [{"row_number": 2, "url": "https://www.facebook.com/profile.php?id=123"}])
        self.sheets.read_range_rows.return_value = []
        self.scraper = MagicMock()
        self.scraper.fetch_profile_timeline.return_value = ([story()], {"has_next_page": False})
        self.scraper.enrich_prefetched.side_effect = lambda values, *args: {"status": "success", "values": values, "raw": {}}
        self.stack.enter_context(patch.object(post_lookup, "SheetsClient", return_value=self.sheets))
        self.stack.enter_context(patch.object(post_lookup, "FacebookScraper", return_value=self.scraper))
        for name in ("ensure_logged_in", "page_state", "cancellable_sleep", "close_driver"):
            self.stack.enter_context(patch.object(post_lookup, name))
        self.driver_factory = self.stack.enter_context(patch.object(post_lookup, "make_driver", return_value=MagicMock()))
        self.stack.enter_context(patch.object(runner, "note_progress"))
        self.stop = self.stack.enter_context(patch.object(runner, "should_stop", return_value=False))
        self.pause = self.stack.enter_context(patch.object(runner, "wait_until_runnable", return_value=True))
        self.accounts = self.stack.enter_context(patch.object(runner, "project_for_account", side_effect=lambda project, index: project))
        self.persist = self.stack.enter_context(patch.object(db, "add_row_run"))

    def run_lookup(self):
        post_lookup.run_lookup_project(self.project, 999, self.counts)

    def last_values(self):
        return dict(self.sheets.write_mapped_values.call_args.args[-1])

    def test_success_long_ids_and_inputs_unchanged(self):
        self.run_lookup()
        self.assertEqual(self.counts, dict(total=1, success=1, failed=0, skipped=0))
        self.assertEqual(self.last_values()["A"], "'122122129083377821")
        self.assertEqual(self.last_values()["E"], "'122122129083377821")
        self.assertEqual(self.last_values()["D"], "成功")
        self.assertTrue(all(call.args[1] == "采集结果" for call in self.sheets.write_mapped_values.call_args_list))
        self.sheets.ensure_worksheet.assert_called_once_with("book", "采集结果", 5)

    def test_successful_ids_skipped_on_rerun(self):
        self.sheets.read_range_rows.return_value = [
            {"row_number": 1, "values": post_lookup.HEADERS + ["贴文ID"]},
            {"row_number": 2, "values": ["122122129083377821", "url", "名称", "成功"]}]
        self.run_lookup()
        self.assertEqual(self.counts["skipped"], 1)
        self.driver_factory.assert_not_called()

    def test_failed_record_retried_in_same_row(self):
        self.sheets.read_range_rows.return_value = [
            {"row_number": 1, "values": post_lookup.HEADERS + ["贴文ID"]},
            {"row_number": 9, "values": ["122122129083377821", "", "", "未找到"]}]
        self.run_lookup()
        self.assertEqual(self.sheets.write_mapped_values.call_args.args[2], 9)
        self.assertEqual(self.counts["success"], 1)

    def test_wrong_owner_is_not_a_match(self):
        self.scraper.fetch_profile_timeline.return_value = ([story(owner="999")], {"has_next_page": False})
        self.run_lookup()
        self.assertEqual(self.counts["success"], 0)
        self.assertTrue(self.last_values()["D"].startswith("未找到"))
        self.scraper.enrich_prefetched.assert_not_called()

    def test_page_failure_and_search_limit_not_reported_as_absent(self):
        self.scraper.fetch_profile_timeline.side_effect = RuntimeError("unavailable")
        self.run_lookup()
        self.assertTrue(self.last_values()["D"].startswith("未完成核实"))
        self.assertEqual(self.counts["failed"], 1)

    def test_limit_and_cursor_are_bounded(self):
        self.scraper.fetch_profile_timeline.side_effect = [
            ([story("5")], {"has_next_page": True, "end_cursor": "next"}),
            ([story("6")], {"has_next_page": True, "end_cursor": "next2"})]
        self.run_lookup()
        self.assertEqual(self.scraper.fetch_profile_timeline.call_count, 2)
        self.assertEqual(self.scraper.fetch_profile_timeline.call_args.args[-1], "next")
        self.assertTrue(self.last_values()["D"].startswith("未完成核实"))

    def test_accounts_rotate_by_candidate_page(self):
        self.sheets.read_column.side_effect = [
            [{"row_number": 2, "url": "122122129083377821"}],
            [{"row_number": 2, "url": "https://facebook.com/555"}, {"row_number": 3, "url": "https://facebook.com/123"}]]
        self.run_lookup()
        self.assertEqual([call.args[1] for call in self.accounts.call_args_list], [0, 1])
        self.assertEqual(self.counts["success"], 1)

    def test_stop_during_enrichment_does_not_write_row(self):
        def cancel(*args):
            self.stop.return_value = True
            check_cancelled()
        self.scraper.enrich_prefetched.side_effect = cancel
        self.run_lookup()
        self.assertEqual(self.sheets.write_mapped_values.call_count, 1)  # headers only
        self.persist.assert_not_called()
        check_cancelled()  # callback cleared after exit

    def test_pause_boundary_before_writing_result(self):
        def stopped_while_paused(*args):
            self.pause.return_value = False
            return {"status": "success", "values": args[0], "raw": {}}
        self.scraper.enrich_prefetched.side_effect = stopped_while_paused
        self.run_lookup()
        self.assertEqual(self.sheets.write_mapped_values.call_count, 1)

    def test_unrelated_output_sheet_is_never_overwritten(self):
        self.sheets.read_range_rows.return_value = [{"row_number": 1, "values": ["重要数据"]}]
        with self.assertRaises(UserVisibleError):
            self.run_lookup()
        self.sheets.write_mapped_values.assert_not_called()

    def test_sheet_write_failure_is_not_success(self):
        self.sheets.write_mapped_values.side_effect = [None, SheetWriteError()]
        with self.assertRaises(SheetWriteError):
            self.run_lookup()
        self.assertEqual(self.counts["success"], 0)

    def test_enrichment_error_keeps_verified_page_and_retriable_status(self):
        self.scraper.enrich_prefetched.side_effect = RuntimeError("broken")
        self.run_lookup()
        self.assertEqual(self.last_values()["C"], "测试专页")
        self.assertIn("采集失败", self.last_values()["D"])

    def test_invalid_id_rejected_before_output_changes(self):
        self.sheets.read_column.side_effect = [
            [{"row_number": 2, "url": "1.221E+17"}], [{"row_number": 2, "url": "https://facebook.com/123"}]]
        with self.assertRaises(UserVisibleError):
            self.run_lookup()
        self.sheets.ensure_worksheet.assert_not_called()


class LookupConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "test.db")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = create_app().test_client()

    def test_creation_render_copy_and_export_import(self):
        response = self.client.post("/projects/new", data={"name": "ID查找", "project_type": "post_lookup"})
        project_id = int(response.location.rsplit("/", 1)[-1])
        project = db.get_project(project_id)
        self.assertEqual(project["page_output_worksheet_name"], "采集结果")
        self.assertEqual(project["fields"][0]["write_column"], "E")
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertEqual(html.count('name="page_source_worksheet_name"'), 1)
        self.assertIn('name="post_id_worksheet_name"', html)
        copied = db.get_project(db.copy_project(project_id))
        self.assertEqual(copied["project_type"], "post_lookup")
        payload = self.client.get(f"/projects/{project_id}/export").get_json()
        response = self.client.post("/projects/import", data={"project_file": (io.BytesIO(json.dumps(payload).encode()), "project.json")})
        imported = db.get_project(int(response.location.rsplit("/", 1)[-1]))
        self.assertEqual(imported["post_id_worksheet_name"], "贴文id")
        self.assertEqual(imported["lookup_page_limit"], 100)

    def test_save_rejects_same_input_output(self):
        project_id = db.create_project("查找", "post_lookup")
        self.client.post(f"/projects/{project_id}", data={"page_output_worksheet_name": "贴文id", "fields_json": "[]"})
        self.assertEqual(db.get_project(project_id)["page_output_worksheet_name"], "采集结果")

    def test_existing_modes_keep_their_defaults(self):
        for mode in ("post", "page"):
            project_id = db.create_project("原项目", mode)
            self.assertEqual(db.get_project(project_id)["fields"][0]["write_column"], "B")
            self.assertEqual(self.client.get(f"/projects/{project_id}").status_code, 200)


class EnsureWorksheetTests(unittest.TestCase):
    def test_create_only_when_absent(self):
        client = SheetsClient(credentials_path="test-credentials.json", token_path="test-token.json")
        client.service = MagicMock()
        endpoint = client.service.spreadsheets.return_value
        endpoint.get.return_value.execute.return_value = {"sheets": []}
        client.ensure_worksheet("book", "采集结果", 31)
        properties = endpoint.batchUpdate.call_args.kwargs["body"]["requests"][0]["addSheet"]["properties"]
        self.assertEqual(properties["gridProperties"]["columnCount"], 31)
        endpoint.reset_mock()
        endpoint.get.return_value.execute.return_value = {"sheets": [{"properties": {
            "title": "采集结果", "sheetId": 5, "gridProperties": {"columnCount": 31}}}]}
        client.ensure_worksheet("book", "采集结果", 31)
        endpoint.batchUpdate.assert_not_called()
