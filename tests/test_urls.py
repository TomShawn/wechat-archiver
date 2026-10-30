import pytest

from wechat_archiver.models import ArchiveError
from wechat_archiver.urls import article_id, canonical_url, validate_url


def test_article_id_ignores_tracking_parameters():
    base = "https://mp.weixin.qq.com/s?__biz=MzA&mid=100&idx=1&sn=abc"
    assert article_id(base) == article_id(base + "&chksm=xyz&scene=21#wechat_redirect")
    assert article_id(base) == article_id("http://mp.weixin.qq.com/s?idx=1&mid=100&__biz=MzA")


def test_article_id_differs_by_idx():
    assert article_id("https://mp.weixin.qq.com/s?__biz=MzA&mid=100&idx=1") != \
        article_id("https://mp.weixin.qq.com/s?__biz=MzA&mid=100&idx=2")


def test_canonical_short_link_kept():
    assert canonical_url("https://mp.weixin.qq.com/s/AbC123") == "https://mp.weixin.qq.com/s/AbC123"


@pytest.mark.parametrize("url", [
    "https://example.com/s/abc",
    "https://mp.weixin.qq.com/mp/profile",
    "ftp://mp.weixin.qq.com/s/abc",
    "https://user:pw@mp.weixin.qq.com/s/abc",
])
def test_rejects_non_article_urls(url):
    with pytest.raises(ArchiveError):
        validate_url(url, article=True)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.png", "http://10.0.0.1/a.png", "http://localhost/a.png", "http://example.com:8080/a.png",
])
def test_rejects_private_hosts(url):
    with pytest.raises(ArchiveError):
        validate_url(url)
