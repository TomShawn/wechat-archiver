from pathlib import Path

from wechat_archiver.archive import Archiver
from wechat_archiver.urls import article_id

ARTICLE = """<html><body><h1 id="activity-name">远程文章</h1><a id="js_name">号</a>
<em id="publish_time">2026-10-01</em>
<div id="js_content"><p>{body}</p></div>
<script>{script}</script></body></html>"""


class FakeFetcher:
    def __init__(self, config, pages):
        self.config = config
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.pages[url].encode(), "text/html", url

    def close(self):
        pass


def page(body="第一版正文内容。", script=""):
    return ARTICLE.format(body=body, script=script)


def archive(config, fetcher, urls, **options):
    archiver = Archiver(config, fetcher=fetcher)
    try:
        summary = archiver.run([{"kind": "url", "url": url} for url in urls], **options)
        rows = {url: archiver.database.get(article_id(url)) for url in urls}
        return summary, rows
    finally:
        archiver.close()


def body_of(row):
    return (Path(row["local_path"]) / "index.md").read_text("utf-8")


URL = "https://mp.weixin.qq.com/s/first"


def test_force_reuses_cached_page(config):
    fetcher = FakeFetcher(config, {URL: page("第一版正文内容。")})
    archive(config, fetcher, [URL])
    fetcher.pages[URL] = page("第二版正文内容。")
    summary, rows = archive(config, fetcher, [URL], force=True)
    assert summary.succeeded == 1
    assert fetcher.calls == [URL]
    assert "第一版" in body_of(rows[URL])


def test_refetch_requests_page_again(config):
    fetcher = FakeFetcher(config, {URL: page("第一版正文内容。")})
    archive(config, fetcher, [URL])
    fetcher.pages[URL] = page("第二版正文内容。")
    summary, rows = archive(config, fetcher, [URL], refetch=True)
    assert summary.succeeded == 1
    assert fetcher.calls == [URL, URL]
    assert "第二版" in body_of(rows[URL])


def test_refetch_failure_keeps_previous_cache_and_archive(config):
    fetcher = FakeFetcher(config, {URL: page("第一版正文内容。")})
    _, first = archive(config, fetcher, [URL])
    fetcher.pages[URL] = "<html><body>环境异常，请完成验证</body></html>"
    summary, rows = archive(config, fetcher, [URL], refetch=True)
    assert summary.failed == 1
    assert rows[URL]["status"] == "success" and rows[URL]["local_path"] == first[URL]["local_path"]
    # 抓取结果无效时不覆盖已缓存的页面，之后 --force 仍可用旧页面重新处理。
    summary, rows = archive(config, fetcher, [URL], force=True)
    assert summary.succeeded == 1 and "第一版" in body_of(rows[URL])


def test_same_article_with_different_dynamic_markup_is_duplicate(config):
    other = "https://mp.weixin.qq.com/s/second"
    fetcher = FakeFetcher(config, {
        URL: page(script="var biz = 'MzA'; var mid = '100'; var idx = '1'; var now = 1;"),
        other: page(script="var biz = 'MzA'; var mid = '100'; var idx = '1'; var now = 2;"),
    })
    summary, rows = archive(config, fetcher, [URL, other])
    assert (summary.succeeded, summary.skipped) == (1, 1)
    assert rows[other]["status"] == "duplicate"
    assert rows[other]["local_path"] == rows[URL]["local_path"]
    assert rows[other]["content_hash"] != rows[URL]["content_hash"]


def test_identity_without_wechat_ids_uses_text(config):
    other = "https://mp.weixin.qq.com/s/second"
    fetcher = FakeFetcher(config, {URL: page(script="var a = 1;"), other: page(script="var a = 2;")})
    _, rows = archive(config, fetcher, [URL, other])
    assert rows[other]["status"] == "duplicate"


def test_different_articles_are_not_duplicates(config):
    other = "https://mp.weixin.qq.com/s/second"
    fetcher = FakeFetcher(config, {URL: page("甲文章正文。"), other: page("乙文章正文。")})
    summary, _ = archive(config, fetcher, [URL, other])
    assert summary.succeeded == 2


ORIGINAL = "https://mp.weixin.qq.com/s?__biz=MzkxNA%3D%3D&mid=1&idx=1&sn=x"
SHARE = f"""<html><body><h1 id="activity-name">转发</h1><div id="js_share_content">
<div id="js_content"><p>[图片]摘要prepar...</p></div>
<span id="js_share_source" data-url="{ORIGINAL.replace('&', '&amp;')}#wechat_redirect">阅读全文</span>
</div></body></html>"""
EXPIRED = "<html><body><div class='weui-msg'>链接已过期</div></body></html>"


def test_share_card_archives_original(config):
    fetcher = FakeFetcher(config, {URL: SHARE, ORIGINAL: page("原文完整正文。")})
    summary, rows = archive(config, fetcher, [URL])
    assert summary.succeeded == 1
    assert fetcher.calls == [URL, ORIGINAL]
    row = rows[URL]
    assert row["source_url"] == ORIGINAL
    document = body_of(row)
    assert "原文完整正文" in document and "摘要prepar" not in document
    assert "分享卡片" in document


def test_share_card_reprocess_uses_both_caches(config):
    fetcher = FakeFetcher(config, {URL: SHARE, ORIGINAL: page("原文完整正文。")})
    archive(config, fetcher, [URL])
    summary, rows = archive(config, fetcher, [URL], force=True)
    assert summary.succeeded == 1 and fetcher.calls == [URL, ORIGINAL]
    assert "分享卡片" in body_of(rows[URL])


def test_share_card_and_original_are_duplicates(config):
    fetcher = FakeFetcher(config, {URL: SHARE, ORIGINAL: page("原文完整正文。")})
    summary, rows = archive(config, fetcher, [ORIGINAL, URL])
    assert (summary.succeeded, summary.skipped) == (1, 1)
    assert rows[URL]["status"] == "duplicate"


def test_expired_link_is_unavailable_and_not_retried(config):
    from wechat_archiver.database import Database
    fetcher = FakeFetcher(config, {URL: EXPIRED})
    summary, rows = archive(config, fetcher, [URL])
    assert summary.failed == 1
    assert rows[URL]["status"] == "unavailable"
    assert "__biz" in summary.errors[0]["suggestion"]
    database = Database(config.path("database"))
    try:
        assert database.rows(failed_only=True) == []
    finally:
        database.close()


def test_expired_share_original_is_unavailable(config):
    fetcher = FakeFetcher(config, {URL: SHARE, ORIGINAL: EXPIRED})
    _, rows = archive(config, fetcher, [URL])
    assert rows[URL]["status"] == "unavailable"
