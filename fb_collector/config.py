import os
import sys
from pathlib import Path


APP_NAME = "FBPostCollector"
APP_VERSION = "1.4.18"
BASE_DIR = Path(__file__).resolve().parent.parent
APP_ROOT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE_DIR


def user_data_dir() -> Path:
    override = os.environ.get("FB_COLLECTOR_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    elif os.name == "nt":
        path = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        path = Path.home() / ".fb_post_collector"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return path
    except OSError:
        fallback = BASE_DIR / ".data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = user_data_dir()
DB_PATH = DATA_DIR / "collector.sqlite3"
TOKEN_DIR = DATA_DIR / "tokens"
TOKEN_DIR.mkdir(parents=True, exist_ok=True)
TOOLS_DIR = BASE_DIR / "tools"
if sys.platform == "darwin":
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")
TEMP_DIR = DATA_DIR / "tmp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# 可从环境变量注入默认 Token；禁止在源码或发布包中硬编码真实凭据。
DEFAULT_GYAZO_ACCESS_TOKEN = os.environ.get("FB_COLLECTOR_DEFAULT_GYAZO_TOKEN", "")
