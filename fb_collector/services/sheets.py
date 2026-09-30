import json
import re
import socket
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import APP_ROOT_DIR, TOKEN_DIR
from .. import db
from ..fields import column_to_index, index_to_column, normalize_column
from .errors import SheetWriteError


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_RETRY_ATTEMPTS = 3
SHEET_RETRY_DELAY_SECONDS = 2


def extract_spreadsheet_id(url_or_id: str) -> str:
    text = (url_or_id or "").strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    return match.group(1) if match else text


class SheetsClient:
    def __init__(self, credentials_path=None, token_path=None):
        configured_credentials = credentials_path or db.setting_get("google_credentials_path")
        configured_token = token_path or db.setting_get("google_token_path")
        self.credentials_path = Path(configured_credentials or TOKEN_DIR / "google_credentials.json")
        self.token_path = Path(configured_token or TOKEN_DIR / "google_token.json")
        self.service = None

    def authenticate(self):
        creds = load_google_credentials()
        info = inspect_credentials_file()
        if info.get("type") == "service_account":
            if not creds:
                raise SheetWriteError(
                    "请先在授权设置中选择并保存 Google 服务账号 JSON 文件。",
                    "Missing service account credentials",
                )
            if not creds.valid:
                creds.refresh(Request())
            self.service = build("sheets", "v4", credentials=creds)
            return True
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds)
        if not creds or not creds.valid:
            creds = run_google_oauth()
        self.service = build("sheets", "v4", credentials=creds)
        return True

    def _service(self):
        if not self.service:
            self.authenticate()
        return self.service

    def _execute(self, build_request, action_message):
        last_exc = None
        for attempt in range(1, SHEET_RETRY_ATTEMPTS + 1):
            try:
                return build_request().execute(num_retries=2)
            except Exception as exc:
                last_exc = exc
                if attempt >= SHEET_RETRY_ATTEMPTS or not is_retryable_sheet_error(exc):
                    break
                time.sleep(SHEET_RETRY_DELAY_SECONDS * attempt)
        user_message = readable_sheet_error(action_message, last_exc)
        raise SheetWriteError(user_message, repr(last_exc)) from last_exc

    def read_column(self, spreadsheet_id, worksheet_name, column, start_row):
        col = normalize_column(column)
        if not col:
            return []
        rng = f"'{worksheet_name}'!{col}{start_row}:{col}"
        resp = self._execute(
            lambda: self._service().spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng),
            "读取表格失败",
        )
        values = resp.get("values", [])
        rows = []
        for offset, row in enumerate(values):
            rows.append({"row_number": start_row + offset, "url": row[0].strip() if row else ""})
        return rows

    def read_column_values(self, spreadsheet_id, worksheet_name, column, start_row):
        return {
            row["row_number"]: row["url"]
            for row in self.read_column(spreadsheet_id, worksheet_name, column, start_row)
        }

    def next_empty_row(self, spreadsheet_id, worksheet_name, column, start_row):
        rows = self.read_column(spreadsheet_id, worksheet_name, column, start_row)
        if not rows:
            return int(start_row or 2)
        return max(row["row_number"] for row in rows) + 1

    def read_range_rows(self, spreadsheet_id, worksheet_name, start_column, end_column, start_row):
        start = start_column.strip().upper()
        end = end_column.strip().upper()
        rng = f"'{worksheet_name}'!{start}{start_row}:{end}"
        resp = self._execute(
            lambda: self._service().spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng),
            "读取表格失败",
        )
        values = resp.get("values", [])
        rows = []
        for offset, row in enumerate(values):
            rows.append({"row_number": start_row + offset, "values": row})
        return rows

    def read_cell(self, spreadsheet_id, worksheet_name, column, row_number):
        col = column.strip().upper()
        rng = f"'{worksheet_name}'!{col}{row_number}:{col}{row_number}"
        resp = self._execute(
            lambda: self._service().spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng),
            "读取表格失败",
        )
        values = resp.get("values", [])
        return values[0][0] if values and values[0] else ""

    def write_headers(self, spreadsheet_id, worksheet_name, start_column, header_row, labels):
        start = column_to_index(start_column)
        end_col = index_to_column(start + len(labels) - 1)
        rng = f"'{worksheet_name}'!{start_column.upper()}{header_row}:{end_col}{header_row}"
        self.write_values(spreadsheet_id, rng, [labels])

    def write_row(self, spreadsheet_id, worksheet_name, start_column, row_number, values):
        start = column_to_index(start_column)
        end_col = index_to_column(start + len(values) - 1)
        rng = f"'{worksheet_name}'!{start_column.upper()}{row_number}:{end_col}{row_number}"
        self.write_values(spreadsheet_id, rng, [values])

    def write_mapped_values(self, spreadsheet_id, worksheet_name, row_number, pairs):
        data = []
        for column, value in pairs or []:
            col = normalize_column(column)
            if not col:
                continue
            data.append(
                {
                    "range": f"'{worksheet_name}'!{col}{row_number}",
                    "values": [[value]],
                }
            )
        if not data:
            return
        self._execute(
            lambda: self._service().spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ),
            "写入表格失败",
        )

    def write_cell(self, spreadsheet_id, worksheet_name, column, row_number, value):
        col = column.strip().upper()
        rng = f"'{worksheet_name}'!{col}{row_number}:{col}{row_number}"
        self.write_values(spreadsheet_id, rng, [[value]])

    def append_row(self, spreadsheet_id, worksheet_name, start_column, values):
        col = start_column.strip().upper()
        rng = f"'{worksheet_name}'!{col}:{col}"
        self._execute(
            lambda: self._service().spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=rng,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [values]},
            ),
            "写入表格失败",
        )

    def write_values(self, spreadsheet_id, range_name, values):
        self._execute(
            lambda: self._service().spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                body={"values": values},
            ),
            "写入表格失败",
        )


def bundled_credentials_path():
    configured = db.setting_get("google_credentials_path")
    if configured:
        path = Path(configured)
        if path.exists():
            return path
    candidates = [
        TOKEN_DIR / "google_credentials.json",
        APP_ROOT_DIR / "google_credentials.json",
        Path.cwd() / "google_credentials.json",
        Path(__file__).resolve().parents[2] / "google_credentials.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return TOKEN_DIR / "google_credentials.json"


def inspect_credentials_file(path=None):
    target = Path(path) if path else bundled_credentials_path()
    if not target.exists():
        return {"exists": False, "type": "", "email": "", "path": str(target)}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {"exists": True, "type": "invalid", "email": "", "path": str(target)}
    if not isinstance(data, dict):
        return {"exists": True, "type": "invalid", "email": "", "path": str(target)}
    if data.get("type") == "service_account":
        return {
            "exists": True,
            "type": "service_account",
            "email": str(data.get("client_email") or ""),
            "path": str(target),
        }
    if "installed" in data or "web" in data:
        return {"exists": True, "type": "oauth_client", "email": "", "path": str(target)}
    return {"exists": True, "type": "unknown", "email": "", "path": str(target)}


def save_uploaded_google_credentials(uploaded_file):
    raw = uploaded_file.read()
    if not raw:
        raise ValueError("文件是空的，请选择 Google 服务账号 JSON。")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("无法解析 JSON 文件。") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 格式不正确。")
    if data.get("type") == "service_account":
        if not data.get("client_email") or not data.get("private_key"):
            raise ValueError("这不是有效的服务账号文件，请选择含 client_email 和 private_key 的 JSON。")
        cred_type = "service_account"
        email = str(data.get("client_email") or "")
    elif "installed" in data or "web" in data:
        cred_type = "oauth_client"
        email = ""
    else:
        raise ValueError("请选择 Google 服务账号 JSON 文件。")
    dest = TOKEN_DIR / "google_credentials.json"
    dest.write_bytes(raw)
    db.setting_set("google_credentials_path", str(dest))
    db.setting_set("google_auth_type", cred_type)
    db.setting_set("google_service_account_email", email)
    if cred_type == "service_account":
        db.setting_set("google_token_json", "")
    return {"type": cred_type, "email": email, "path": str(dest)}


def load_google_credentials():
    info = inspect_credentials_file()
    if info.get("type") == "service_account":
        return service_account.Credentials.from_service_account_file(info["path"], scopes=SCOPES)
    creds = load_saved_credentials()
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
        except Exception:
            pass
    return creds


def load_saved_credentials():
    token_json = db.setting_get("google_token_json")
    if token_json:
        try:
            return Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        except Exception:
            return None
    legacy_token = db.setting_get("google_token_path")
    if legacy_token and Path(legacy_token).exists():
        return Credentials.from_authorized_user_file(legacy_token, SCOPES)
    return None


def save_credentials(creds):
    db.setting_set("google_token_json", creds.to_json())


def google_auth_status():
    info = inspect_credentials_file()
    creds = None
    if info.get("type") != "service_account":
        creds = load_saved_credentials()
    authorized = False
    if info.get("type") == "service_account":
        try:
            authorized = bool(load_google_credentials() and info.get("email"))
        except Exception:
            authorized = False
    else:
        authorized = bool(creds and creds.valid)
    return {
        "authorized": authorized,
        "auth_type": info.get("type") or "",
        "service_account_email": info.get("email") or "",
        "expired": bool(creds and creds.expired) if creds else False,
        "has_refresh_token": bool(creds and creds.refresh_token) if creds else False,
        "credentials_file": info.get("path") or str(bundled_credentials_path()),
        "credentials_file_exists": bool(info.get("exists")),
    }


def run_google_oauth():
    info = inspect_credentials_file()
    if info.get("type") == "service_account":
        return load_google_credentials()
    credentials_file = bundled_credentials_path()
    if not credentials_file.exists():
        raise SheetWriteError(
            "请先选择并保存 Google 服务账号 JSON 文件。",
            f"Missing Google credentials file: {credentials_file}",
        )
    if info.get("type") != "oauth_client":
        raise SheetWriteError(
            "当前文件不是 OAuth 客户端配置。请上传服务账号 JSON，保存后即可授权。",
            f"Unsupported credentials type: {info.get('type')}",
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds


def clear_google_oauth():
    db.setting_set("google_token_json", "")
    db.setting_set("google_auth_type", "")
    db.setting_set("google_service_account_email", "")
    saved = TOKEN_DIR / "google_credentials.json"
    configured = db.setting_get("google_credentials_path")
    if configured:
        path = Path(configured)
        if path.exists() and path.resolve() == saved.resolve():
            path.unlink(missing_ok=True)
    elif saved.exists():
        saved.unlink(missing_ok=True)
    db.setting_set("google_credentials_path", "")


def is_retryable_sheet_error(exc):
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, HttpError):
        return exc.resp.status in {429, 500, 502, 503, 504}
    text = repr(exc).lower()
    return any(marker in text for marker in ["timed out", "timeout", "temporarily unavailable", "connection reset"])


def readable_sheet_error(action_message, exc):
    text = repr(exc).lower()
    content = ""
    if isinstance(exc, HttpError):
        try:
            content = (exc.content or b"").decode("utf-8", errors="replace").lower()
        except Exception:
            content = ""
    combined = f"{text} {content}"
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in combined or "timeout" in combined:
        return f"{action_message}：Google 表格连接超时，请检查网络后重试"
    if isinstance(exc, HttpError):
        if exc.resp.status in {401, 403} or "permission" in combined or "insufficient" in combined:
            email = inspect_credentials_file().get("email") or "服务账号邮箱"
            return (
                f"{action_message}：没有权限访问该表格。请把表格共享给 {email}，权限选“编辑者”。"
            )
        if exc.resp.status == 404 or "requested entity was not found" in combined:
            return f"{action_message}：找不到表格或工作表，请检查表格链接和工作表名称。"
        if "has not been used" in combined or "is disabled" in combined or "access not configured" in combined:
            return f"{action_message}：当前 Google Cloud 项目未启用 Google Sheets API。"
        if exc.resp.status == 429:
            return f"{action_message}：Google 表格请求太频繁，请稍后重试"
        if exc.resp.status in {500, 502, 503, 504}:
            return f"{action_message}：Google 表格服务暂时不可用，请稍后重试"
        return f"{action_message}：Google 返回 {exc.resp.status}"
    return action_message
