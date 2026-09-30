import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fb_collector import db, runner
from fb_collector.services.account_status import record_status


class DatabaseResilienceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'test.db'
        p = patch.object(db, 'DB_PATH', self.path)
        p.start()
        self.addCleanup(p.stop)
        db.init_db()

    def test_external_reader_does_not_block_commit(self):
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM browser_accounts').fetchall()
        with ThreadPoolExecutor(max_workers=1) as pool:
            account_id = pool.submit(db.create_browser_account, 'new').result(timeout=3)
        reader.rollback()
        self.assertEqual(db.get_browser_account(account_id)['name'], 'new')

    def test_concurrent_status_updates_and_reads(self):
        account_id = db.create_browser_account('test')
        def update(number):
            record_status(account_id, 'logged_in')
            return db.list_browser_accounts()[0]['id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(update, range(50))), [account_id] * 50)

    def test_status_database_failure_is_nonfatal(self):
        with patch.object(db, 'update_browser_account', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertLogs('fb_collector.services.account_status', level='ERROR'):
                record_status(1, 'logged_in')

    def test_task_failure_and_failed_persistence_still_end_live_state(self):
        project = {'id': 1, 'name': 'test', 'project_type': 'post', 'browser_accounts': [{'id': 77}]}
        run_id = 900001
        with patch.object(db, 'get_project', return_value=project), patch.object(
            runner, 'run_post_project', side_effect=sqlite3.OperationalError('database is locked')
        ), patch.object(db, 'finish_task_run', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertLogs('fb_collector.runner', level='ERROR'):
                runner.run_project(1, run_id=run_id)
        self.assertEqual(runner.RUNNING[run_id]['status'], 'failed')
        self.assertEqual(runner.RUNNING[run_id]['logs'][-1]['status'], 'failed')
        self.assertFalse(runner.PROFILE_LOCKS['browser_account|77'])
        runner.RUNNING.pop(run_id)


if __name__ == '__main__':
    unittest.main()
