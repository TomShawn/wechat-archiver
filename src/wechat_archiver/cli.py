import argparse
import csv
import json
import logging
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from .archive import Archiver
from .config import load_config
from .inputs import parse_url_batch
from .models import ArchiveError, now
from .parser import parse_date
from .storage import write_json


def parser():
    command = argparse.ArgumentParser(prog="wechat-archiver", description="微信公众号文章本地归档；运行时不依赖 AI")
    command.add_argument("--config", type=Path, default=Path("config.yaml"), help="配置文件（路径相对于该文件）")
    command.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = command.add_subparsers(dest="command", required=True)
    archive = sub.add_parser("archive", help="归档 URL 或 CSV 选中项")
    source = archive.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", action="append")
    source.add_argument("--input", type=Path, help="外部工具给出的纯文本或 Markdown 链接列表")
    source.add_argument("--selected", type=Path)
    imported = sub.add_parser("import", help="导入保存的 HTML 或 Markdown")
    source = imported.add_mutually_exclusive_group(required=True)
    source.add_argument("--html", type=Path)
    source.add_argument("--markdown", type=Path)
    imported.add_argument("--source-url")
    imported.add_argument("--offline", action="store_true", help="不请求远程图片")
    for name in ("title", "account", "author", "published-at"):
        imported.add_argument("--" + name)
    retry = sub.add_parser("retry", help="恢复失败或部分完成的归档")
    retry.add_argument("--failed", action="store_true", required=True)
    sub.add_parser("status", help="查看归档状态")
    sub.add_parser("doctor", help="检查本机环境；只请求首页，不测试真实文章")
    search = sub.add_parser("search", help="检索本地索引、候选 CSV 或用户指定的 SearXNG 实例")
    search.add_argument("keywords", nargs="+")
    search.add_argument("--source", action="append", choices=("local", "searxng", "sogou"))
    search.add_argument("--catalog", type=Path, action="append", default=[], help="追加已有候选 CSV 来源")
    search.add_argument("--searxng-url", help="用户有权使用、允许自动查询且已启用 JSON 的实例根地址")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--account")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--pages", type=int, default=1)
    search.add_argument("--sort", choices=("relevance", "newest", "oldest"), default="relevance")
    search.add_argument("--verify", action="store_true", help="请求候选文章正文并核验元数据；不下载图片")
    search.add_argument("--no-cache", action="store_true")
    for item in (archive, imported, retry):
        group = item.add_mutually_exclusive_group()
        group.add_argument("--ocr", dest="ocr", action="store_true", default=None)
        group.add_argument("--no-ocr", dest="ocr", action="store_false")
        item.add_argument("--method", choices=("http", "browser"), default="http")
        item.add_argument("--output-dir", type=Path, help="本批全部标题.md 的交付目录")
        item.add_argument("--force", action="store_true", help="重新处理并保留旧版本")
    return command


def read_selected(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"selected", "url"}.issubset(reader.fieldnames or []):
            raise ArchiveError("CSV 必须包含 selected 和 url 列")
        return [{"kind": "url", "url": (row.get("url") or "").strip()} for row in reader
                if (row.get("selected") or "").strip().lower() in ("1", "true", "yes", "y")]


def execute(args, config):
    if args.command == "doctor":
        from .doctor import diagnose
        result = diagnose(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        write_json(config.root / "reports" / "doctor.json", result)
        return 0
    ocr_engine = None
    if args.command not in ("status", "search") and (args.ocr if args.ocr is not None else config.ocr):
        from .ocr import VisionOCR
        ocr_engine = VisionOCR(config.ocr_min_confidence)
    browser = None
    if getattr(args, "method", "http") == "browser":
        from .browser import BrowserFetcher
        browser = BrowserFetcher(config)
    archiver = Archiver(config, ocr_engine=ocr_engine, browser=browser)
    try:
        if args.command == "search":
            from .search import CatalogProvider, LocalProvider, SearxProvider, search_articles, write_results
            if not 1 <= args.limit <= 100 or not 1 <= args.pages <= 5:
                raise ArchiveError("--limit 必须在 1～100 之间，--pages 必须在 1～5 之间")
            for name in ("since", "until"):
                value = getattr(args, name)
                if value and (parse_date(value) != value):
                    raise ArchiveError(f"--{name} 必须是 YYYY-MM-DD 日期")
            if args.since and args.until and args.since > args.until:
                raise ArchiveError("--since 不得晚于 --until")
            providers, source_errors = [], []
            for source in args.source or (["local"] if not args.catalog else []):
                if source == "local":
                    providers.append(LocalProvider(archiver.database))
                elif source == "searxng":
                    if not args.searxng_url:
                        raise ArchiveError("--source searxng 需要 --searxng-url；程序不会自动选择公共实例")
                    providers.append(SearxProvider(args.searxng_url, archiver.fetcher))
                else:
                    source_errors.append({"source": "sogou", "reason":
                        "来源已停用：已观察到 robots.txt 的 User-agent: * / Disallow: /；未发送搜索请求"})
            providers += [CatalogProvider(path) for path in args.catalog]
            rows, summary = search_articles(args.keywords, providers, archiver.database, archiver.fetcher,
                limit=args.limit, pages=args.pages, since=args.since, until=args.until, account=args.account,
                sort=args.sort, verify_results=args.verify, no_cache=args.no_cache,
                cache_hours=config.search_cache_hours)
            summary.errors.extend(source_errors)
            write_results(rows, config.path("search_results_dir"))
            result = {"finished_at": now(), **summary.to_dict(), "exported": len(rows),
                      "sources": [provider.name for provider in providers],
                      "results_directory": str(config.path("search_results_dir"))}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            write_json(config.path("log_dir") / "last-search.json", result)
            return 1 if summary.errors else 0
        if args.command == "status":
            rows = archiver.database.rows()
            counts = {}
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            print(json.dumps({"counts": counts, "articles": rows}, ensure_ascii=False, indent=2))
            return 0
        input_info = {}
        if args.command == "archive":
            if args.selected:
                inputs = read_selected(args.selected)
            elif args.input:
                if args.input.suffix.lower() == ".csv":
                    raise ArchiveError("CSV 请使用 --selected，并用 selected=1 明确选择要归档的行")
                batch = parse_url_batch(args.input.read_text("utf-8-sig"))
                inputs = [{"kind": "url", "url": url} for url in batch.urls]
                input_info = {"input_url_occurrences": batch.occurrences,
                              "input_duplicates_removed": batch.duplicates}
            else:
                inputs = [{"kind": "url", "url": url} for url in args.url]
        elif args.command == "import":
            if args.published_at and parse_date(args.published_at) != args.published_at:
                raise ArchiveError("--published-at 必须是有效的 YYYY-MM-DD 日期")
            inputs = [{"kind": "html" if args.html else "markdown", "path": str((args.html or args.markdown).resolve()),
                       "url": args.source_url, "offline": args.offline, "title": args.title, "account": args.account,
                       "author": args.author, "published_at": args.published_at}]
        elif args.command == "retry":
            inputs = [json.loads(row["input_json"]) for row in archiver.database.rows(failed_only=True)]
        else:
            raise ArchiveError("未知命令")
        summary = archiver.run(inputs, force=args.force or args.command == "retry")
        from .batch import export_batch
        from datetime import datetime
        import uuid
        destination = args.output_dir or config.root / "batches" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        batch_directory = export_batch(archiver, inputs, destination, summary)
        result = {"finished_at": now(), **summary.to_dict(), **input_info, "batch_directory": batch_directory}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        write_json(config.path("log_dir") / "last-run.json", result)
        return 1 if summary.failed or summary.partial else 0
    finally:
        archiver.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        log_dir = config.path("log_dir")
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(handlers=[RotatingFileHandler(log_dir / "archive.log", maxBytes=5 * 1024 * 1024,
                                                          backupCount=3, encoding="utf-8")], level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        return execute(args, config)
    except KeyboardInterrupt:
        print("已中断。已完成归档保留，未完成任务可使用 retry --failed 恢复。", file=sys.stderr)
        return 130
    except (ArchiveError, OSError, ValueError, yaml.YAMLError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

