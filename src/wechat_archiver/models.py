from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


class ArchiveError(Exception):
    """可以直接向用户展示的归档错误。"""


class AccessBlocked(ArchiveError):
    """明确限制访问；当前批次不得继续请求同一主机。"""


class UnavailableArticle(ArchiveError):
    """文章本身不可访问（链接过期、已删除等）；重试无意义，retry 也不再处理。"""

    def __init__(self, message, suggestion):
        super().__init__(message)
        self.suggestion = suggestion


class SharedArticle(ArchiveError):
    """“分享一篇文章”页面只有摘要卡片，正文在原文链接中。"""

    def __init__(self, original_url):
        super().__init__(f"这是转发的分享卡片，只含摘要；原文地址：{original_url}")
        self.original_url = original_url
        self.suggestion = "改为归档原文地址"


@dataclass
class Article:
    title: str
    html: str
    account: str | None = None
    author: str | None = None
    published_at: str | None = None
    source_url: str | None = None
    date_evidence: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Summary:
    candidates: int = 0
    verified: int = 0
    selected: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    images_saved: int = 0
    images_downloaded: int = 0
    ocr_processed: int = 0
    errors: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

