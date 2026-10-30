import pytest

from wechat_archiver.inputs import parse_url_batch
from wechat_archiver.models import ArchiveError

GOOD = "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=a"
SIGNED = "http://mp.weixin.qq.com/s?src=11&timestamp=1&ver=7053&signature=abc"


def test_rejects_url_broken_by_space():
    batch = parse_url_batch(f"{GOOD}\n{SIGNED}N19IL * 6Ve7NB*sLUH&new=1\n")
    assert batch.urls == [GOOD]
    assert batch.rejected == [{"line": 2, "url": SIGNED + "N19IL", "reason": "链接中间含空格，已被截断；请重新复制完整链接"}]


def test_rejects_space_before_query_tail():
    batch = parse_url_batch(f"{GOOD}\n{SIGNED[:-4]} abc&new=1\n")
    assert batch.urls == [GOOD] and len(batch.rejected) == 1


@pytest.mark.parametrize("line", [
    "- [标题](https://mp.weixin.qq.com/s/abc) 2026-10-01",
    "| https://mp.weixin.qq.com/s/abc | 标题 = 示例 |",
    "https://mp.weixin.qq.com/s/abc 这篇讲了 a=b 的用法",
    "https://mp.weixin.qq.com/s/abc https://mp.weixin.qq.com/s/def",
])
def test_normal_lines_are_not_rejected(line):
    batch = parse_url_batch(line)
    assert batch.rejected == [] and batch.urls


def test_only_broken_urls_reports_count():
    with pytest.raises(ArchiveError, match="1 个链接中间含空格"):
        parse_url_batch(f"{SIGNED} * 6Ve&new=1")
