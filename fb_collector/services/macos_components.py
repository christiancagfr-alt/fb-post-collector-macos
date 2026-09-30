"""Install optional macOS components without requiring system Python."""
import shutil
from pathlib import Path


def install_component(component):
    from .component_installer import (
        LANGUAGE_COMPONENTS, CORE_TESSERACT_LANGS, _result,
        download_language_files, run_command_logged, writable_tools_dir,
    )
    from .environment import detect_tools

    packages = {"tesseract": "tesseract", "ffmpeg": "ffmpeg", "yt_dlp": "yt-dlp", "whisper": "python@3.12"}
    if component not in packages and component not in LANGUAGE_COMPONENTS:
        raise RuntimeError(f"不支持的组件：{component}")
    brew = shutil.which("brew")
    if not brew:
        return _result(component, False, 1, "Mac 组件安装需要 Homebrew。请先按照 https://brew.sh 的说明安装 Homebrew，然后重新打开软件重试。无需预装 Python。")
    package = packages.get(component, "tesseract")
    completed = run_command_logged([brew, "install", package], timeout=3600)
    if completed.returncode:
        return _result(component, False, completed.returncode, completed.stdout[-4000:])
    notes = []
    if component == "tesseract" or component in LANGUAGE_COMPONENTS:
        languages = list(CORE_TESSERACT_LANGS)
        if component in LANGUAGE_COMPONENTS:
            languages.append(LANGUAGE_COMPONENTS[component])
        dest = writable_tools_dir() / "tesseract" / "tessdata"
        dest.mkdir(parents=True, exist_ok=True)
        download_language_files(languages, dest, notes)
        success = detect_tools(force=True)["tesseract"]["available"] and all((dest / f"{lang}.traineddata").is_file() for lang in languages)
    elif component == "whisper":
        prefix = run_command_logged([brew, "--prefix", "python@3.12"], timeout=30)
        python = Path(prefix.stdout.strip()) / "bin" / "python3.12"
        venv = writable_tools_dir() / "whisper-venv"
        commands = [[str(python), "-m", "venv", str(venv)],
                    [str(venv / "bin/python"), "-m", "pip", "install", "--upgrade", "pip", "openai-whisper"]]
        for command in commands:
            completed = run_command_logged(command, timeout=3600)
            if completed.returncode:
                return _result(component, False, completed.returncode, completed.stdout[-4000:])
        success = detect_tools(force=True)[component]["available"]
    else:
        success = detect_tools(force=True)[component]["available"]
    return _result(component, success, 0 if success else 1, "\n".join(notes) or ("安装并检测成功" if success else "安装后运行检测失败，请查看安装日志"))
