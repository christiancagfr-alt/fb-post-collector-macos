import os
import sys
import shutil
import time
from pathlib import Path

from ..config import BASE_DIR, DATA_DIR, TOOLS_DIR
from .browser_profiles import chrome_executable
from .proc import run_hidden

_TOOLS_CACHE = {"at": 0, "value": None}
_TOOLS_CACHE_SECONDS = 60

USER_TOOLS_DIR = DATA_DIR / "tools"
CORE_TESSERACT_LANGS = ("eng", "por", "ara", "chi_sim")
OPTIONAL_TESSERACT_LANGS = ("swa", "fra", "Latin")
TESSERACT_LANGS = CORE_TESSERACT_LANGS + OPTIONAL_TESSERACT_LANGS


def first_existing(paths):
    for path in paths:
        if path and Path(path).exists():
            return str(Path(path))
    return ""


def command_available(command):
    candidate = Path(command) if command else None
    found = str(candidate) if candidate and candidate.exists() else shutil.which(command)
    if not found:
        return {"available": False, "path": "", "version": ""}
    version = ""
    try:
        is_whisper = Path(found).name.lower() in ("whisper.exe", "whisper")
        flag = "--help" if is_whisper else ("-version" if Path(found).stem == "ffmpeg" else "--version")
        proc = run_hidden([found, flag], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60 if is_whisper else 10)
        version = (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else ""
        if proc.returncode != 0:
            return {"available": False, "path": found, "version": version or "运行检测失败"}
    except Exception:
        return {"available": False, "path": found, "version": "已找到，但运行检测失败"}
    return {"available": True, "path": found, "version": version}


def tessdata_dirs(tesseract_path=""):
    dirs = []
    if tesseract_path:
        dirs.append(Path(tesseract_path).parent / "tessdata")
    dirs.extend(
        [
            USER_TOOLS_DIR / "tesseract" / "tessdata",
            TOOLS_DIR / "tesseract" / "tessdata",
        ]
    )
    unique = []
    seen = set()
    for directory in dirs:
        key = str(directory).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(directory)
    return unique


def lang_available(directories, lang):
    return any((Path(directory) / f"{lang}.traineddata").exists() for directory in directories)


def detect_tools(force=False):
    now = time.time()
    if not force and _TOOLS_CACHE["value"] is not None and now - _TOOLS_CACHE["at"] < _TOOLS_CACHE_SECONDS:
        return _TOOLS_CACHE["value"]
    result = _detect_tools()
    _TOOLS_CACHE["at"] = now
    _TOOLS_CACHE["value"] = result
    return result


def _python_scripts_dirs():
    dirs = []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    for base in (local / "Programs" / "Python", roaming / "Python"):
        if not base.exists():
            continue
        for child in sorted(base.glob("Python3*")):
            dirs.append(child / "Scripts")
        for child in sorted(base.glob("python3*")):
            dirs.append(child / "Scripts")
    return dirs


def _detect_tools():
    if sys.platform == "darwin":
        result = {}
        for key, command in (("tesseract", "tesseract"), ("ffmpeg", "ffmpeg"), ("yt_dlp", "yt-dlp"), ("whisper", "whisper")):
            local = USER_TOOLS_DIR / "whisper-venv" / "bin" / command
            result[key] = command_available(str(local) if key == "whisper" and local.exists() else command)
        directories = tessdata_dirs(result["tesseract"]["path"]) + [Path("/opt/homebrew/share/tessdata"), Path("/usr/local/share/tessdata")]
        result["tesseract"].update({lang: lang_available(directories, lang) for lang in TESSERACT_LANGS})
        chrome = chrome_executable()
        result.update(base_dir=str(BASE_DIR), tessdata_dirs=[str(p) for p in directories], chrome={"available": Path(chrome).is_file(), "path": chrome})
        return result
    winget_links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
    local_programs = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
    tesseract_path = first_existing(
        [
            TOOLS_DIR / "tesseract" / "tesseract.exe",
            USER_TOOLS_DIR / "tesseract" / "tesseract.exe",
            USER_TOOLS_DIR / "tesseract" / "tesseract" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Tesseract-OCR" / "tesseract.exe",
            local_programs / "Tesseract-OCR" / "tesseract.exe",
            winget_links / "tesseract.exe",
            shutil.which("tesseract") or "",
        ]
    )
    ffmpeg_path = first_existing(
        [
            TOOLS_DIR / "ffmpeg" / "ffmpeg.exe",
            TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
            USER_TOOLS_DIR / "ffmpeg" / "ffmpeg.exe",
            USER_TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
            winget_links / "ffmpeg.exe",
            Path(os.environ.get("ProgramFiles", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
            shutil.which("ffmpeg") or "",
        ]
    )
    whisper_path = first_existing(
        [
            USER_TOOLS_DIR / "whisper-venv" / "Scripts" / "whisper.exe",
            USER_TOOLS_DIR / "whisper-venv" / "Scripts" / "whisper.EXE",
            USER_TOOLS_DIR / "whisper.exe",
            USER_TOOLS_DIR / "whisper.EXE",
            TOOLS_DIR / "whisper.exe",
            TOOLS_DIR / "whisper.EXE",
            *[path / "whisper.exe" for path in _python_scripts_dirs()],
            *[path / "whisper.EXE" for path in _python_scripts_dirs()],
            shutil.which("whisper") or "",
            shutil.which("whisper.EXE") or "",
        ]
    )
    whisper = command_available(whisper_path) if whisper_path else {"available": False, "path": "", "version": ""}
    ytdlp_path = first_existing(
        [
            TOOLS_DIR / "yt-dlp.exe",
            TOOLS_DIR / "yt-dlp" / "yt-dlp.exe",
            USER_TOOLS_DIR / "yt-dlp.exe",
            USER_TOOLS_DIR / "yt-dlp" / "yt-dlp.exe",
            winget_links / "yt-dlp.exe",
            shutil.which("yt-dlp.exe") or "",
            shutil.which("yt-dlp") or "",
        ]
    )
    ytdlp = command_available(ytdlp_path) if ytdlp_path else {"available": False, "path": "", "version": ""}
    tessdata = tessdata_dirs(tesseract_path)
    chrome_path = chrome_executable()
    chrome_available = bool(chrome_path and Path(chrome_path).exists())
    return {
        "base_dir": str(BASE_DIR),
        "tessdata_dirs": [str(path) for path in tessdata],
        "tesseract": {
            "available": bool(tesseract_path),
            "path": tesseract_path,
            **{lang: lang_available(tessdata, lang) for lang in TESSERACT_LANGS},
        },
        "ffmpeg": {"available": bool(ffmpeg_path), "path": ffmpeg_path},
        "whisper": whisper,
        "yt_dlp": ytdlp,
        "chrome": {
            "available": chrome_available,
            "path": chrome_path if chrome_available else "",
        },
    }
