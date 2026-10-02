"""OS-keystore-backed authenticated encryption; never fall back to plaintext."""
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

PREFIX = "fbenc:v1:"
SECRET_SETTINGS = {"gyazo_access_token", "groq_api_key", "groq_api_keys",
                   "gemini_api_key", "gemini_api_keys", "google_token_json"}
_lock = threading.RLock()


@contextmanager
def _vault_lock():
    """Serialize first-key creation across app processes, not just threads."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, "Local\\FBPostCollector.CredentialKey.v1")
        acquired = False
        try:
            if not handle or kernel.WaitForSingleObject(handle, 10000) not in (0, 0x80):
                raise RuntimeError("凭据库正在被其他实例使用，请稍后重试")
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            if handle:
                kernel.CloseHandle(handle)
    elif sys.platform == "darwin":
        import fcntl
        with (Path(tempfile.gettempdir()) / "fbpostcollector-credential-key.lock").open("a+b") as handle:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("凭据库正在被其他实例使用，请稍后重试") from None
                    time.sleep(.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    else:
        raise RuntimeError("凭据加密仅支持 Windows 和 macOS")


def _backend():
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        return Keyring()
    raise RuntimeError("凭据加密仅支持 Windows 凭据管理器和 macOS 钥匙串")


def _cipher(create=False):
    with _lock, _vault_lock():
        try:
            vault = _backend()
            key = vault.get_password("FBPostCollector", "credential-encryption-v1")
            if not key:
                if not create:
                    raise RuntimeError("当前系统账号没有解密密钥，请使用原电脑账号或重新授权")
                key = Fernet.generate_key().decode("ascii")
                vault.set_password("FBPostCollector", "credential-encryption-v1", key)
                if vault.get_password("FBPostCollector", "credential-encryption-v1") != key:
                    raise RuntimeError("系统凭据库未能保存密钥")
            return Fernet(key.encode("ascii"))
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("无法访问系统凭据库，请解锁钥匙串/凭据管理器后重试；未降级为明文") from None


def protect(value):
    value = str(value or "")
    if not value:
        return value
    if value.startswith(PREFIX):
        reveal(value)  # Validate before retaining an existing encrypted value.
        return value
    return PREFIX + _cipher(create=True).encrypt(value.encode("utf-8")).decode("ascii")


def reveal(value):
    value = str(value or "")
    if not value.startswith(PREFIX):
        return value  # Legacy data is migrated explicitly, never newly written.
    try:
        return _cipher().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise RuntimeError("凭据无法解密或已损坏，请使用原系统账号或重新授权；原数据未修改") from None


def read_secret_file(path):
    return reveal(Path(path).read_text(encoding="utf-8"))


def write_secret_file(path, text):
    encrypted = protect(text)  # Acquire/verify the key before touching the old file.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def migrate_credentials():
    from . import db
    from .config import TOKEN_DIR
    # Check existing ciphertexts first: a missing vault key must never be replaced.
    with db.connect() as conn:
        stored_settings = list(conn.execute("SELECT key,value FROM app_settings"))
        stored_accounts = list(conn.execute("SELECT id,token FROM drive_accounts"))
        for value in [row["value"] for row in stored_settings if row["key"] in SECRET_SETTINGS] + [row["token"] for row in stored_accounts]:
            if value and value.startswith(PREFIX):
                reveal(value)
        for name in ("google_credentials.json", "drive_client.json", "google_token.json"):
            path = TOKEN_DIR / name
            if path.is_file():
                read_secret_file(path)
        # Prepare every database ciphertext before the first database write.
        settings = [(protect(row["value"]), row["key"]) for row in stored_settings
                    if row["key"] in SECRET_SETTINGS and row["value"] and not row["value"].startswith(PREFIX)]
        accounts = [(protect(row["token"]), row["id"]) for row in stored_accounts
                    if row["token"] and not row["token"].startswith(PREFIX)]
        conn.executemany("UPDATE app_settings SET value=? WHERE key=?", settings)
        conn.executemany("UPDATE drive_accounts SET token=? WHERE id=?", accounts)
    for name in ("google_credentials.json", "drive_client.json", "google_token.json"):
        path = TOKEN_DIR / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if not text.startswith(PREFIX):
                write_secret_file(path, text)
    # Copy legacy configured files into managed encrypted storage; never alter
    # a user's external original JSON, which may be shared with other programs.
    configured = db.setting_get("google_credentials_path")
    if configured and Path(configured).is_file():
        destination = TOKEN_DIR / "google_credentials.json"
        if Path(configured).resolve() != destination.resolve():
            write_secret_file(destination, read_secret_file(configured))
            db.setting_set("google_credentials_path", str(destination))
    legacy_token = db.setting_get("google_token_path")
    if legacy_token and Path(legacy_token).is_file():
        if not db.setting_get("google_token_json"):
            db.setting_set("google_token_json", read_secret_file(legacy_token))
        db.setting_set("google_token_path", "")
    if settings or accounts:
        with db.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
