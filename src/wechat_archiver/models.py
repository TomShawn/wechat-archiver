from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


class ArchiveError(Exception):
    """可以直接向用户展示的归档错误。"""


class AccessBlocked(ArchiveError):
    """明确限制访问；当前批次不得继续请求同一主机。"""


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

