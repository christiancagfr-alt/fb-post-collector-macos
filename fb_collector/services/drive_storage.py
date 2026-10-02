"""Per-user Drive authorization and serial, round-robin image uploads."""
import json
import mimetypes
import threading
from pathlib import Path
import httplib2
from google_auth_httplib2 import AuthorizedHttp

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .. import db
from ..credential_store import protect, reveal, read_secret_file, write_secret_file
from ..config import TOKEN_DIR
from .errors import UploadError
from .task_control import stage, interruptible_lock
from .sheets import bundled_credentials_path, trusted_google_config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_upload_lock = threading.RLock()
_login_lock = threading.Lock()
_login_state = {"status": "idle", "message": ""}
_cursor = 0
LABELS = {"ready": "已登录", "full": "空间不足", "auth_error": "授权失效，请重新登录", "error": "异常", "unknown": "待检测"}


def init_storage():
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS drive_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL, name TEXT NOT NULL, avatar TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL, folder_id TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'ready',
            detail TEXT NOT NULL DEFAULT '', used INTEGER NOT NULL DEFAULT 0,
            capacity INTEGER, checked_at TEXT NOT NULL DEFAULT '')""")


def accounts():
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id,email,name,avatar,enabled,status,detail,used,capacity,checked_at FROM drive_accounts ORDER BY id"
        )]
    for row in rows:
        row["status_label"] = LABELS.get(row["status"], "异常")
        row["used_gb"] = round(row["used"] / 1024 ** 3, 2)
        row["capacity_gb"] = round(row["capacity"] / 1024 ** 3, 2) if row["capacity"] is not None else None
    return rows


def _update(account_id, **values):
    if "token" in values:
        values["token"] = protect(values["token"])
    allowed = {"token", "folder_id", "enabled", "status", "detail", "used", "capacity", "checked_at", "avatar", "name"}
    if not values or not set(values).issubset(allowed):
        raise ValueError("无效账号更新")
    with db.connect() as conn:
        conn.execute(f"UPDATE drive_accounts SET {','.join(key + '=?' for key in values)} WHERE id=?",
                     [*values.values(), account_id])


def _account(account_id):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM drive_accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError("云盘账号不存在")
    result = dict(row)
    result["token"] = reveal(result["token"])
    return result


def save_client(upload):
    raw = upload.read(65537)
    if len(raw) > 65536:
        raise ValueError("OAuth 配置文件过大")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("请选择有效的 OAuth JSON") from None
    installed = data.get("installed", {}) if isinstance(data, dict) else {}
    if not isinstance(installed, dict) or not installed.get("client_id") or not installed.get("client_secret"):
        raise ValueError("需要桌面应用 OAuth 客户端 JSON，不能使用服务账号 JSON")
    # Use Google's endpoints even if an imported file contains custom endpoints.
    installed["auth_uri"] = "https://accounts.google.com/o/oauth2/auth"
    installed["token_uri"] = "https://oauth2.googleapis.com/token"
    installed["redirect_uris"] = ["http://localhost"]
    write_secret_file(TOKEN_DIR / "drive_client.json", json.dumps({"installed": installed}))


def client_path():
    own = TOKEN_DIR / "drive_client.json"
    return own if own.exists() else bundled_credentials_path()


def login_status():
    with _login_lock:
        return dict(_login_state)


def start_login():
    path = client_path()
    if not path.exists():
        raise ValueError("请先导入 Google 桌面应用 OAuth 客户端 JSON")
    try:
        data = json.loads(read_secret_file(path))
    except ValueError:
        raise ValueError("OAuth 客户端配置无效") from None
    if "installed" not in data:
        raise ValueError("请导入桌面应用 OAuth 客户端 JSON；现有表格服务账号不能用于个人云盘登录")
    with _login_lock:
        if _login_state["status"] == "running":
            raise ValueError("登录正在进行，请完成已打开的 Google 授权页面")
        _login_state.update(status="running", message="请在浏览器选择 Google 账号并授权（5 分钟内完成）")
    threading.Thread(target=_login_worker, args=(str(path),), daemon=True).start()


def _login_worker(path):
    try:
        flow = InstalledAppFlow.from_client_config(trusted_google_config(json.loads(read_secret_file(path))), SCOPES)
        creds = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=300,
                                      prompt="consent select_account", access_type="offline")
        if not creds or not creds.refresh_token:
            raise ValueError("未取得离线授权，请重新登录并同意授权")
        service = _build_service(creds)
        info = service.about().get(fields="user,storageQuota").execute()
        user, quota = info["user"], info.get("storageQuota", {})
        identity = user.get("permissionId") or user["emailAddress"]
        with _upload_lock, db.connect() as conn:
            conn.execute("""INSERT INTO drive_accounts(identity,email,name,avatar,token,checked_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                email=excluded.email,name=excluded.name,avatar=excluded.avatar,token=excluded.token,
                status='ready',detail='',checked_at=excluded.checked_at""",
                (identity, user.get("emailAddress", ""), user.get("displayName", "Google"),
                 user.get("photoLink", ""), protect(creds.to_json()), db.utc_now()))
            conn.execute("UPDATE drive_accounts SET used=?,capacity=? WHERE identity=?",
                         (int(quota.get("usage", 0)), int(quota["limit"]) if "limit" in quota else None, identity))
        message = "登录成功，账号已保存；重复登录会更新原账号授权"
        status = "success"
    except Exception as exc:
        status, message = "failed", error_state(exc)[1]
    with _login_lock:
        _login_state.update(status=status, message=message)


def _build_service(creds):
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=60))
    return build("drive", "v3", http=http, cache_discovery=False)


def _service(account):
    creds = Credentials.from_authorized_user_info(json.loads(account["token"]), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise RefreshError("Missing refresh token")
        creds.refresh(Request())
        _update(account["id"], token=creds.to_json())
    return _build_service(creds)


def error_state(exc):
    if isinstance(exc, RefreshError):
        return "auth_error", "Google 授权已失效，请重新登录该账号"
    if isinstance(exc, HttpError):
        content = (exc.content or b"").decode("utf-8", errors="replace")
        if "storageQuotaExceeded" in content:
            return "full", "云盘容量不足，已尝试切换其他账号"
        if exc.resp.status == 401:
            return "auth_error", "Google 授权已失效，请重新登录该账号"
        if "accessNotConfigured" in content or "SERVICE_DISABLED" in content:
            return "error", "请在 OAuth 所属 Google Cloud 项目启用 Google Drive API"
        return "error", f"Google Drive 请求失败（HTTP {exc.resp.status}），请检查网络、权限和额度后重新检测"
    if isinstance(exc, ValueError):
        return "error", str(exc)
    return "error", f"云盘连接或操作失败（{type(exc).__name__}），请检查网络后重新检测"


def _inspect(account, service):
    info = service.about().get(fields="user,storageQuota").execute(num_retries=2)
    quota = info.get("storageQuota", {})
    used = int(quota.get("usage", 0))
    capacity = int(quota["limit"]) if "limit" in quota else None
    state = "full" if capacity is not None and used >= capacity else "ready"
    _update(account["id"], status=state, detail="空间不足" if state == "full" else "",
            used=used, capacity=capacity, checked_at=db.utc_now(),
            avatar=info.get("user", {}).get("photoLink", ""), name=info.get("user", {}).get("displayName", account["name"]))
    return capacity, used


def check_account(account_id):
    with _upload_lock:
        account = _account(account_id)
        try:
            _inspect(account, _service(account))
        except Exception as exc:
            state, detail = error_state(exc)
            _update(account_id, status=state, detail=detail, checked_at=db.utc_now())


def change_account(account_id, action):
    with _upload_lock:
        account = _account(account_id)
        if action == "remove":
            with db.connect() as conn:
                conn.execute("DELETE FROM drive_accounts WHERE id=?", (account_id,))
        elif action == "toggle":
            _update(account_id, enabled=0 if account["enabled"] else 1)


def upload_file(path):
    global _cursor
    image = Path(path)
    if not image.is_file():
        raise UploadError("图片上传失败：本地图片不存在")
    # Serial uploads prevent two workers from choosing the same nearly full account.
    stage("等待云盘上传")
    with interruptible_lock(_upload_lock):
        pool = [item for item in accounts() if item["enabled"] and item["status"] not in {"auth_error", "full"}]
        if not pool:
            raise UploadError("没有可用 Google Drive 账号，请登录账号或清理容量后重新检测")
        start = _cursor % len(pool)
        _cursor += 1
        errors = []
        for item in pool[start:] + pool[:start]:
            stage("检查云盘容量并上传图片")
            account = _account(item["id"])
            try:
                service = _service(account)
                capacity, used = _inspect(account, service)
                if capacity is not None and capacity - used < image.stat().st_size:
                    _update(account["id"], status="full", detail="剩余容量不足以保存本次图片")
                    errors.append(f"{account['email']}：空间不足")
                    continue
                folder = account["folder_id"]
                if not folder:
                    folder = service.files().create(body={"name": "FBPostCollector 图片", "mimeType": "application/vnd.google-apps.folder"}, fields="id").execute()["id"]
                    _update(account["id"], folder_id=folder)
                media = MediaFileUpload(str(image), mimetype=mimetypes.guess_type(image.name)[0] or "application/octet-stream", resumable=True)
                try:
                    uploaded = service.files().create(body={"name": image.name, "parents": [folder]}, media_body=media, fields="id,webViewLink").execute(num_retries=2)
                finally:
                    media.stream().close()
                file_id = uploaded["id"]
                link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
                if db.setting_get("drive_public_links", "0") == "1":
                    try:
                        service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute(num_retries=2)
                    except Exception:
                        # Preserve the created file and its link; do not duplicate uploads.
                        _update(account["id"], status="error", detail="图片已保存，但公开共享失败；返回的链接需要登录该云盘账号查看")
                        return link
                _update(account["id"], status="ready", detail="", used=used + image.stat().st_size)
                return link
            except Exception as exc:
                state, detail = error_state(exc)
                _update(account["id"], status=state, detail=detail, checked_at=db.utc_now())
                errors.append(f"{account['email']}：{detail}")
        raise UploadError("所有 Google Drive 账号均上传失败：" + "；".join(errors))
