import unittest
from unittest.mock import patch
from fb_collector.services import component_installer, update_checker, browser_profiles


class MacSupportTests(unittest.TestCase):
    def test_missing_homebrew_is_actionable(self):
        with patch('sys.platform', 'darwin'), patch('shutil.which', return_value=None):
            result = component_installer.install_one('ffmpeg')
        self.assertFalse(result['success'])
        self.assertIn('https://brew.sh', result['output'])

    def test_download_matches_platform_and_arch(self):
        assets = [{'name': name, 'browser_download_url': 'https://github.com/christiancagfr-alt/fb-post-collector-macos/releases/download/v1/' + name}
                  for name in ['Setup.exe', 'macos-arm64.dmg', 'macos-x86_64.dmg']]
        with patch('sys.platform', 'darwin'), patch('platform.machine', return_value='arm64'):
            self.assertEqual([a['name'] for a in update_checker.preferred_assets(assets)], ['macos-arm64.dmg'])

    def test_mac_profile_location(self):
        with patch('sys.platform', 'darwin'):
            profiles = browser_profiles.chrome_user_data_dirs()
        self.assertIn('Application Support', str(profiles[0][1]))

    def test_mac_never_launches_windows_update(self):
        with patch('sys.platform', 'darwin'):
            with self.assertRaisesRegex(RuntimeError, 'DMG'):
                update_checker.start_auto_update()
