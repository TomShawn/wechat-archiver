import json
import re
import logging
import sqlite3
import tempfile
import uuid
from pathlib import Path

import yaml

from .database import Database
from .fetcher import Fetcher
from .images import markdown_images, process_images
from .markdown import convert_html, render_document
from .models import ArchiveError, Summary, now
from .parser import parse_article
from .quality import archive_is_intact, check_content
from .storage import (atomic_write, archive_lock, commit_directory, digest, directory_name, link_or_copy,
                      remove_staging, write_json)
from .urls import article_id, validate_url

logger = logging.getLogger(__name__)


class Archiver:
    def __init__(self, config, *, fetcher=None, ocr_engine=None, browser=None):
        self.config = config
        self.browser = browser
        self.fetcher = fetcher or Fetcher(config)
        self.database = Database(config.path("database"))
        self.ocr_engine = ocr_engine

    def close(self):
        self.fetcher.close()
        if self.browser: self.browser.close()
        self.database.close()

    def run(self, inputs: list[dict], *, force=False, refetch=False) -> Summary:
        with archive_lock(self.config.path("database")):
            self.database.recover_running()
            # 重新抓取必然需要重新处理。
            return self._run(inputs, force=force or refetch, refetch=refetch)

    def _run(self, inputs: list[dict], *, force=False, refetch=False) -> Summary:
        summary = Summary(selected=len(inputs))
        for spec in inputs:
            key = None
            staging = None
            previous = None
            try:
                kind = spec["kind"]
                path = Path(spec["path"]).expanduser().resolve() if spec.get("path") else None
                source_url = validate_url(spec["url"], article=True) if spec.get("url") else None
                if kind == "url":
                    if not source_url:
                        raise ArchiveError("URL 不能为空")
                    key = article_id(source_url)
                elif kind in ("html", "markdown"):
                    if not path or not path.is_file():
                        raise ArchiveError(f"导入文件不存在：{path}")
                    # 路径用作本地导入任务标识；即使没有 source_url，也可补充元数据后强制重处理。
                    key = article_id(source_url) if source_url else digest(str(path).encode())
                else:
                    raise ArchiveError(f"未知输入类型：{kind}")
                previous = self.database.get(key)
                if previous and previous["status"] in ("success", "duplicate") and not force and archive_is_intact(previous):
                    summary.skipped += 1
                    continue
                input_json = json.dumps(spec, ensure_ascii=False)
                self.database.save(key, {"source_url": source_url, "status": "running", "error": None,
                                         "input_json": input_json})
                if kind == "url":
                    checkpoint = self.config.path("database").parent / "checkpoints" / key
                    checkpoint.mkdir(parents=True, exist_ok=True)
                    cached_html = checkpoint / "source.html"
                    # --force 复用已抓取的页面，只有 --refetch 才重新请求微信。
                    if cached_html.exists() and not refetch:
                        raw, mime = cached_html.read_bytes(), "text/html"
                    else:
                        raw, mime, source_url = (self.browser or self.fetcher).get(source_url, article=True)
                        parse_article(raw, source_url)
                        atomic_write(cached_html, raw)
                    if mime and mime not in ("text/html", "application/xhtml+xml"):
                        raise ArchiveError(f"文章响应类型不正确：{mime}")
                else:
                    if path.stat().st_size > self.config.max_html_bytes:
                        raise ArchiveError("导入文件超过 HTML 大小限制")
                    raw = path.read_bytes()
                if kind == "markdown":
                    article = self._import_markdown(raw, path, source_url)
                    source_url = article.source_url
                else:
                    article = parse_article(raw, source_url, local=kind != "url", title_override=spec.get("title"))
                for name in ("title", "account", "author", "published_at"):
                    if spec.get(name):
                        setattr(article, name, spec[name])
                        article.warnings.append(f"{name} 由用户显式补充，未视为远程核验结果")
                        if name == "published_at":
                            article.date_evidence = f"user_provided: {spec[name]}（未远程核验）"
                # Durable identity ignores signed discovery URLs and browser-only markup.
                identity_material = json.dumps([article.title, article.account, article.published_at,
                    __import__('bs4').BeautifulSoup(article.html, 'lxml').get_text()], ensure_ascii=False)
                identity = digest(identity_material.encode())
                identity_fields = {}
                for field in ("biz", "mid", "idx"):
                    match_field = re.search(r"\bvar\s+" + field + r"\s*=\s*[\"']([^\"']+)[\"']", raw.decode("utf-8", "replace"))
                    if match_field: identity_fields[field] = match_field.group(1)
                if len(identity_fields) == 3:
                    identity = "wechat:" + digest("|".join(identity_fields[field] for field in ("biz", "mid", "idx")).encode())
                alias = source_url or str(path)
                existing = self.database.find_identity(identity, exclude=key)
                if existing and archive_is_intact(existing) and not force:
                    # 只引用已有归档的交付路径和元数据，发现时间等保留本记录自己的值。
                    self.database.save(key, {"source_url": source_url, "status": "duplicate",
                                             **{name: existing[name] for name in (
                                                 "title", "account", "published_at", "local_path", "archived_at")},
                                             "content_hash": digest(raw), "error": None, "input_json": input_json})
                    self.database.add_alias(alias, existing["id"])
                    summary.skipped += 1
                    continue
                self.database.claim_identity(identity, key, alias)
                checkpoint = self.config.path("database").parent / "checkpoints" / key
                checkpoint.mkdir(parents=True, exist_ok=True)
                atomic_write(checkpoint / ("source.md" if kind == "markdown" else "source.html"), raw)
                out = self.config.path("output_dir")
                out.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=out))
                cached_images = {}
                cache_manifest = checkpoint / "images.json"
                if cache_manifest.exists():
                    for image in json.loads(cache_manifest.read_text()):
                        if image.get("path") and (checkpoint / image["path"]).is_file():
                            cached_images[image["source"]] = {"file": checkpoint / image["path"], "ocr": image.get("ocr")}
                if previous and previous.get("local_path"):
                    previous_path = Path(previous["local_path"])
                    quality_path = previous_path / "quality.json"
                    if quality_path.is_file():
                        for image in json.loads(quality_path.read_text("utf-8")).get("images", []):
                            if image["path"]:
                                cached_path = (previous_path / image["path"]).resolve()
                                if cached_path.is_relative_to(previous_path.resolve()) and cached_path.is_file():
                                    cached_images[image["source"]] = {"file": cached_path, "ocr": image.get("ocr")}
                html, images = process_images(
                    article.html, checkpoint / "assets", self.fetcher, base_url=source_url,
                    local_root=path.parent if path else None, offline=spec.get("offline", False),
                    ocr_engine=self.ocr_engine, cached_images=cached_images,
                    checkpoint_callback=lambda records: write_json(cache_manifest, records),
                )
                write_json(cache_manifest, images)
                # 只带入本文引用的图片；检查点里旧版本留下的图片不进入交付目录。
                for record in images:
                    if record["path"] and not (staging / record["path"]).exists():
                        (staging / record["path"]).parent.mkdir(parents=True, exist_ok=True)
                        link_or_copy(checkpoint / record["path"], staging / record["path"])
                body = convert_html(html, source_url)
                original_name = "original.md" if kind == "markdown" else "original.html"
                (staging / original_name).write_bytes(raw)
                if not body.strip():
                    raise ArchiveError("转换后正文为空")
                content_checks = check_content(article.html, body)
                content_issue = not content_checks["body_text_equal_ignoring_whitespace"] or not content_checks["code_blocks_equal"]
                if content_issue:
                    article.warnings.append("正文文本或代码块未通过转换前后对照，请对照原始文件人工检查")
                failures = [record for record in images if record["status"] != "saved"]
                ocr_errors = [record for record in images if (record.get("ocr") or {}).get("status") == "error"]
                if failures:
                    article.warnings.append(f"{len(failures)} 个图片位置保存失败，请检查 quality.json")
                if ocr_errors:
                    article.warnings.append(f"{len(ocr_errors)} 个图片 OCR 失败，原图已保留")
                low_ocr = [record for record in images if (record.get("ocr") or {}).get("low_confidence")]
                if low_ocr:
                    article.warnings.append(f"{len(low_ocr)} 个图片位置的 OCR 置信度不足，需对照原图核对")
                if self.ocr_engine and getattr(self.ocr_engine, "unavailable_reason", None):
                    article.warnings.append(self.ocr_engine.unavailable_reason)
                status = "partial" if failures or ocr_errors or content_issue else "success"
                verification = "body_verified" if kind == "url" else "local_import_unverified"
                archived_at = now()
                metadata = {"title": article.title, "author": article.author, "account": article.account,
                            "published_at": article.published_at, "source_url": source_url,
                            "archived_at": archived_at, "retrieval_method": "browser" if kind == "url" and self.browser else kind,
                            "verification_status": verification, "archive_status": status,
                            "date_evidence": article.date_evidence, "warnings": article.warnings}
                document = render_document(metadata, body)
                (staging / "index.md").write_text(document, "utf-8")
                # 检查实际落盘文件，而非仅检查字符串生成是否成功。
                for record in images:
                    if record["path"] and not (staging / record["path"]).is_file():
                        raise ArchiveError(f"本地图片不存在：{record['path']}")
                for reference in markdown_images(body):
                    if not reference.startswith("assets/") or not (staging / reference).is_file():
                        raise ArchiveError(f"Markdown 图片引用无效：{reference}")
                quality = {"article_id": key, "status": status, "verification_status": verification,
                           "checked_at": archived_at, "warnings": article.warnings, "images": images,
                           "checks": {"title_present": True, "body_nonempty": True,
                                      **content_checks,
                                      "saved_images_exist": True, "markdown_images_resolve": True,
                                      "date_has_evidence": bool(article.date_evidence),
                                      "full_content_manually_verified": False},
                           "content_hash": digest(raw)}
                write_json(staging / "quality.json", quality)
                target = out / directory_name(article.title, key)
                if target.exists():
                    target = out / (target.name + "--" + uuid.uuid4().hex[:8])
                commit_directory(staging, target)
                staging = None
                self.database.save(key, {"source_url": source_url, "title": article.title,
                                         "account": article.account, "published_at": article.published_at,
                                         "local_path": str(target), "status": status, "archived_at": archived_at,
                                         "content_hash": digest(raw), "error": "; ".join(
                                             r["error"] or "" for r in failures) or None,
                                         "input_json": input_json})
                summary.verified += int(kind == "url")
                summary.succeeded += int(status == "success")
                summary.partial += int(status == "partial")
                summary.images_saved += sum(bool(r["new_file"]) for r in images)
                summary.images_downloaded += sum(bool(r["downloaded"]) for r in images)
                summary.ocr_processed += sum(bool(r["ocr_executed"]) for r in images)
                for record in failures:
                    summary.errors.append({"input": source_url or str(path), "image": record["position"],
                                           "reason": record["error"], "suggestion": "保存网页及资源文件夹后重新导入"})
                for record in ocr_errors:
                    summary.errors.append({"input": source_url or str(path), "image": record["position"],
                                           "reason": record["ocr"]["error"], "suggestion": "检查本地 Vision 安装，或 --no-ocr"})
                if content_issue:
                    summary.errors.append({"input": source_url or str(path), "reason": "正文或代码块对照检查失败",
                                           "suggestion": "对照 original.html / original.md 检查 index.md"})
                logger.info("archive id=%s status=%s path=%s", key, status, target)
            except (ArchiveError, OSError, ValueError, yaml.YAMLError, sqlite3.Error) as exc:
                summary.failed += 1
                summary.errors.append({"input": spec.get("url") or spec.get("path"), "reason": str(exc),
                                       "suggestion": "在浏览器检查文章并保存 HTML 与资源文件夹，再使用 import --html"})
                if key:
                    values = {"status": "failed", "error": str(exc), "input_json": json.dumps(spec, ensure_ascii=False)}
                    # 已有可用交付的记录（成功、重复引用或部分完成）不因重处理失败降级为 failed。
                    if previous and previous["status"] in ("success", "duplicate", "partial") \
                            and previous.get("local_path"):
                        values["status"] = previous["status"]
                    self.database.save(key, values)
                logger.warning("archive failed id=%s reason=%s", key, exc)
            except KeyboardInterrupt:
                if key:
                    self.database.save(key, {"status": previous["status"] if previous else "interrupted",
                                             "error": "用户中断；可使用 retry --failed 恢复",
                                             "input_json": json.dumps(spec, ensure_ascii=False)})
                raise
            finally:
                if staging:
                    remove_staging(staging)
        return summary

    def _import_markdown(self, raw, path, source_url):
        import re
        from bs4 import BeautifulSoup
        from markdown_it import MarkdownIt
        from .models import Article
        from .parser import parse_date

        body = raw.decode("utf-8-sig")
        metadata = {}
        if body.startswith("---\n"):
            match = re.match(r"\A---\n(.*?)\n---\s*\n", body, re.S)
            if not match:
                raise ArchiveError("Markdown Front Matter 未闭合")
            metadata = yaml.safe_load(match.group(1)) or {}
            if not isinstance(metadata, dict):
                raise ArchiveError("Markdown Front Matter 必须是对象")
            body = body[match.end():]
        title = metadata.get("title")
        if not title:
            match = re.search(r"^#\s+(.+)$", body, re.M)
            title = match.group(1) if match else path.stem
        if not source_url and metadata.get("source_url"):
            source_url = validate_url(str(metadata["source_url"]), article=True)
        html = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"]).render(body)
        soup = BeautifulSoup(html, "lxml")
        for node in soup.select("script, style, iframe, meta, link"):
            node.decompose()
        article = Article(title=str(title), html=str(soup), account=metadata.get("account"), author=metadata.get("author"),
                          published_at=parse_date(str(metadata.get("published_at", ""))), source_url=source_url,
                          warnings=["导入已有 Markdown，正文和元数据未经远程核验"])
        if article.published_at:
            article.date_evidence = "imported_markdown_front_matter: published_at（未远程核验）"
        return article

