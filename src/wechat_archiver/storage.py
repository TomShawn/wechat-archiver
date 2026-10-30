import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .models import ArchiveError


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def directory_name(title: str, key: str) -> str:
    safe = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", title).strip(" .")[:60] or "未命名文章"
    return f"{safe}--{key[:12]}"


def atomic_write(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def link_or_copy(source, target):
    # 归档图片按内容命名且写入后不再修改，可安全共享同一份数据；跨文件系统时退回复制。
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def commit_directory(staging: Path, target: Path):
    # 每次处理创建独立版本；旧版本保持可读，SQLite 最后切换到新目录。
    # 避免覆盖非空目录造成崩溃时旧文件丢失。
    if target.exists():
        raise ArchiveError(f"目标目录已存在：{target}")
    os.replace(staging, target)


def remove_staging(path: Path):
    if path.exists():
        shutil.rmtree(path)


@contextmanager
def archive_lock(database_path: Path):
    import fcntl
    lock_path = database_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError("另一个归档进程正在使用此数据库，请待其完成后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

