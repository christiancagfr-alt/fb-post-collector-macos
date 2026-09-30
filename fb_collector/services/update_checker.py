import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import requests

from ..config import APP_VERSION, DATA_DIR


REPOSITORY = "secure-artifacts/fb-post-collector"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
CHECK_CACHE_SECONDS = 600
_CACHE_LOCK = threading.Lock()
_CACHE = {"checked_at": 0.0, "value": None}
UPDATE_LOCK = threading.Lock()
UPDATE_STATE = {
    "status": "idle",
    "message": "",
    "progress": 0,
    "logs": [],
    "installer_path": "",
    "version": "",
}


def version_tuple(value):
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple((numbers + [0, 0, 0, 0])[:4])


def preferred_assets(assets):
    output = []
    priority = {".exe": 0, ".msi": 1, ".zip": 2}
    if sys.platform == "darwin":
        priority = {".dmg": 0}
    for asset in assets or []:
        name = str(asset.get("name") or "")
        if sys.platform == "darwin" and platform.machine().lower() not in name.lower():
            continue
        url = str(asset.get("browser_download_url") or "")
        suffix = next((item for item in priority if name.lower().endswith(item)), "")
        if not suffix or not url.startswith("https://github.com/"):
            continue
        output.append(
            {
                "name": name,
                "url": url,
                "size": int(asset.get("size") or 0),
                "kind": suffix.lstrip(".").upper(),
            }
        )
    return sorted(output, key=lambda item: priority.get("." + item["kind"].lower(), 9))


def installer_asset(assets):
    for item in assets or []:
        name = str(item.get("name") or "").lower()
        if item.get("kind") == "EXE" and "setup" in name:
            return item
    for item in assets or []:
        if item.get("kind") == "EXE":
            return item
    return None


def auto_update_status():
    with UPDATE_LOCK:
        state = dict(UPDATE_STATE)
        state["logs"] = list(UPDATE_STATE.get("logs") or [])
        return state


def _update_log(message, progress=None, status=None):
    text = str(message or "").strip()
    with UPDATE_LOCK:
        if text:
            logs = UPDATE_STATE.setdefault("logs", [])
            logs.append(text)
            del logs[:-80]
            UPDATE_STATE["message"] = text
        if progress is not None:
            UPDATE_STATE["progress"] = int(progress)
        if status:
            UPDATE_STATE["status"] = status


def start_auto_update():
    if sys.platform == "darwin":
        raise RuntimeError("Mac 版请下载对应芯片的 DMG，退出软件后拖入 Applications 覆盖安装。")
    if not getattr(sys, "frozen", False):
        raise RuntimeError("当前是源码运行，请下载安装包更新。")
    info = check_for_update(force=True)
    installer = info.get("installer")
    if not info.get("update_available"):
        raise RuntimeError("当前已经是最新版本。")
    if not installer:
        raise RuntimeError("没有找到可自动安装的 EXE 安装包，请手动下载。")
    with UPDATE_LOCK:
        if UPDATE_STATE["status"] in {"running", "restarting"}:
            raise RuntimeError("正在更新中，请稍候。")
        UPDATE_STATE.update(
            {
                "status": "running",
                "message": f"准备下载 v{info['latest_version']}",
                "progress": 0,
                "logs": [f"准备下载 v{info['latest_version']}"],
                "installer_path": "",
                "version": info["latest_version"],
            }
        )
    thread = threading.Thread(target=_update_worker, args=(installer, info["latest_version"]), daemon=True)
    thread.start()
    return auto_update_status()


def _download_installer(url, dest, label):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": f"FBPostCollector/{APP_VERSION}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        read = 0
        last = -1
        with tmp.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                read += len(chunk)
                if total:
                    pct = min(99, int(read * 100 / total))
                    if pct >= last + 5:
                        last = pct
                        _update_log(f"正在下载 {label}：{pct}%", progress=pct)
    tmp.replace(dest)
    _update_log(f"安装包已下载：{dest.name}", progress=100)
    return dest


def _update_worker(installer, version):
    try:
        folder = DATA_DIR / "updates"
        dest = folder / installer["name"]
        _update_log(f"开始下载 {installer['name']}", progress=1, status="running")
        _download_installer(installer["url"], dest, installer["name"])
        with UPDATE_LOCK:
            UPDATE_STATE["installer_path"] = str(dest)
        _update_log("正在启动安装程序，软件即将自动重启", progress=100, status="restarting")
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x01000000
        popen_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "creationflags": flags,
        }
        if os.name != "nt":
            popen_kwargs["close_fds"] = True
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(
            [str(dest), "/VERYSILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS"],
            **popen_kwargs,
        )
        time.sleep(1.2)
        os._exit(0)
    except Exception as exc:
        _update_log(f"自动更新失败：{exc}", status="failed")


def check_for_update(force=False):
    now = time.time()
    with _CACHE_LOCK:
        if not force and _CACHE["value"] and now - _CACHE["checked_at"] < CHECK_CACHE_SECONDS:
            return dict(_CACHE["value"])

    response = requests.get(
        (f"https://api.github.com/repos/{REPOSITORY}/releases" if sys.platform == "darwin" else LATEST_RELEASE_API),
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"FBPostCollector/{APP_VERSION}"},
        timeout=12,
    )
    response.raise_for_status()
    release = response.json()
    if sys.platform == "darwin":
        release = next((item for item in release if not item.get("draft") and preferred_assets(item.get("assets"))), {})
        if not release:
            raise RuntimeError("暂未找到适用于当前 Mac 芯片的安装包，请查看仓库发布页面。")
    latest_version = str(release.get("tag_name") or "").lstrip("vV")
    release_url = str(release.get("html_url") or "")
    if not release_url.startswith("https://github.com/"):
        release_url = f"https://github.com/{REPOSITORY}/releases"
    assets = preferred_assets(release.get("assets"))
    value = {
        "ok": True,
        "current_version": APP_VERSION,
        "latest_version": latest_version,
        "update_available": version_tuple(latest_version) > version_tuple(APP_VERSION),
        "release_name": release.get("name") or release.get("tag_name") or "",
        "release_notes": release.get("body") or "",
        "published_at": release.get("published_at") or "",
        "release_url": release_url,
        "assets": assets,
        "installer": installer_asset(assets),
        "can_auto_update": bool(installer_asset(assets) and getattr(sys, "frozen", False)),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    with _CACHE_LOCK:
        _CACHE.update({"checked_at": now, "value": value})
    return dict(value)
