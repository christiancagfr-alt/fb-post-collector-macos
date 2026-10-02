"""Download ffmpeg and yt-dlp into ./tools so they ship inside the packaged app."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from fb_collector.services.component_manifest import YT_DLP_URL, FFMPEG_URLS
from fb_collector.services.verified_download import download_verified


ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"


def download(url: str, dest: Path) -> None:
    print(f"downloading {url}")
    download_verified(url, dest)
    print(f"SHA256 verified: {dest}")


def ensure_yt_dlp() -> None:
    dest = TOOLS / "yt-dlp.exe"
    download(YT_DLP_URL, dest)


def ensure_ffmpeg() -> None:
    dest_dir = TOOLS / "ffmpeg"
    exe = dest_dir / "ffmpeg.exe"
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
                        if info.file_size > 512 * 1024 * 1024:
                            raise RuntimeError("Oversized executable in verified archive")
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
