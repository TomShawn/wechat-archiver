import logging
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

import certifi
import httpx

from .config import Config
from .models import AccessBlocked, ArchiveError
from .urls import validate_url

logger = logging.getLogger(__name__)

# 单张图片被拒绝不代表整站拦截；同一图片主机连续限流达到此次数才停止本批请求。
IMAGE_THROTTLE_LIMIT = 3


class Fetcher:
    def __init__(self, config: Config, transport=None):
        self.config = config
        self.blocked: set[str] = set()
        self.last_request: dict[str, float] = {}
        self.image_throttled: dict[str, int] = {}
        # 图片并发下载时共享限速状态。
        self.lock = threading.Lock()
        self.client = httpx.Client(
            verify=ssl.create_default_context(cafile=certifi.where()),
            timeout=config.timeout, follow_redirects=False, transport=transport,
            headers={"User-Agent": "wechat-archiver/0.1 (personal local archive)",
                     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"},
        )

    def close(self):
        self.client.close()

    def block(self, url: str, reason: str):
        host = urlsplit(url).hostname or ""
        with self.lock:
            self.blocked.add(host)
        raise AccessBlocked(f"{host}：{reason}；已停止当前批次对该主机的请求，请在浏览器查看并手动保存 HTML")

    def wait_turn(self, host: str, interval: float):
        with self.lock:
            current = time.monotonic()
            slot = max(current, self.last_request.get(host, 0) + interval)
            self.last_request[host] = slot
        if slot > current:
            time.sleep(slot - current)

    def reject_image(self, url: str, status: int):
        host = urlsplit(url).hostname or ""
        if status == 429:
            with self.lock:
                self.image_throttled[host] = self.image_throttled.get(host, 0) + 1
                count = self.image_throttled[host]
            if count >= IMAGE_THROTTLE_LIMIT:
                self.block(url, f"图片请求连续 {count} 次 HTTP 429")
        raise ArchiveError(f"图片请求被拒绝：HTTP {status}")

    def get(self, url: str, *, image=False, article=False, allow_private=False) -> tuple[bytes, str, str]:
        url = validate_url(url, article=article, allow_private=allow_private)
        limit = self.config.max_image_bytes if image else self.config.max_html_bytes
        interval = self.config.image_request_interval if image else self.config.request_interval
        for redirect in range(6):
            host = urlsplit(url).hostname or ""
            if host in self.blocked:
                raise AccessBlocked(f"{host} 已在当前批次停止请求；请手动保存 HTML 后导入")
            for attempt in range(self.config.retries + 1):
                self.wait_turn(host, interval)
                try:
                    with self.client.stream("GET", url) as response:
                        if response.status_code in (401, 403, 429):
                            if image:
                                self.reject_image(url, response.status_code)
                            self.block(url, f"HTTP {response.status_code}")
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("location")
                            if not location:
                                raise ArchiveError("重定向缺少 Location")
                            destination = urljoin(url, location)
                            logger.info("redirect host=%s status=%s path=%s", host, response.status_code,
                                        urlsplit(destination).path)
                            if any(marker in urlsplit(destination).path.lower()
                                   for marker in ("captcha", "antispider", "verify")):
                                self.block(url, "重定向至验证码或访问验证页面")
                            url = validate_url(destination, article=article, allow_private=allow_private)
                            break
                        if response.status_code >= 500 and attempt < self.config.retries:
                            continue
                        if response.status_code != 200:
                            raise ArchiveError(f"HTTP {response.status_code}：{url}")
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise ArchiveError(f"响应超过大小限制 {limit} 字节")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if image:
                            with self.lock:
                                self.image_throttled.pop(host, None)
                        else:
                            text = body.decode("utf-8", "replace")
                            # 正常文章可能在正文里讨论验证码，只有缺少正文容器时才检查拦截提示。
                            if 'id="js_content"' not in text and "id='js_content'" not in text and any(
                                    s in text.lower() for s in (
                                        "site unavailable", "antispider", "访问过于频繁", "环境异常",
                                        "访问受限", "完成验证", "验证码", "verifycaptcha")):
                                self.block(url, "响应为访问限制或验证页面")
                        logger.info("GET host=%s status=%s bytes=%s", host, response.status_code, len(body))
                        return body, mime, url
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt == self.config.retries:
                        raise ArchiveError(f"网络请求失败：{type(exc).__name__}：{exc}") from exc
            else:
                raise ArchiveError("请求未取得有效响应")
        raise ArchiveError("重定向次数过多")

