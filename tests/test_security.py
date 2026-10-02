import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, request
from fb_collector.security import configure_local_security, safe_sheet_value
from fb_collector.services.image_storage import preview_formula
from fb_collector.services import media_download, update_checker
from fb_collector.services.sheets import trusted_google_config
from fb_collector.services import verified_download


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        configure_local_security(self.app)
        self.app.add_url_rule("/", view_func=lambda: request.get_data() or "ok", methods=["GET", "POST"])
        self.client = self.app.test_client()

    def test_cross_site_and_rebinding_rejected(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Referer": "https://evil.example/a"}):
            self.assertEqual(self.client.post("/", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/", base_url="http://evil.example").status_code, 403)
        self.assertEqual(self.client.get("/", environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code, 403)

    def test_same_origin_works_and_response_headers(self):
        response = self.client.post("/", headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_upload_size_limit(self):
        self.assertEqual(self.client.post("/", data=b"x" * (2 * 1024 * 1024 + 1)).status_code, 413)

    def test_session_key_not_hardcoded(self):
        other = Flask("other")
        with patch.dict("os.environ", {}, clear=True):
            configure_local_security(other)
        self.assertNotEqual(other.secret_key, self.app.secret_key)


class ExportAndDownloadSecurityTests(unittest.TestCase):
    def test_component_unknown_url_rejected_without_network(self):
        with patch.object(verified_download.urllib.request, "build_opener") as opener:
            with self.assertRaises(RuntimeError):
                verified_download.download_verified("https://evil.example/setup.exe", Path("unused.exe"))
            opener.assert_not_called()

    def test_component_hash_is_verified_before_replacing_file(self):
        url = "https://github.com/vendor/project/releases/download/v1/test.exe"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.exe"
            path.write_bytes(b"original")
            for body, accepted in ((b"tampered", False), (b"trusted", True)):
                response = io.BytesIO(body)
                response.headers = {"Content-Length": str(len(body))}
                opener = MagicMock()
                opener.open.return_value = response
                with patch.dict(verified_download.SHA256, {url: hashlib.sha256(b"trusted").hexdigest()}), patch.object(
                        verified_download.urllib.request, "build_opener", return_value=opener):
                    if accepted:
                        verified_download.download_verified(url, path)
                        self.assertEqual(path.read_bytes(), b"trusted")
                    else:
                        with self.assertRaises(RuntimeError):
                            verified_download.download_verified(url, path)
                        self.assertEqual(path.read_bytes(), b"original")
                self.assertEqual(list(Path(folder).glob("*.part")), [])

    def test_component_redirect_cannot_downgrade_tls(self):
        handler = verified_download.HTTPSRedirect()
        for target in ("http://github.com/file", "file:///tmp/file", "https://github.com.evil.example/file"):
            with self.assertRaises(RuntimeError):
                handler.redirect_request(None, None, 302, "", {}, target)

    def test_formulas_escaped_except_trusted_image(self):
        for text in ('=IMPORTXML("https://evil.example", "//a")', '+cmd', '@SUM(A1)', '\n=1+1'):
            self.assertEqual(safe_sheet_value(text), "'" + text)
        self.assertEqual(safe_sheet_value("-12"), "-12")
        formula = preview_formula("https://drive.google.com/file/d/abc_123/view")
        self.assertTrue(safe_sheet_value(formula).startswith('=IMAGE("https://drive.google.com/thumbnail?'))
        self.assertEqual(preview_formula('https://example.com/a"&SUM(A1)&"'), "")
        self.assertEqual(preview_formula("https://example.com/a\nb"), "")

    def test_media_url_boundaries(self):
        for url in ("http://127.0.0.1/x", "file:///etc/passwd", "https://fbcdn.net.evil.example/x",
                    "https://evilfbcdn.net/x", "https://user:pass@fbcdn.net/x", "https://fbcdn.net:22/x"):
            with self.assertRaises(ValueError):
                media_download.validate_media_url(url)
        media_download.validate_media_url("https://scontent.xx.fbcdn.net/image.jpg")

    def test_media_redirect_cannot_reach_loopback(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = True
        response.headers = {"Location": "http://127.0.0.1:5199/settings"}
        with patch.object(media_download.requests, "get", return_value=response) as get:
            with self.assertRaises(ValueError):
                media_download.download_media("https://fbcdn.net/x", 10)
        self.assertEqual(get.call_count, 1)

    def test_media_stream_limit(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = False
        response.headers = {}
        response.iter_content.return_value = [b"123", b"456"]
        with patch.object(media_download.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                media_download.download_media("https://fbcdn.net/x", 5)

    def test_google_import_cannot_override_endpoints(self):
        result = trusted_google_config({"installed": {"token_uri": "https://evil.example"}})
        self.assertEqual(result["installed"]["token_uri"], "https://oauth2.googleapis.com/token")
        result = trusted_google_config({"type": "service_account", "token_uri": "http://127.0.0.1"})
        self.assertEqual(result["token_uri"], "https://oauth2.googleapis.com/token")

    def test_update_asset_boundaries(self):
        base = "https://github.com/christiancagfr-alt/fb-post-collector-macos/releases/download/v1/"
        self.assertTrue(update_checker.valid_release_asset("setup.exe", base + "setup.exe"))
        for name, url in (("../setup.exe", base + "../setup.exe"),
                          ("setup.exe", base.replace("christiancagfr-alt", "evil") + "setup.exe"),
                          ("setup.exe", base + "other.exe")):
            self.assertFalse(update_checker.valid_release_asset(name, url))

    def test_update_digest_failure_keeps_old_file_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "setup.exe"
            dest.write_bytes(b"old")
            response = io.BytesIO(b"tampered")
            response.headers = {}
            with patch.object(update_checker.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(RuntimeError):
                    update_checker._download_installer(
                        "https://github.com/christiancagfr-alt/fb-post-collector-macos/releases/download/v1/setup.exe",
                        dest, "installer", "sha256:" + hashlib.sha256(b"expected").hexdigest())
            self.assertEqual(dest.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).glob("*.part")), [])
