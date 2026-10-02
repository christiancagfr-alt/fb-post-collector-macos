"""Download Facebook media only, validating every redirect and limiting bytes."""
from urllib.parse import urlsplit, urljoin
import requests
from .task_control import check_cancelled

ROOTS = ("facebook.com", "fbcdn.net", "fbsbx.com", "fb.com", "fb.watch")


def validate_media_url(url):
    parsed = urlsplit(str(url))
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or any(char in str(url) for char in "\r\n\t")
            or not any(host == root or host.endswith("." + root) for root in ROOTS)):
        raise ValueError("媒体地址不属于受信任的 Facebook HTTPS 域名")


def download_media(url, limit):
    for redirect_count in range(6):
        check_cancelled()
        validate_media_url(url)
        with requests.get(url, timeout=(5, 30), stream=True, allow_redirects=False,
                          headers={"User-Agent": "FBPostCollector"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            response.raise_for_status()
            if int(response.headers.get("Content-Length") or 0) > limit:
                raise ValueError("媒体文件超过安全大小限制")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                check_cancelled()
                if len(data) + len(chunk) > limit:
                    raise ValueError("媒体文件超过安全大小限制")
                data.extend(chunk)
            return bytes(data), response.headers.get("Content-Type", "")
    raise ValueError("媒体重定向次数过多")
