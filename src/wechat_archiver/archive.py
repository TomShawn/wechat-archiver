import json
import logging
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from .database import Database
from .fetcher import Fetcher
from .images import markdown_images, process_images
from .markdown import convert_html, render_document
from .models import ArchiveError, Article, SharedArticle, Summary, UnavailableArticle, now
from .parser import parse_article, parse_date
from .quality import archive_is_intact, check_content
from .storage import (
    archive_lock,
    atomic_write,
    commit_directory,
    digest,
    directory_name,
    link_or_copy,
    remove_staging,
    write_json,
)
from .urls import article_id, validate_url

logger = logging.getLogger(__name__)


@dataclass
class Job:
    """一条输入在处理过程中的状态；异常处理需要读取已确定的 key、旧记录和暂存目录。"""
    spec: dict
    kind: str | None = None
    path: Path | None = None
    source_url: str | None = None
    key: str | None = None
    previous: dict | None = None
    staging: Path | None = None

    @property
    def input_json(self):
        return json.dumps(self.spec, ensure_ascii=False)


@dataclass
class Assessment:
    content_checks: dict
    content_issue: bool
    failures: list
    ocr_errors: list
    unreferenced: list
    references: set
    status: str


def article_identity(article, raw: bytes) -> str:
    """持久身份：优先用页面中的 biz/mid/idx，否则用标题、公众号、日期和正文文本，忽略签名链接和浏览器标记。"""
    page = raw.decode("utf-8", "replace")
    fields = {}
    for field in ("biz", "mid", "idx"):
        match = re.search(r"\bvar\s+" + field + r"\s*=\s*[\"']([^\"']+)[\"']", page)
        if match:
            fields[field] = match.group(1)
    if len(fields) == 3:
        return "wechat:" + digest("|".join(fields[field] for field in ("biz", "mid", "idx")).encode())
    material = json.dumps([article.title, article.account, article.published_at,
                           BeautifulSoup(article.html, "lxml").get_text()], ensure_ascii=False)
    return digest(material.encode())


def cached_image_index(checkpoint: Path, manifest: Path, previous: dict | None) -> dict:
    """汇总可复用的图片和 OCR：检查点清单，以及上一版交付目录中记录的图片。"""
    cached = {}
    if manifest.exists():
        for image in json.loads(manifest.read_text()):
            if image.get("path") and (checkpoint / image["path"]).is_file():
                cached[image["source"]] = {"file": checkpoint / image["path"], "ocr": image.get("ocr")}
    if previous and previous.get("local_path"):
        previous_path = Path(previous["local_path"])
        quality_path = previous_path / "quality.json"
        if quality_path.is_file():
            for image in json.loads(quality_path.read_text("utf-8")).get("images", []):
                if image["path"]:
                    cached_path = (previous_path / image["path"]).resolve()
                    if cached_path.is_relative_to(previous_path.resolve()) and cached_path.is_file():
                        cached[image["source"]] = {"file": cached_path, "ocr": image.get("ocr")}
    return cached


def assess(article, body: str, images: list[dict], ocr_engine) -> Assessment:
    """对照转换前后正文、图片保存与引用、OCR 结果，给出状态并把问题追加到文章警告。"""
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
    if ocr_engine and getattr(ocr_engine, "unavailable_reason", None):
        article.warnings.append(ocr_engine.unavailable_reason)
    # 反向核对：已保存的图片都必须出现在正文中，否则转换时丢了图片位置。
    references = set(markdown_images(body))
    unreferenced = [record for record in images if record["path"] and record["path"] not in references]
    if unreferenced:
        positions = "、".join(str(record["position"]) for record in unreferenced)
        article.warnings.append(f"{len(unreferenced)} 个已保存的图片未出现在正文中（位置 {positions}），"
                                "请对照原始文件检查")
    status = "partial" if failures or ocr_errors or content_issue or unreferenced else "success"
    return Assessment(content_checks, content_issue, failures, ocr_errors, unreferenced, references, status)


class Archiver:
    def __init__(self, config, *, fetcher=None, ocr_engine=None, browser=None):
        self.config = config
        self.browser = browser
        self.fetcher = fetcher or Fetcher(config)
        self.database = Database(config.path("database"))
        self.ocr_engine = ocr_engine

    def close(self):
        self.fetcher.close()
        if self.browser:
            self.browser.close()
        self.database.close()

    def run(self, inputs: list[dict], *, force=False, refetch=False) -> Summary:
        with archive_lock(self.config.path("database")):
            self.database.recover_running()
            # 重新抓取必然需要重新处理。
            return self._run(inputs, force=force or refetch, refetch=refetch)

    def _run(self, inputs: list[dict], *, force=False, refetch=False) -> Summary:
        summary = Summary(selected=len(inputs))
        for spec in inputs:
            job = Job(spec)
            try:
                self._archive_one(job, summary, force=force, refetch=refetch)
            except (ArchiveError, OSError, ValueError, yaml.YAMLError, sqlite3.Error) as exc:
                self._record_failure(job, exc, summary)
            except KeyboardInterrupt:
                if job.key:
                    self.database.save(job.key, {"status": job.previous["status"] if job.previous else "interrupted",
                                                 "error": "用户中断；可使用 retry --failed 恢复",
                                                 "input_json": job.input_json})
                raise
            finally:
                if job.staging:
                    remove_staging(job.staging)
        return summary

    def _archive_one(self, job, summary, *, force, refetch):
        """单篇文章：解析输入 → 读取来源 → 去重 → 处理图片与转换 → 质量检查 → 落盘。"""
        self._resolve_input(job)
        job.previous = self.database.get(job.key)
        if job.previous and job.previous["status"] in ("success", "duplicate") and not force \
                and archive_is_intact(job.previous):
            summary.skipped += 1
            return
        self.database.save(job.key, {"source_url": job.source_url, "status": "running", "error": None,
                                     "input_json": job.input_json})
        raw, shared_from = self._load_source(job, refetch)
        article = self._parse(job, raw, shared_from)
        if self._skip_duplicate(job, article, raw, force):
            summary.skipped += 1
            return
        checkpoint = self._checkpoint(job.key)
        checkpoint.mkdir(parents=True, exist_ok=True)
        if job.kind != "url":
            atomic_write(checkpoint / ("source.md" if job.kind == "markdown" else "source.html"), raw)
        html, images = self._process_images(job, article, checkpoint)
        out = self.config.path("output_dir")
        out.mkdir(parents=True, exist_ok=True)
        job.staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=out))
        # 只带入本文引用的图片；检查点里旧版本留下的图片不进入交付目录。
        for record in images:
            if record["path"] and not (job.staging / record["path"]).exists():
                (job.staging / record["path"]).parent.mkdir(parents=True, exist_ok=True)
                link_or_copy(checkpoint / record["path"], job.staging / record["path"])
        body = convert_html(html, job.source_url)
        (job.staging / ("original.md" if job.kind == "markdown" else "original.html")).write_bytes(raw)
        if not body.strip():
            raise ArchiveError("转换后正文为空")
        assessment = assess(article, body, images, self.ocr_engine)
        target = self._commit(job, article, body, raw, images, assessment, out)
        self._record_success(job, summary, images, assessment)
        logger.info("archive id=%s status=%s path=%s", job.key, assessment.status, target)

    def _resolve_input(self, job):
        spec = job.spec
        job.kind = spec["kind"]
        job.path = Path(spec["path"]).expanduser().resolve() if spec.get("path") else None
        job.source_url = validate_url(spec["url"], article=True) if spec.get("url") else None
        if job.kind == "url":
            if not job.source_url:
                raise ArchiveError("URL 不能为空")
            job.key = article_id(job.source_url)
        elif job.kind in ("html", "markdown"):
            if not job.path or not job.path.is_file():
                raise ArchiveError(f"导入文件不存在：{job.path}")
            # 路径用作本地导入任务标识；即使没有 source_url，也可补充元数据后强制重处理。
            job.key = article_id(job.source_url) if job.source_url else digest(str(job.path).encode())
        else:
            raise ArchiveError(f"未知输入类型：{job.kind}")

    def _checkpoint(self, key):
        return self.config.path("database").parent / "checkpoints" / key

    def _load_source(self, job, refetch):
        """返回原始页面字节和分享卡片链接（输入不是分享卡片时为 None）。"""
        if job.kind != "url":
            if job.path.stat().st_size > self.config.max_html_bytes:
                raise ArchiveError("导入文件超过 HTML 大小限制")
            return job.path.read_bytes(), None
        checkpoint = self._checkpoint(job.key)
        shared_from = None
        raw, mime, job.source_url = self._load_page(checkpoint / "source.html", job.source_url, refetch)
        try:
            parse_article(raw, job.source_url)
        except SharedArticle as shared:
            # 分享卡片只有摘要：改为归档原文，原文页面单独缓存，保留卡片以便重处理时识别。
            shared_from = job.source_url
            raw, mime, job.source_url = self._load_page(
                checkpoint / "shared-original.html", shared.original_url, refetch)
        if mime and mime not in ("text/html", "application/xhtml+xml"):
            raise ArchiveError(f"文章响应类型不正确：{mime}")
        return raw, shared_from

    def _parse(self, job, raw, shared_from):
        spec = job.spec
        if job.kind == "markdown":
            article = self._import_markdown(raw, job.path, job.source_url)
            job.source_url = article.source_url
        else:
            article = parse_article(raw, job.source_url, local=job.kind != "url", title_override=spec.get("title"))
        if shared_from:
            article.warnings.append(f"输入链接是转发的分享卡片，已改为归档原文；分享链接：{shared_from}")
        for name in ("title", "account", "author", "published_at"):
            if spec.get(name):
                setattr(article, name, spec[name])
                article.warnings.append(f"{name} 由用户显式补充，未视为远程核验结果")
                if name == "published_at":
                    article.date_evidence = f"user_provided: {spec[name]}（未远程核验）"
        return article

    def _skip_duplicate(self, job, article, raw, force):
        """同一篇文章已有完整归档时记为 duplicate 并返回 True；否则登记本记录的身份。"""
        identity = article_identity(article, raw)
        alias = job.source_url or str(job.path)
        existing = self.database.find_identity(identity, exclude=job.key)
        if existing and archive_is_intact(existing) and not force:
            # 只引用已有归档的交付路径和元数据，发现时间等保留本记录自己的值。
            self.database.save(job.key, {"source_url": job.source_url, "status": "duplicate",
                                         **{name: existing[name] for name in (
                                             "title", "account", "published_at", "local_path", "archived_at")},
                                         "content_hash": digest(raw), "error": None, "input_json": job.input_json})
            self.database.add_alias(alias, existing["id"])
            return True
        self.database.claim_identity(identity, job.key, alias)
        return False

    def _process_images(self, job, article, checkpoint):
        manifest = checkpoint / "images.json"
        cached_images = cached_image_index(checkpoint, manifest, job.previous)
        html, images = process_images(
            article.html, checkpoint / "assets", self.fetcher, base_url=job.source_url,
            local_root=job.path.parent if job.path else None, offline=job.spec.get("offline", False),
            ocr_engine=self.ocr_engine, cached_images=cached_images,
            checkpoint_callback=lambda records: write_json(manifest, records),
        )
        write_json(manifest, images)
        return html, images

    def _commit(self, job, article, body, raw, images, assessment, out):
        """写入 index.md 与 quality.json，核对落盘文件后原子切换到交付目录并更新索引。"""
        staging = job.staging
        verification = "body_verified" if job.kind == "url" else "local_import_unverified"
        archived_at = now()
        metadata = {"title": article.title, "author": article.author, "account": article.account,
                    "published_at": article.published_at, "source_url": job.source_url,
                    "archived_at": archived_at,
                    "retrieval_method": "browser" if job.kind == "url" and self.browser else job.kind,
                    "verification_status": verification, "archive_status": assessment.status,
                    "date_evidence": article.date_evidence, "warnings": article.warnings}
        (staging / "index.md").write_text(render_document(metadata, body), "utf-8")
        # 检查实际落盘文件，而非仅检查字符串生成是否成功。
        for record in images:
            if record["path"] and not (staging / record["path"]).is_file():
                raise ArchiveError(f"本地图片不存在：{record['path']}")
        for reference in assessment.references:
            if not reference.startswith("assets/") or not (staging / reference).is_file():
                raise ArchiveError(f"Markdown 图片引用无效：{reference}")
        quality = {"article_id": job.key, "status": assessment.status, "verification_status": verification,
                   "checked_at": archived_at, "warnings": article.warnings, "images": images,
                   "checks": {"title_present": True, "body_nonempty": True,
                              **assessment.content_checks,
                              "saved_images_exist": True, "markdown_images_resolve": True,
                              "saved_images_referenced": not assessment.unreferenced,
                              "date_has_evidence": bool(article.date_evidence),
                              "full_content_manually_verified": False},
                   "content_hash": digest(raw)}
        write_json(staging / "quality.json", quality)
        target = out / directory_name(article.title, job.key)
        if target.exists():
            target = out / (target.name + "--" + uuid.uuid4().hex[:8])
        commit_directory(staging, target)
        job.staging = None
        self.database.save(job.key, {"source_url": job.source_url, "title": article.title,
                                     "account": article.account, "published_at": article.published_at,
                                     "local_path": str(target), "status": assessment.status,
                                     "archived_at": archived_at, "content_hash": digest(raw),
                                     "error": "; ".join(r["error"] or "" for r in assessment.failures) or None,
                                     "input_json": job.input_json})
        return target

    def _record_success(self, job, summary, images, assessment):
        source = job.source_url or str(job.path)
        summary.verified += int(job.kind == "url")
        summary.succeeded += int(assessment.status == "success")
        summary.partial += int(assessment.status == "partial")
        summary.images_saved += sum(bool(r["new_file"]) for r in images)
        summary.images_downloaded += sum(bool(r["downloaded"]) for r in images)
        summary.ocr_processed += sum(bool(r["ocr_executed"]) for r in images)
        for record in assessment.failures:
            summary.errors.append({"input": source, "image": record["position"],
                                   "reason": record["error"], "suggestion": "保存网页及资源文件夹后重新导入"})
        for record in assessment.ocr_errors:
            summary.errors.append({"input": source, "image": record["position"],
                                   "reason": record["ocr"]["error"], "suggestion": "检查本地 Vision 安装，或 --no-ocr"})
        for record in assessment.unreferenced:
            summary.errors.append({"input": source, "image": record["position"],
                                   "reason": "图片已保存但未出现在转换后的正文中",
                                   "suggestion": "对照 original.html 检查 index.md 中该图片位置"})
        if assessment.content_issue:
            summary.errors.append({"input": source, "reason": "正文或代码块对照检查失败",
                                   "suggestion": "对照 original.html / original.md 检查 index.md"})

    def _record_failure(self, job, exc, summary):
        summary.failed += 1
        summary.errors.append({"input": job.spec.get("url") or job.spec.get("path"), "reason": str(exc),
                               "suggestion": getattr(exc, "suggestion", None)
                               or "在浏览器检查文章并保存 HTML 与资源文件夹，再使用 import --html"})
        if job.key:
            # 不可访问的文章记为 unavailable，retry --failed 不再反复请求。
            values = {"status": "unavailable" if isinstance(exc, UnavailableArticle) else "failed",
                      "error": str(exc), "input_json": job.input_json}
            # 已有可用交付的记录（成功、重复引用或部分完成）不因重处理失败降级为 failed。
            if job.previous and job.previous["status"] in ("success", "duplicate", "partial") \
                    and job.previous.get("local_path"):
                values["status"] = job.previous["status"]
            self.database.save(job.key, values)
        logger.warning("archive failed id=%s reason=%s", job.key, exc)

    def _load_page(self, cache, url, refetch):
        """读取已缓存的文章页面；没有缓存或要求 --refetch 时重新抓取，确认是文章页后才写入缓存。"""
        # --force 复用已抓取的页面，只有 --refetch 才重新请求微信。
        if cache.exists() and not refetch:
            return cache.read_bytes(), "text/html", url
        raw, mime, final_url = (self.browser or self.fetcher).get(url, article=True)
        try:
            parse_article(raw, final_url)
        except SharedArticle:
            pass
        atomic_write(cache, raw)
        return raw, mime, final_url

    def _import_markdown(self, raw, path, source_url):
        body = raw.decode("utf-8-sig")
        metadata = {}
        if body.startswith("---\n"):
            match = re.match(r"\A---\n(.*?)\n---\s*\n", body, re.DOTALL)
            if not match:
                raise ArchiveError("Markdown Front Matter 未闭合")
            metadata = yaml.safe_load(match.group(1)) or {}
            if not isinstance(metadata, dict):
                raise ArchiveError("Markdown Front Matter 必须是对象")
            body = body[match.end():]
        title = metadata.get("title")
        if not title:
            match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
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

