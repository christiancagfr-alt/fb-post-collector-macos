"""Fail-closed, bounded, atomic downloads from the reviewed component manifest."""
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
import urllib.request
from urllib.parse import urlsplit
from .component_manifest import SHA256

MAX_BYTES = 512 * 1024 * 1024
REDIRECT_ROOTS = ("github.com", "githubusercontent.com", "python.org")


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not any(host == root or host.endswith("." + root) for root in REDIRECT_ROOTS)):
            raise RuntimeError("组件下载重定向到非受信任地址，已中止")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_verified(url, destination, progress=None):
    expected = SHA256.get(url)
    if not expected or urlsplit(url).scheme != "https":
        raise RuntimeError("组件地址没有固定版本和 SHA256，已禁止下载")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="component-", suffix=".part", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    digest = hashlib.sha256()
    count = 0
    opener = urllib.request.build_opener(HTTPSRedirect())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FBPostCollector"})
        with opener.open(req, timeout=30) as response, temporary.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            if total > MAX_BYTES:
                raise RuntimeError("组件超过安全大小限制")
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_BYTES:
                    raise RuntimeError("组件超过安全大小限制")
                digest.update(chunk)
                handle.write(chunk)
                if progress:
                    progress(count, total)
        if (total and count != total) or not hmac.compare_digest(digest.hexdigest(), expected):
            raise RuntimeError("组件 SHA256 或长度校验失败，已保留原文件并禁止安装")
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
