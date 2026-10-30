import httpx
import pytest
from conftest import png

from wechat_archiver.fetcher import IMAGE_THROTTLE_LIMIT, Fetcher
from wechat_archiver.models import AccessBlocked, ArchiveError

IMAGE_HOST = "https://mmbiz.qpic.cn"


def fetcher_for(config, handler):
    return Fetcher(config, transport=httpx.MockTransport(handler))


def test_image_403_does_not_block_host(config):
    def handler(request):
        if request.url.path == "/denied.png":
            return httpx.Response(403)
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    fetcher = fetcher_for(config, handler)
    with pytest.raises(ArchiveError) as excinfo:
        fetcher.get(f"{IMAGE_HOST}/denied.png", image=True)
    assert not isinstance(excinfo.value, AccessBlocked)
    assert "mmbiz.qpic.cn" not in fetcher.blocked
    body, mime, _ = fetcher.get(f"{IMAGE_HOST}/ok.png", image=True)
    assert body.startswith(b"\x89PNG") and mime == "image/png"


def test_article_403_blocks_host(config):
    fetcher = fetcher_for(config, lambda request: httpx.Response(403))
    with pytest.raises(AccessBlocked):
        fetcher.get("https://mp.weixin.qq.com/s/abc", article=True)
    assert "mp.weixin.qq.com" in fetcher.blocked
    with pytest.raises(AccessBlocked):
        fetcher.get("https://mp.weixin.qq.com/s/def", article=True)


def test_repeated_image_429_blocks_host(config):
    fetcher = fetcher_for(config, lambda request: httpx.Response(429))
    for _ in range(IMAGE_THROTTLE_LIMIT - 1):
        with pytest.raises(ArchiveError) as excinfo:
            fetcher.get(f"{IMAGE_HOST}/a.png", image=True)
        assert not isinstance(excinfo.value, AccessBlocked)
    with pytest.raises(AccessBlocked):
        fetcher.get(f"{IMAGE_HOST}/a.png", image=True)
    assert "mmbiz.qpic.cn" in fetcher.blocked


def test_successful_image_resets_throttle_count(config):
    responses = iter([429] * (IMAGE_THROTTLE_LIMIT - 1) + [200] + [429] * (IMAGE_THROTTLE_LIMIT - 1))

    def handler(request):
        status = next(responses)
        return httpx.Response(status, content=png() if status == 200 else b"")

    fetcher = fetcher_for(config, handler)
    for _ in range(IMAGE_THROTTLE_LIMIT - 1):
        with pytest.raises(ArchiveError):
            fetcher.get(f"{IMAGE_HOST}/a.png", image=True)
    fetcher.get(f"{IMAGE_HOST}/a.png", image=True)
    for _ in range(IMAGE_THROTTLE_LIMIT - 1):
        with pytest.raises(ArchiveError) as excinfo:
            fetcher.get(f"{IMAGE_HOST}/a.png", image=True)
        assert not isinstance(excinfo.value, AccessBlocked)


def test_wait_turn_spaces_requests_per_host(config, monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr("wechat_archiver.fetcher.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("wechat_archiver.fetcher.time.sleep", sleeps.append)
    fetcher = Fetcher(config)
    for _ in range(3):
        fetcher.wait_turn("mmbiz.qpic.cn", 0.5)
    fetcher.wait_turn("other.example", 0.5)
    # 同一时刻发起的请求依次排到 +0.5、+1.0；不同主机互不影响。
    assert sleeps == [0.5, 1.0]
