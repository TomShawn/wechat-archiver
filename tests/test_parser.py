import pytest

from wechat_archiver.models import ArchiveError
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
