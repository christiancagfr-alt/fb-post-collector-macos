import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from google.auth.exceptions import RefreshError
from fb_collector import db
from fb_collector.app import create_app
from fb_collector.services import drive_storage as drive
from fb_collector.services.image_storage import preview_formula
from fb_collector.services.errors import UploadError, LoginRequiredError
from fb_collector.services.scraper import FacebookScraper, facebook_logged_in


class DriveStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(db, 'DB_PATH', Path(self.temp.name) / 'test.sqlite')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.image = Path(self.temp.name) / 'frame.png'
        self.image.write_bytes(b'image bytes')
        drive._cursor = 0
        with db.connect() as conn:
            for number in (1, 2):
                conn.execute("INSERT INTO drive_accounts(identity,email,name,token,folder_id) VALUES(?,?,?,?,?)",
                             (str(number), f'user{number}@example.com', f'User {number}', 'SECRET_TOKEN', f'folder{number}'))

    def service(self, capacity=1000, used=0):
        service = MagicMock()
        service.about().get().execute.return_value = {'user': {'displayName': 'User'}, 'storageQuota': {'limit': str(capacity), 'usage': str(used)}}
        service.files().create().execute.return_value = {'id': 'file123', 'webViewLink': 'https://drive.google.com/file/d/file123/view'}
        service.reset_mock()
        return service

    def test_full_account_switches_without_uploading_and_next_call_rotates(self):
        full, ready = self.service(10, 10), self.service()
        with patch.object(drive, '_service', side_effect=[full, ready]) as services:
            self.assertIn('/file123/view', drive.upload_file(self.image))
        full.files.assert_not_called()
        self.assertEqual([call.args[0]['id'] for call in services.call_args_list], [1, 2])
        self.assertEqual(drive.accounts()[0]['status'], 'full')

    def test_round_robin_uploads_and_private_by_default(self):
        service = self.service()
        with patch.object(drive, '_service', return_value=service) as services:
            drive.upload_file(self.image)
            drive.upload_file(self.image)
            drive.upload_file(self.image)
        self.assertEqual([call.args[0]['id'] for call in services.call_args_list], [1, 2, 1])
        service.permissions.assert_not_called()

    def test_expired_authorization_switches_to_next_account(self):
        with patch.object(drive, '_service', side_effect=[RefreshError('expired'), self.service()]):
            drive.upload_file(self.image)
        self.assertEqual(drive.accounts()[0]['status'], 'auth_error')

    def test_all_failed_is_reported_without_leaking_tokens(self):
        with patch.object(drive, '_service', side_effect=RefreshError('SECRET_TOKEN')):
            with self.assertRaises(UploadError) as error:
                drive.upload_file(self.image)
        self.assertNotIn('SECRET_TOKEN', str(error.exception))
        self.assertIn('所有 Google Drive', error.exception.user_message)

    def test_public_share_failure_preserves_uploaded_link_no_duplicate(self):
        db.setting_set('drive_public_links', '1')
        service = self.service()
        service.permissions().create().execute.side_effect = RuntimeError('policy')
        with patch.object(drive, '_service', return_value=service) as services:
            self.assertIn('/file123/view', drive.upload_file(self.image))
        self.assertEqual(services.call_count, 1)
        self.assertIn('公开共享失败', drive.accounts()[0]['detail'])

    def test_status_api_hides_tokens_and_existing_sheets_authorization_survives(self):
        db.setting_set('google_token_json', 'EXISTING_SHEETS_TOKEN')
        response = self.client.get('/drive-accounts/status')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'SECRET_TOKEN', response.data)
        response = self.client.get('/drive-accounts')
        self.assertEqual(response.status_code, 200)
        self.client.post('/drive-accounts', data={'provider': 'drive'})
        self.assertEqual(db.setting_get('google_token_json'), 'EXISTING_SHEETS_TOKEN')
        self.assertEqual(preview_formula('https://drive.google.com/file/d/test/view'), '=IMAGE("https://drive.google.com/thumbnail?id=test&sz=w1000")')

    def test_drive_preview_url_formats_preserve_file_id(self):
        for url in ('https://drive.google.com/file/d/abc_123-xyz/view?usp=sharing',
                    'https://drive.google.com/open?id=abc_123-xyz',
                    'https://drive.google.com/thumbnail?id=abc_123-xyz'):
            self.assertEqual(preview_formula(url), '=IMAGE("https://drive.google.com/thumbnail?id=abc_123-xyz&sz=w1000")')
        self.assertEqual(preview_formula('https://drive.google.com/'), '')
        self.assertEqual(preview_formula('https://example.com/photo.png'), '=IMAGE("https://example.com/photo.png")')

    def test_login_saves_profile_quota_and_reauthorization_updates_same_account(self):
        service = self.service()
        service.about().get().execute.return_value = {
            'user': {'permissionId': '1', 'emailAddress': 'user1@example.com', 'displayName': 'Profile', 'photoLink': 'https://example.com/avatar'},
            'storageQuota': {'limit': '10000', 'usage': '500'},
        }
        credentials = MagicMock()
        credentials.refresh_token = 'refresh'
        credentials.to_json.return_value = 'NEW_TOKEN'
        flow = MagicMock()
        flow.run_local_server.return_value = credentials
        client_path = Path(self.temp.name) / 'client.json'
        drive.write_secret_file(client_path, json.dumps({'installed': {'client_id': 'test', 'client_secret': 'test'}}))
        with patch.object(drive.InstalledAppFlow, 'from_client_config', return_value=flow), patch.object(drive, '_build_service', return_value=service):
            drive._login_worker(str(client_path))
        self.assertEqual(drive.login_status()['status'], 'success')
        self.assertEqual(len(drive.accounts()), 2)
        profile = drive.accounts()[0]
        self.assertEqual(profile['name'], 'Profile')
        self.assertEqual(profile['used'], 500)
        self.assertEqual(profile['capacity'], 10000)
        self.assertNotIn('token', profile)

    def test_cloud_upload_failure_not_swallowed_as_ocr_error(self):
        scraper = FacebookScraper()
        with patch('fb_collector.services.scraper.download_temp_file', return_value=self.image), patch.object(scraper, 'apply_ocr_to_local_media'), patch('fb_collector.services.scraper.upload_file', side_effect=UploadError('云盘失败')):
            with self.assertRaises(UploadError):
                scraper.process_image_media('https://example.com/image.png', {}, {}, [])

    def test_login_page_overrides_stale_cookie_and_failure_updates_account(self):
        driver = MagicMock()
        driver.current_url = 'https://www.facebook.com/login/'
        driver.get_cookies.return_value = [{'name': 'c_user', 'domain': '.facebook.com', 'value': '123'}]
        self.assertFalse(facebook_logged_in(driver))
        account_id = db.create_browser_account('test')
        with patch.object(FacebookScraper, '_scrape', side_effect=LoginRequiredError()):
            with self.assertRaises(LoginRequiredError):
                FacebookScraper().scrape('https://facebook.com/test', {'browser_account_id': account_id})
        self.assertEqual(db.get_browser_account(account_id)['facebook_login_status'], 'not_logged_in')


if __name__ == '__main__':
    unittest.main()
