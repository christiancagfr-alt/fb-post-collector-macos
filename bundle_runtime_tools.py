"""Download ffmpeg and yt-dlp into ./tools so they ship inside the packaged app."""

from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
FFMPEG_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]
USER_AGENT = "Mozilla/5.0 FBPostCollector"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(dest)
    print(f"saved {dest}")


def ensure_yt_dlp() -> None:
    dest = TOOLS / "yt-dlp.exe"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"yt-dlp already present: {dest}")
        return
    download(YT_DLP_URL, dest)


def ensure_ffmpeg() -> None:
    dest_dir = TOOLS / "ffmpeg"
    exe = dest_dir / "ffmpeg.exe"
    if exe.exists() and exe.stat().st_size > 1_000_000:
        print(f"ffmpeg already present: {exe}")
        return
    zip_path = TOOLS / "ffmpeg-download.zip"
    last_error = None
    for url in FFMPEG_URLS:
        try:
            download(url, zip_path)
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path) as archive:
                for info in archive.infolist():
                    name = Path(info.filename).name.lower()
                    if name in {"ffmpeg.exe", "ffprobe.exe"} and not info.is_dir():
                        target = dest_dir / Path(info.filename).name
                        with archive.open(info) as source, target.open("wb") as handle:
                            shutil.copyfileobj(source, handle)
                        print(f"extracted {target}")
            if exe.exists():
                return
            last_error = RuntimeError(f"zip from {url} did not contain ffmpeg.exe")
        except Exception as exc:
            last_error = exc
            print(f"ffmpeg download failed: {exc}")
        finally:
            zip_path.unlink(missing_ok=True)
    raise RuntimeError(f"failed to bundle ffmpeg: {last_error}")


def main() -> None:
    TOOLS.mkdir(parents=True, exist_ok=True)
    ensure_yt_dlp()
    ensure_ffmpeg()
    print("runtime tools ready")


if __name__ == "__main__":
    main()
