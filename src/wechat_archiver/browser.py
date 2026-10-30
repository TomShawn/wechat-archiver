import json
import logging
import shutil
import subprocess
import time
import uuid

from .models import AccessBlocked, ArchiveError, UnavailableArticle
from .parser import unavailable_article

logger = logging.getLogger(__name__)

READ_PAGE = ("JSON.stringify({html:document.documentElement.outerHTML,url:location.href,"
             "text:document.body.innerText,ready:!!document.querySelector('#js_content')?.textContent.trim()})")


class BrowserFetcher:
    def __init__(self, config):
        self.config = config
        self.session = "wa-" + uuid.uuid4().hex[:12]
        self.blocked = False
        self.started = False

    def command(self, *args):
        if not shutil.which("agent-browser"):
            raise ArchiveError("请先安装 agent-browser 及其浏览器运行时")
        try:
            result = subprocess.run(["agent-browser", "--session", self.session, *args],
                                    capture_output=True, text=True, timeout=self.config.timeout + 15)
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired 不是 OSError，不转换会中断整个批次。
            raise ArchiveError(f"浏览器命令超时：{args[0]}") from exc
        if result.returncode:
            raise ArchiveError(result.stderr[:500] or "浏览器命令失败")
        return result.stdout

    def get(self, url, **kwargs):
        if self.blocked:
            raise AccessBlocked("浏览器遇到验证，当前批次已停止网络访问")
        self.started = True
        for attempt in range(self.config.retries + 1):
            try:
                return self.load(url)
            except (AccessBlocked, UnavailableArticle):
                raise
            except ArchiveError as exc:
                if attempt == self.config.retries:
                    raise
                logger.warning("browser retry attempt=%s reason=%s", attempt + 1, exc)
                time.sleep(self.config.request_interval)
        raise ArchiveError("浏览器没有取得正文")

    def load(self, url):
        self.command("open", url)
        data = {"text": ""}
        for _ in range(10):
            try:
                response = json.loads(self.command("eval", READ_PAGE, "--json"))
                if not response.get("success"):
                    raise ArchiveError("浏览器读取失败")
                data = json.loads(response["data"]["result"])
            except (ValueError, KeyError, TypeError) as exc:
                raise ArchiveError(f"浏览器返回无法解析：{exc}") from exc
            if "captcha" in data["url"] or (not data["ready"] and any(
                    marker in data["text"] for marker in ("验证码", "环境异常", "访问受限", "操作频繁"))):
                self.blocked = True
                raise AccessBlocked("浏览器出现验证或访问限制，请人工处理后重新运行")
            if not data["ready"] and (error := unavailable_article(data["text"])):
                raise error
            if data["ready"]:
                raw = data["html"].encode()
                if len(raw) > self.config.max_html_bytes:
                    raise ArchiveError("页面超过大小限制")
                time.sleep(self.config.request_interval)
                return raw, "text/html", data["url"]
            time.sleep(1)
        raise ArchiveError("浏览器没有取得正文：" + data["text"][:150])

    def close(self):
        if self.started:
            try:
                self.command("close")
            except Exception:
                pass
