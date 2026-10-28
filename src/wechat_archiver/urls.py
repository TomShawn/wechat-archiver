import hashlib
import ipaddress
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .models import ArchiveError


def validate_url(url: str, *, article: bool = False, allow_private=False) -> str:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise ArchiveError("无效 URL") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ArchiveError("只支持不包含账号密码的 HTTP(S) URL")
    if article and (parts.hostname.lower() != "mp.weixin.qq.com" or parts.path.rstrip("/") != "/s"
                    and not parts.path.startswith("/s/")):
        raise ArchiveError("归档 URL 必须是 mp.weixin.qq.com/s 或 /s/ 文章链接")
    if not allow_private and port not in (None, 80, 443):
        raise ArchiveError("不支持非标准端口")
    host = parts.hostname.lower()
    if not allow_private and (host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".localhost"))):
        raise ArchiveError("不下载本地主机上的网络资源")
    try:
        if not allow_private and not ipaddress.ip_address(host).is_global:
            raise ArchiveError("不下载私有网络资源")
    except ValueError:
        pass
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def canonical_url(url: str) -> str:
    url = validate_url(url, article=True)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if all(query.get(key) for key in ("__biz", "mid", "idx")):
        kept = {key: query[key][0] for key in ("__biz", "mid", "idx", "sn") if query.get(key)}
        return urlunsplit(("https", "mp.weixin.qq.com", "/s", urlencode(kept), ""))
    return urlunsplit(("https", "mp.weixin.qq.com", parts.path, parts.query, ""))


def article_id(url: str) -> str:
    canonical = canonical_url(url)
    parts = urlsplit(canonical)
    query = parse_qs(parts.query)
    key = "|".join(query[k][0] for k in ("__biz", "mid", "idx")) if all(
        query.get(k) for k in ("__biz", "mid", "idx")
    ) else canonical
    return hashlib.sha256(key.encode()).hexdigest()

