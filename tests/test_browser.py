import json
import subprocess

import pytest

from wechat_archiver import browser
from wechat_archiver.browser import BrowserFetcher
from wechat_archiver.models import AccessBlocked, ArchiveError

URL = "https://mp.weixin.qq.com/s/abc"


def page(ready=True, text="正文", url=URL):
    result = json.dumps({"html": "<div id='js_content'>正文</div>", "url": url, "text": text, "ready": ready})
    return json.dumps({"success": True, "data": {"result": result}})


@pytest.fixture
def scripted(monkeypatch, config):
    """按顺序返回预设结果；每项可以是 stdout 字符串或要抛出的异常。"""
    config.retries = 1
    steps, calls = [], []

    def run(command, **kwargs):
        calls.append(command[3])
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return subprocess.CompletedProcess(command, 0, stdout=step, stderr="")

    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/bin/agent-browser")
    monkeypatch.setattr(browser.subprocess, "run", run)
    monkeypatch.setattr(browser.time, "sleep", lambda seconds: None)
    return BrowserFetcher(config), steps, calls


def test_success(scripted):
    fetcher, steps, calls = scripted
    steps += ["", page()]
    raw, mime, url = fetcher.get(URL)
    assert b"js_content" in raw and mime == "text/html" and url == URL
    assert calls == ["open", "eval"]


def test_timeout_becomes_archive_error_and_retries(scripted):
    fetcher, steps, calls = scripted
    steps += [subprocess.TimeoutExpired("agent-browser", 35), "", page()]
    raw, _, _ = fetcher.get(URL)
    assert raw and calls == ["open", "open", "eval"]


def test_gives_up_after_retries(scripted):
    fetcher, steps, _ = scripted
    steps += [subprocess.TimeoutExpired("agent-browser", 35)] * 2
    with pytest.raises(ArchiveError, match="超时"):
        fetcher.get(URL)


def test_unparseable_output_is_retried(scripted):
    fetcher, steps, _ = scripted
    steps += ["", "not json", "", page()]
    raw, _, _ = fetcher.get(URL)
    assert raw


def test_captcha_is_not_retried_and_blocks_batch(scripted):
    fetcher, steps, calls = scripted
    steps += ["", page(ready=False, text="当前环境异常，完成验证后即可继续访问")]
    with pytest.raises(AccessBlocked):
        fetcher.get(URL)
    assert calls == ["open", "eval"]
    with pytest.raises(AccessBlocked):
        fetcher.get(URL)
    assert calls == ["open", "eval"]
