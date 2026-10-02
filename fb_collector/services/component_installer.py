import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA_DIR, TOOLS_DIR
from .environment import CORE_TESSERACT_LANGS, USER_TOOLS_DIR, detect_tools
from .proc import hidden_popen_kwargs, run_hidden


INSTALL_LOG_PATH = DATA_DIR / "component_install.log"
INSTALL_LOCK = threading.Lock()
INSTALL_STATE = {
    "status": "idle",
    "components": [],
    "current": "",
    "message": "",
    "logs": [],
    "results": [],
    "started_at": "",
    "finished_at": "",
}
COMPONENT_LABELS = {
    "tesseract": "Tesseract OCR及语言包",
    "ffmpeg": "ffmpeg",
    "whisper": "Whisper语音识别",
    "yt_dlp": "yt-dlp",
    "tessdata_swa": "斯瓦希里语 OCR 语言包",
    "tessdata_fra": "法语 OCR 语言包",
    "tessdata_latin": "马达加斯加语 OCR 通用拉丁文字包",
}
WINGET_PACKAGES = {
    "tesseract": "UB-Mannheim.TesseractOCR",
    "ffmpeg": "Gyan.FFmpeg",
    "yt_dlp": "yt-dlp.yt-dlp",
    "python": "Python.Python.3.12",
}
from .component_manifest import (
    TESSDATA_URLS, YT_DLP_URL, FFMPEG_URLS, TESSERACT_MANUAL_LINKS,
    TESSERACT_SETUP_URLS, PYTHON_SETUP_URL, PIP_REQUIREMENT, WHISPER_REQUIREMENT,
)
from .verified_download import download_verified

if sys.platform == "darwin":
    TESSERACT_MANUAL_LINKS = [{"label": "Homebrew（macOS 组件安装）", "url": "https://brew.sh"}]

LANGUAGE_COMPONENTS = {
    "tessdata_swa": "swa", "tessdata_fra": "fra", "tessdata_latin": "Latin",
}


def now_text():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def installation_status():
    with INSTALL_LOCK:
        state = dict(INSTALL_STATE)
        state["logs"] = list(INSTALL_STATE.get("logs") or [])
        state["results"] = list(INSTALL_STATE.get("results") or [])
        return state


def log_install(message, *, status=False):
    text = str(message or "").strip()
    if not text:
        return
    with INSTALL_LOCK:
        logs = INSTALL_STATE.setdefault("logs", [])
        logs.append({"time": now_text(), "message": text})
        del logs[:-250]
        if status:
            INSTALL_STATE["message"] = text
    try:
        with INSTALL_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_text()} {text}\n")
    except OSError:
        pass


def start_installation(components):
    requested = []
    for component in components or []:
        if component in COMPONENT_LABELS and component not in requested:
            requested.append(component)
    if not requested:
        raise ValueError("没有可安装的缺失组件")
    with INSTALL_LOCK:
        if INSTALL_STATE["status"] == "running":
            raise RuntimeError("组件安装正在进行中")
        INSTALL_STATE.update(
            {
                "status": "running",
                "components": requested,
                "current": "",
                "message": "准备安装缺失组件",
                "logs": [{"time": now_text(), "message": "准备安装缺失组件"}],
                "results": [],
                "started_at": now_text(),
                "finished_at": "",
            }
        )
    thread = threading.Thread(target=_install_worker, args=(requested,), daemon=True)
    thread.start()
    return installation_status()


def _install_worker(components):
    results = []
    for component in components:
        label = COMPONENT_LABELS[component]
        with INSTALL_LOCK:
            INSTALL_STATE["current"] = component
        log_install(f"开始安装：{label}", status=True)
        try:
            result = install_one(component)
        except Exception as exc:
            log_install(f"{label} 失败：{exc}", status=True)
            result = {
                "component": component,
                "label": label,
                "success": False,
                "returncode": -1,
                "output": repr(exc),
            }
        results.append(result)
        append_install_log(result)
        log_install(f"{label}：{'成功' if result.get('success') else '失败'}", status=True)
        if result.get("output"):
            log_install(result["output"][-1500:])
        with INSTALL_LOCK:
            INSTALL_STATE["results"] = list(results)
        detect_tools(force=True)

    failed = [item for item in results if not item["success"]]
    message = f"安装完成：成功 {len(results) - len(failed)}，失败 {len(failed)}"
    log_install(message, status=True)
    with INSTALL_LOCK:
        INSTALL_STATE.update(
            {
                "status": "failed" if failed else "success",
                "current": "",
                "message": message,
                "results": results,
                "finished_at": now_text(),
            }
        )


def install_one(component):
    if sys.platform == "darwin":
        from .macos_components import install_component
        return install_component(component)
    if component in LANGUAGE_COMPONENTS:
        return install_language_pack(component, LANGUAGE_COMPONENTS[component])
    if component == "tesseract":
        return install_tesseract()
    if component == "ffmpeg":
        return install_ffmpeg()
    if component == "whisper":
        return install_whisper()
    if component == "yt_dlp":
        return install_yt_dlp()
    raise RuntimeError(f"不支持的组件：{component}")


def writable_tools_dir():
    for path in (USER_TOOLS_DIR, DATA_DIR / "tools", TOOLS_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            continue
    fallback = DATA_DIR / "tools"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def is_writable_dir(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def writable_tessdata_dir(tesseract_path=""):
    dest = writable_tools_dir() / "tesseract" / "tessdata"
    dest.mkdir(parents=True, exist_ok=True)
    if tesseract_path:
        native = Path(tesseract_path).parent / "tessdata"
        if native != dest and is_writable_dir(native):
            return native
    return dest


def download_file(url, dest, label=""):
    name = label or dest.name
    log_install(f"正在下载并校验 {name}")
    last = -1
    def progress(read, total):
        nonlocal last
        if total:
            percent = min(100, int(read * 100 / total))
            if percent >= last + 10 or percent == 100:
                last = percent
                log_install(f"正在下载 {name}：{percent}%")
    result = download_verified(url, dest, progress)
    log_install(f"SHA256 校验通过：{name}")
    return result


def download_first(urls, dest, label=""):
    errors = []
    for url in urls:
        try:
            log_install(f"尝试地址：{url}")
            return download_file(url, dest, label=label)
        except Exception as exc:
            errors.append(f"{url} -> {exc}")
            log_install(f"下载失败：{exc}")
    raise RuntimeError("；".join(errors) or "下载失败")


def extract_exes_from_zip(zip_path, dest_dir, names):
    dest_dir.mkdir(parents=True, exist_ok=True)
    found = {}
    wanted = {name.lower() for name in names}
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            filename = Path(info.filename).name.lower()
            if filename not in wanted or filename in found:
                continue
            if info.file_size > 512 * 1024 * 1024:
                raise RuntimeError("解压文件超过安全大小限制")
            target = dest_dir / filename
            log_install(f"正在解压 {Path(info.filename).name}")
            with archive.open(info) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            found[filename] = target
    return found


def run_command_logged(command, timeout=3600, cwd=None):
    log_install("$ " + " ".join(str(part) for part in command))
    kwargs = hidden_popen_kwargs()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        **kwargs,
    )
    lines = []
    timed_out = threading.Event()
    def expire():
        if proc.poll() is None:
            timed_out.set()
            proc.kill()
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                lines.append(line)
                log_install(line[-400:])
        returncode = proc.wait(timeout=timeout)
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(command, timeout)
    except Exception:
        proc.kill()
        raise
    finally:
        timer.cancel()
        proc.stdout.close()
    output = "\n".join(lines)
    return subprocess.CompletedProcess(command, returncode, output, "")


def run_command(command, timeout=3600):
    return run_command_logged(command, timeout=timeout)


def winget_install(package_id):
    if package_id != WINGET_PACKAGES["python"]:
        raise RuntimeError("固定版本下载失败，不再自动降级到未锁定的组件版本，请检查网络后重试")
    winget = shutil.which("winget")
    if not winget:
        raise RuntimeError("未检测到 winget，无法使用系统包管理器安装。")
    return run_command_logged(
        [
            winget,
            "install",
            "--exact",
            "--id",
            package_id,
            "--version",
            "3.12.10",
            "--scope",
            "user",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--disable-interactivity",
        ]
    )


def install_ffmpeg():
    notes = []
    tools = detect_tools(force=True)
    if tools["ffmpeg"]["available"]:
        return _result("ffmpeg", True, 0, f"已可用：{tools['ffmpeg']['path']}")
    dest_dir = writable_tools_dir() / "ffmpeg"
    zip_path = writable_tools_dir() / "ffmpeg-download.zip"
    try:
        download_first(FFMPEG_URLS, zip_path, "ffmpeg")
        found = extract_exes_from_zip(zip_path, dest_dir, {"ffmpeg.exe", "ffprobe.exe"})
        notes.append(f"已解压到 {dest_dir}")
        if "ffmpeg.exe" not in found:
            raise RuntimeError("压缩包里没有 ffmpeg.exe")
    except Exception as exc:
        notes.append(f"直接下载失败：{exc}")
        log_install("改为尝试 winget 安装 ffmpeg")
        try:
            completed = winget_install(WINGET_PACKAGES["ffmpeg"])
            notes.append(completed.stdout[-1500:])
            if completed.returncode != 0:
                return _result("ffmpeg", False, completed.returncode, "\n".join(notes))
        except Exception as winget_exc:
            notes.append(str(winget_exc))
            return _result("ffmpeg", False, -1, "\n".join(notes))
    finally:
        zip_path.unlink(missing_ok=True)
    tools = detect_tools(force=True)
    success = bool(tools["ffmpeg"]["available"])
    return _result("ffmpeg", success, 0 if success else -1, "\n".join(notes))


def tesseract_exe_exists(dest_dir):
    tools = detect_tools(force=True)
    if tools["tesseract"].get("available"):
        return True
    return False


def run_tesseract_setup(setup, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = str(dest_dir)
    commands = [
        [str(setup), "/S", f"/D={dest}"],
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", f"/DIR={dest}"],
    ]
    for command in commands:
        log_install("执行安装程序：" + " ".join(command))
        try:
            completed = run_hidden(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            log_install(f"安装程序退出码 {completed.returncode}")
            if completed.stdout:
                log_install(completed.stdout[-500:])
            if completed.stderr:
                log_install(completed.stderr[-500:])
        except Exception as exc:
            log_install(f"安装程序执行失败：{exc}")
            continue
        if tesseract_exe_exists(dest_dir):
            return True
    return tesseract_exe_exists(dest_dir)


def download_language_files(languages, tessdata, notes):
    for lang in languages:
        dest = tessdata / f"{lang}.traineddata"
        if dest.exists():
            notes.append(f"语言包 {lang} 已存在")
            continue
        try:
            download_first(TESSDATA_URLS[lang], dest, f"{lang} 语言包")
            notes.append(f"已下载语言包 {lang}")
        except Exception as exc:
            notes.append(f"语言包 {lang} 下载失败：{exc}")
            log_install(f"语言包 {lang} 下载失败：{exc}")


def install_tesseract():
    notes = []
    dest_dir = writable_tools_dir() / "tesseract"
    tools = detect_tools(force=True)
    if not tools["tesseract"]["available"]:
        setup = writable_tools_dir() / "tesseract-setup.exe"
        try:
            download_first(TESSERACT_SETUP_URLS, setup, "Tesseract 安装程序")
            log_install(f"正在静默安装 Tesseract 到 {dest_dir}", status=True)
            installed = run_tesseract_setup(setup, dest_dir)
            notes.append(f"静默安装{'成功' if installed else '未完成'}")
        except Exception as exc:
            notes.append(f"直接安装失败：{exc}")
            log_install(f"直接安装失败：{exc}")
            installed = False
        finally:
            setup.unlink(missing_ok=True)
        if not tesseract_exe_exists(dest_dir):
            log_install("改为尝试 winget 安装 Tesseract")
            try:
                completed = winget_install(WINGET_PACKAGES["tesseract"])
                notes.append(completed.stdout[-1500:])
            except Exception as exc:
                notes.append(str(exc))
                log_install(str(exc))
        tools = detect_tools(force=True)
        if not tesseract_exe_exists(dest_dir):
            return _result("tesseract", False, -1, "\n".join(part for part in notes if part) or "Tesseract 安装失败")
    tools = detect_tools(force=True)
    tessdata = writable_tessdata_dir(tools["tesseract"].get("path") or str(dest_dir / "tesseract.exe"))
    missing = [lang for lang in CORE_TESSERACT_LANGS if not tools["tesseract"].get(lang)]
    if missing:
        log_install(f"正在下载缺失语言包：{', '.join(missing)}", status=True)
        download_language_files(missing, tessdata, notes)
    tools = detect_tools(force=True)
    has_exe = bool(tools["tesseract"]["available"])
    missing_after = [lang for lang in CORE_TESSERACT_LANGS if not tools["tesseract"].get(lang)]
    complete = has_exe and not missing_after
    if has_exe and missing_after:
        notes.append("程序已安装，但语言包仍缺失：" + ", ".join(missing_after))
    return _result(
        "tesseract",
        complete,
        0 if complete else -1,
        "\n".join(part for part in notes if part) or ("Tesseract 及语言包已就绪" if complete else "Tesseract 安装未完成"),
    )


def install_language_pack(component, language):
    notes = []
    tools = detect_tools(force=True)
    if not tools["tesseract"]["available"]:
        base_result = install_tesseract()
        notes.append(base_result.get("output") or "")
        if not base_result["success"]:
            return _result(component, False, base_result.get("returncode", -1), "\n".join(part for part in notes if part))
        tools = detect_tools(force=True)
    tessdata = writable_tessdata_dir(tools["tesseract"].get("path") or "")
    destination = tessdata / f"{language}.traineddata"
    if not destination.exists():
        download_first(TESSDATA_URLS[language], destination, f"{language} 语言包")
        notes.append(f"已下载语言包 {language} 到 {destination}")
    tools = detect_tools(force=True)
    success = bool(tools["tesseract"].get(language))
    return _result(component, success, 0 if success else -1, "\n".join(part for part in notes if part) or ("语言包已就绪" if success else "语言包安装未完成"))


def install_yt_dlp():
    dest = writable_tools_dir() / "yt-dlp.exe"
    notes = []
    try:
        download_file(YT_DLP_URL, dest, "yt-dlp")
        notes.append(f"已下载 yt-dlp 到 {dest}")
    except Exception as exc:
        notes.append(f"直接下载失败：{exc}")
        try:
            completed = winget_install(WINGET_PACKAGES["yt_dlp"])
            notes.append(completed.stdout[-1500:])
            if completed.returncode != 0 and not dest.exists():
                return _result("yt_dlp", False, completed.returncode, "\n".join(notes))
        except Exception as winget_exc:
            notes.append(str(winget_exc))
            return _result("yt_dlp", False, -1, "\n".join(notes))
    tools = detect_tools(force=True)
    success = bool(tools["yt_dlp"].get("available"))
    return _result("yt_dlp", success, 0 if success else -1, "\n".join(notes))


def python_candidates():
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    found = []
    if not getattr(sys, "frozen", False):
        found.append(sys.executable)
    for name in ("python", "python3"):
        which = shutil.which(name)
        if which:
            found.append(which)
    if local.exists():
        for child in sorted(local.glob("Python3*"), reverse=True):
            exe = child / "python.exe"
            if exe.exists():
                found.append(str(exe))
    unique = []
    seen = set()
    for item in found:
        key = str(item).lower()
        if (key in seen or Path(item).name.lower() == "fbpostcollector.exe"
                or "windowsapps" in key.replace("\\", "/").split("/")):
            continue
        seen.add(key)
        unique.append(item)
    return unique


def usable_python(command):
    """Probe the interpreter, not just the presence of a Windows alias executable."""
    try:
        completed = run_hidden(
            command + ["-I", "-c",
                       "import sys, struct, venv, ensurepip; "
                       "print('FB_PYTHON_OK' if (3, 10) <= sys.version_info[:2] <= (3, 12) "
                       "and struct.calcsize('P') == 8 else 'unsupported')"],
            capture_output=True, text=True, timeout=15,
        )
        return completed.returncode == 0 and completed.stdout.strip() == "FB_PYTHON_OK"
    except (OSError, subprocess.SubprocessError):
        return False


def find_usable_python():
    for candidate in python_candidates():
        command = [candidate]
        if usable_python(command):
            return command
        log_install(f"跳过不可用或不兼容的 Python：{candidate}")
    py = shutil.which("py")
    if py:
        for version in ("-3.12", "-3.11", "-3.10"):
            if usable_python([py, version]):
                return [py, version]
    return None


def ensure_python():
    command = find_usable_python()
    if command:
        return command
    log_install("未找到可用的 64 位 Python 3.10–3.12（已忽略商店占位程序），正在自动安装 Python 3.12", status=True)
    try:
        completed = winget_install(WINGET_PACKAGES["python"])
        log_install(f"Python 安装程序退出码：{completed.returncode}")
    except Exception as exc:
        log_install(f"Python 自动安装失败：{exc}")
    command = find_usable_python()
    if command:
        return command
    log_install("系统安装方式不可用，改用 Python 官方安装包（无需预装 Python）", status=True)
    try:
        setup = writable_tools_dir() / "python-3.12.10-amd64.exe"
        download_file(PYTHON_SETUP_URL, setup, "Python 3.12 64 位安装包")
        completed = run_command_logged([
            str(setup), "/quiet", "/norestart", "InstallAllUsers=0",
            "Include_pip=1", "Include_launcher=0", "Include_test=0",
            "PrependPath=0", "/log", str(DATA_DIR / "python_setup.log"),
        ], timeout=900)
        log_install(f"Python 官方安装程序退出码：{completed.returncode}；详细日志：{DATA_DIR / 'python_setup.log'}")
        command = find_usable_python()
        if command:
            return command
    except Exception as exc:
        log_install(f"Python 官方安装失败：{exc}")
    raise RuntimeError(
        "没有可用的 Python，无法安装 Whisper。请从 https://www.python.org/downloads/windows/ "
        "安装 Python 3.12（64 位，包含 pip），然后重新打开软件并点击安装。"
        "WindowsApps/python.exe 是商店占位程序，不是已安装的 Python。"
    )


def install_whisper():
    notes = []
    tools = detect_tools(force=True)
    if tools["whisper"]["available"]:
        return _result("whisper", True, 0, f"已可用：{tools['whisper']['path']}")
    python_command = ensure_python()
    venv_dir = writable_tools_dir() / "whisper-venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    if not usable_python([str(venv_python)]):
        log_install(f"正在创建 Whisper 虚拟环境：{venv_dir}")
        completed = run_command_logged(python_command + ["-m", "venv", str(venv_dir)], timeout=300)
        notes.append(completed.stdout[-800:])
        if completed.returncode != 0 or not venv_python.exists():
            return _result("whisper", False, completed.returncode, "创建虚拟环境失败\n" + "\n".join(notes))
    log_install("正在安装 openai-whisper（含 PyTorch，可能需要几分钟，请保持网络畅通）")
    completed = run_command_logged([str(venv_python), "-m", "pip", "install", "--upgrade", PIP_REQUIREMENT], timeout=300)
    notes.append(completed.stdout[-800:])
    if completed.returncode != 0:
        return _result("whisper", False, completed.returncode, "pip 准备失败，请检查网络后重试\n" + "\n".join(notes))
    completed = run_command_logged([str(venv_python), "-m", "pip", "install", "--upgrade", WHISPER_REQUIREMENT], timeout=3600)
    notes.append(completed.stdout[-1500:])
    tools = detect_tools(force=True)
    success = completed.returncode == 0 and bool(tools["whisper"]["available"])
    if not success:
        notes.append("Whisper 未通过运行检测，请检查上述安装输出后重试。")
    return _result("whisper", success, 0 if success else completed.returncode, "\n".join(part for part in notes if part))


def _result(component, success, returncode, output):
    return {
        "component": component,
        "label": COMPONENT_LABELS[component],
        "success": bool(success),
        "returncode": int(returncode or 0),
        "output": output or "",
    }


def tail_output(stdout, stderr, limit=4000):
    text = "\n".join(part.strip() for part in (stdout or "", stderr or "") if part.strip())
    return text[-limit:]


def append_install_log(result):
    entry = {"time": now_text(), **result}
    try:
        with INSTALL_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
