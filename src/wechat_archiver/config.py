from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import ArchiveError


@dataclass
class Config:
    root: Path
    # Directly constructed Config objects stay rooted locally (useful for isolated tests).
    # load_config() supplies external runtime defaults for normal CLI execution.
    output_dir: str = "output"
    database: str = "data/archive.db"
    search_results_dir: str = "search_results"
    log_dir: str = "logs"
    reports_dir: str = "reports"
    batch_output_dir: str = ".."
    timeout: float = 20
    request_interval: float = 2
    image_request_interval: float = 0.3
    image_workers: int = 4
    retries: int = 1
    max_html_bytes: int = 15 * 1024 * 1024
    max_image_bytes: int = 20 * 1024 * 1024
    ocr: bool = True
    ocr_min_confidence: float = 0.65
    search_cache_hours: float = 24

    def path(self, key: str) -> Path:
        return (self.root / getattr(self, key)).resolve()


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    values = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ArchiveError("配置文件必须是 YAML 对象")
    allowed = set(Config.__dataclass_fields__) - {"root"}
    if set(values) - allowed:
        raise ArchiveError(f"未知配置项：{', '.join(sorted(set(values) - allowed))}")
    runtime_path_defaults = {
        "output_dir": "../wechat-archiver-data/output",
        "database": "../wechat-archiver-data/data/archive.db",
        "search_results_dir": "../wechat-archiver-data/search_results",
        "log_dir": "../wechat-archiver-data/logs",
        "reports_dir": "../wechat-archiver-data/reports",
        "batch_output_dir": "..",
    }
    config = Config(root=path.parent, **{**runtime_path_defaults, **values})
    for key in ("output_dir", "database", "search_results_dir", "log_dir", "reports_dir", "batch_output_dir"):
        if not isinstance(getattr(config, key), str) or not getattr(config, key).strip():
            raise ArchiveError(f"{key} 必须是非空路径字符串")
    for key in ("timeout", "max_html_bytes", "max_image_bytes"):
        if not isinstance(getattr(config, key), (int, float)) or getattr(config, key) <= 0:
            raise ArchiveError(f"{key} 必须大于 0")
    if not isinstance(config.request_interval, (int, float)) or not isinstance(config.retries, int):
        raise ArchiveError("request_interval 和 retries 必须是数字")
    if config.request_interval < 0 or not 0 <= config.retries <= 3:
        raise ArchiveError("请求间隔不得为负数，retries 必须在 0～3 之间")
    if not isinstance(config.image_request_interval, (int, float)) or config.image_request_interval < 0:
        raise ArchiveError("image_request_interval 必须是非负数字")
    if not isinstance(config.image_workers, int) or not 1 <= config.image_workers <= 8:
        raise ArchiveError("image_workers 必须是 1～8 之间的整数")
    if not isinstance(config.ocr, bool):
        raise ArchiveError("ocr 必须是 true 或 false")
    if not isinstance(config.ocr_min_confidence, (int, float)) or not 0 <= config.ocr_min_confidence <= 1:
        raise ArchiveError("ocr_min_confidence 必须在 0～1 之间")
    if not isinstance(config.search_cache_hours, (int, float)) or config.search_cache_hours < 0:
        raise ArchiveError("search_cache_hours 必须是非负数字")
    return config

