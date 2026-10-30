import pytest

from wechat_archiver.models import ArchiveError, SharedArticle, UnavailableArticle
from wechat_archiver.parser import parse_article, parse_date

ARTICLE = """
<html><head><meta property="og:title" content="备用标题"></head><body>
<h1 id="activity-name"> 测试文章 </h1>
<a id="js_name">测试公众号</a>
<em id="publish_time">2026年10月28日 16:30</em>
<div id="js_content">
  <p>正文第一段。</p>
  <img data-src="https://mmbiz.qpic.cn/a.png"><img class="rich_pages wxw-img">
  <section class="code-snippet__fix"><ul>
    <li class="code-snippet__line">SELECT&nbsp;1;</li><li class="code-snippet__line">SELECT 2;</li>
  </ul></section>
  <script>alert(1)</script>
</div>
<script>var ct = "1790000000";</script>
</body></html>
"""


def test_parses_metadata_and_cleans_body():
    article = parse_article(ARTICLE.encode(), "https://mp.weixin.qq.com/s/x")
    assert article.title == "测试文章"
    assert article.account == "测试公众号"
    assert article.published_at == "2026-10-28"
    assert article.date_evidence.startswith("#publish_time")
    assert "<script" not in article.html
    assert article.html.count("<img") == 1
    assert "<pre><code>SELECT 1;\nSELECT 2;</code></pre>" in article.html


def test_missing_content_container_raises():
    with pytest.raises(ArchiveError):
        parse_article("<html><title>微信公众平台</title><body>请完成验证</body></html>".encode())


def test_local_import_falls_back_to_article_tag():
    article = parse_article(b"<h1>Local</h1><article><p>hello</p></article>", local=True)
    assert article.title == "Local" and "hello" in article.html


@pytest.mark.parametrize("value,expected", [
    ("2026-10-28", "2026-10-28"), ("2026年1月2日", "2026-01-02"), ("2026/02/30", None), (None, None),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected


SHARE_CARD = """<html><body><h1 id="activity-name">转发标题</h1>
<div id="js_share_content" class="share_media"><div class="original_panel">
<div id="js_content"><p>[图片]摘要内容prepar...</p></div></div>
<span id="js_share_source" data-url="http://mp.weixin.qq.com/s?__biz=MzkxNA%3D%3D&amp;mid=1&amp;idx=1&amp;sn=x#wechat_redirect">阅读全文</span>
</div></body></html>"""


def test_share_card_points_to_original():
    with pytest.raises(SharedArticle) as excinfo:
        parse_article(SHARE_CARD.encode())
    assert excinfo.value.original_url == "http://mp.weixin.qq.com/s?__biz=MzkxNA%3D%3D&mid=1&idx=1&sn=x"


def test_share_card_without_link_is_error():
    page = SHARE_CARD.replace('id="js_share_source"', 'id="other"')
    with pytest.raises(ArchiveError) as excinfo:
        parse_article(page.encode())
    assert not isinstance(excinfo.value, SharedArticle)


@pytest.mark.parametrize("marker", ["链接已过期", "该内容已被发布者删除", "系统出错"])
def test_unavailable_pages(marker):
    with pytest.raises(UnavailableArticle) as excinfo:
        parse_article(f"<html><body><div class='weui-msg'>{marker}</div></body></html>".encode())
    assert marker in str(excinfo.value) and excinfo.value.suggestion


def test_article_discussing_expired_links_is_not_unavailable():
    article = parse_article(ARTICLE.replace("正文第一段。", "如果提示链接已过期，请重新复制。").encode())
    assert "链接已过期" in article.html
