"""候选发现与核验分开。默认仅搜索本地索引，不将零结果伪装成网络搜索成功。"""
import csv
import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .models import AccessBlocked, ArchiveError, Summary, now
from .parser import parse_article, parse_date
from .storage import atomic_write, digest
from .urls import article_id, validate_url

FIELDS = ["selected", "title", "account", "author", "published_at", "summary", "url", "redirect_url",
          "search_source", "first_discovered_at", "last_verified_at", "verification_status",
          "date_evidence", "date_match", "error"]


def candidate(row, source):
    value = {key: row.get(key) or "" for key in FIELDS}
    value["selected"] = "0"
    value["search_source"] = source
    value["first_discovered_at"] = row.get("first_discovered_at") or now()
    # 外部清单提供的 verified 字段本身不构成远程核验证据。
    value["verification_status"] = "unverified"
    value["last_verified_at"] = ""
    value["date_evidence"] = f"reported_by:{source}" if value["published_at"] else ""
    value["published_at"] = parse_date(str(value["published_at"])) or ""
    try:
        value["url"] = validate_url(str(value["url"]), article=True)
    except ArchiveError:
        value["redirect_url"] = str(row.get("redirect_url") or row.get("url") or "")
        value["url"] = ""
    return value


class LocalProvider:
    name = "local"

    def __init__(self, database):
        self.database = database
        self.cache_identity = "local:" + digest(json.dumps(self.database.rows(), ensure_ascii=False).encode())

    def search(self, keyword, page, limit):
        if page != 1:
            return []
        results = []
        for row in self.database.rows():
            if row["status"] not in ("success", "partial"):
                continue
            body = ""
            path = Path(row["local_path"] or "") / "index.md"
            if path.is_file():
                body = path.read_text("utf-8")
            haystack = " ".join(str(row.get(key) or "") for key in ("title", "account")) + " " + body
            if keyword.casefold() in haystack.casefold():
                item = candidate({**row, "url": row["source_url"]}, self.name)
                item["verification_status"] = "local_archive_unverified"
                if row["source_url"]:
                    results.append(item)
        return results[:limit]


class CatalogProvider:
    def __init__(self, path):
        self.path = path.resolve()
        self.name = f"catalog:{self.path.name}:{digest(self.path.read_bytes())[:12]}"

    def search(self, keyword, page, limit):
        if page != 1:
            return []
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        return [candidate(row, self.name) for row in rows if keyword.casefold() in " ".join(
            str(row.get(key, "")) for key in ("title", "summary", "account")
        ).casefold()][:limit]


class SearxProvider:
    """仅连接用户指定且允许 API 查询的实例；不自动挑选公共实例。"""
    def __init__(self, url, fetcher):
        self.url = validate_url(url, allow_private=True).rstrip("/")
        self.fetcher = fetcher
        self.name = "searxng:" + self.url

    def search(self, keyword, page, limit):
        query = urlencode({"q": f"{keyword} site:mp.weixin.qq.com/s", "format": "json", "pageno": page})
        raw, _, _ = self.fetcher.get(self.url + "/search?" + query, allow_private=True)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArchiveError("搜索实例未返回 JSON；请检查实例是否允许自动查询及 JSON 输出") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ArchiveError("搜索实例结果缺少 results 数组")
        results = []
        for row in payload["results"]:
            if not isinstance(row, dict):
                continue
            item = candidate({"title": row.get("title"), "url": row.get("url"),
                              "published_at": row.get("publishedDate"), "summary": row.get("content")}, self.name)
            if item["url"]:
                results.append(item)
        return results[:limit]


def verify(item, fetcher):
    item["last_verified_at"] = now()
    if not item["url"]:
        item["error"] = "只有搜索跳转链接，没有已确认的微信文章地址"
        return
    try:
        raw, _, final_url = fetcher.get(item["url"], article=True)
        article = parse_article(raw, final_url)
        item.update(title=article.title, account=article.account or "", author=article.author or "",
                    published_at=article.published_at or "", date_evidence=article.date_evidence or "",
                    url=final_url, verification_status="body_verified", error="")
    except AccessBlocked as exc:
        item.update(verification_status="blocked", error=str(exc))
    except ArchiveError as exc:
        item.update(verification_status="failed", error=str(exc))


def date_match(item, since, until):
    if not since and not until:
        return "not_requested"
    value = item["published_at"]
    if not value:
        return "unknown"
    within = (not since or value >= since) and (not until or value <= until)
    if not within:
        return "out_of_range"
    return "confirmed_in_range" if item["verification_status"] == "body_verified" else "reported_in_range_unverified"


def search_articles(keywords, providers, database, fetcher, *, limit=10, pages=1, since=None,
                    until=None, account=None, sort="relevance", verify_results=False,
                    no_cache=False, cache_hours=24):
    summary = Summary()
    merged, errors = {}, []
    for keyword in keywords:
        for provider in providers:
            for page in range(1, pages + 1):
                key = digest(json.dumps([getattr(provider, "cache_identity", provider.name), keyword, page, limit],
                                        ensure_ascii=False).encode())
                cached = database.cache_get(key) if not no_cache else None
                try:
                    if cached and datetime.fromisoformat(cached["created_at"]) > (
                        datetime.fromisoformat(now()) - timedelta(hours=cache_hours)
                    ):
                        rows = json.loads(cached["payload"])
                    else:
                        rows = provider.search(keyword, page, limit)
                        database.cache_put(key, rows)
                except (ArchiveError, OSError, ValueError) as exc:
                    errors.append({"source": provider.name, "keyword": keyword, "reason": str(exc)})
                    break
                if not rows:
                    break
                for row in rows:
                    identity = article_id(row["url"]) if row["url"] else row["redirect_url"] or row["title"]
                    if identity in merged:
                        existing = merged[identity]
                        sources = set(existing["search_source"].split("; ")) | {row["search_source"]}
                        existing["search_source"] = "; ".join(sorted(sources))
                        for field in ("account", "author", "published_at", "date_evidence", "summary"):
                            if not existing[field] and row[field]:
                                existing[field] = row[field]
                    else:
                        merged[identity] = row
    summary.candidates = len(merged)
    results = []
    verification_attempts = 0
    for identity, item in merged.items():
        if verify_results and verification_attempts < limit:
            # 主机被限制后，剩余候选仅标记状态，不再发起网络请求。
            if urlsplit(item["url"]).hostname in fetcher.blocked:
                item.update(verification_status="blocked", error="同一主机已在当前批次停止请求")
            else:
                verify(item, fetcher)
                verification_attempts += 1
        summary.verified += int(item["verification_status"] == "body_verified")
        item["date_match"] = date_match(item, since, until)
        database.save_candidate(identity, item)
        if item["date_match"] == "out_of_range":
            continue
        if account and account.casefold() not in item["account"].casefold():
            continue
        results.append(item)
    if sort in ("newest", "oldest"):
        known = sorted([r for r in results if r["published_at"]], key=lambda row: row["published_at"],
                       reverse=sort == "newest")
        results = known + [r for r in results if not r["published_at"]]
    summary.errors = errors + [{"url": item["url"], "reason": item["error"]} for item in results if item["error"]]
    return results[:limit], summary


def write_results(rows, directory):
    directory.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(directory / "results.csv", "\ufeff" + stream.getvalue())

    def cell(value):
        return re.sub(r"[\r\n]+", " ", str(value)).replace("|", "\\|").replace("<", "&lt;")

    lines = ["# 候选文章清单", "", "默认均未选择。将 CSV 中 selected 改为 1 后再执行归档。",
             "搜索日期仅作来源报告；date_match=unknown 或 reported_in_range_unverified 不能视为日期核验通过。",
             "", "| selected | 标题 | 公众号 | 日期 | 核验状态 | 日期匹配 | URL / 跳转链接 |",
             "|---|---|---|---|---|---|---|"]
    for row in rows:
        values = [row["selected"], row["title"], row["account"], row["published_at"],
                  row["verification_status"], row["date_match"], row["url"] or row["redirect_url"]]
        lines.append("| " + " | ".join(map(cell, values)) + " |")
    atomic_write(directory / "results.md", "\n".join(lines) + "\n")

