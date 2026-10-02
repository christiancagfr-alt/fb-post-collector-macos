import re
from urllib.parse import parse_qs, urlencode, urlparse

from .. import db
from ..security import SpreadsheetFormula
from . import drive_storage, gyazo


def upload_file(path):
    if db.setting_get("image_storage_provider", "gyazo") == "drive":
        return drive_storage.upload_file(path)
    return gyazo.upload_file(path)


def preview_formula(url):
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or any(char in url for char in ('"', '\r', '\n')):
        return ""
    if parsed.hostname == "drive.google.com":
        query = parse_qs(parsed.query)
        match = re.search(r"/file/d/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
        file_id = match.group(1) if match else (query.get("id") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            return ""
        params = {"id": file_id, "sz": "w1000"}
        if query.get("resourcekey"):
            params["resourcekey"] = query["resourcekey"][0]
        url = "https://drive.google.com/thumbnail?" + urlencode(params)
    return SpreadsheetFormula(f'=IMAGE("{url}")')
