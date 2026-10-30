import base64
import binascii
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

from .models import ArchiveError


def inspect_image(data: bytes) -> tuple[str, int, int]:
    formats = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tiff"}
    try:
        with Image.open(io.BytesIO(data)) as image:
            extension = formats.get(image.format)
            width, height = image.size
            image.verify()
        if not extension:
            raise ArchiveError("不支持的图片格式；仅保存可验证的栅格图片")
        return extension, width, height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ArchiveError("响应不是有效图片或图片已损坏") from exc


def local_resource(source: str, root: Path) -> Path:
    parts = urlsplit(source)
    path = Path(unquote(parts.path))
    if parts.scheme == "file":
        if parts.netloc not in ("", "localhost"):
            raise ArchiveError("不支持远程 file URL")
        candidate = path.resolve()
    else:
        candidate = (root / path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ArchiveError("图片路径超出导入文件所在目录，请将资源移至同目录后重试")
    if not candidate.is_file():
        raise ArchiveError("本地图片不存在，请同时保存网页的资源文件夹")
    return candidate


def image_source(node) -> str:
    return node.get("data-src") or node.get("data-original") or node.get("src") or ""


def needs_download(source: str, local_root: Path | None, cached_images: dict) -> bool:
    if not source or source.startswith("data:") or source in cached_images:
        return False
    try:
        parts = urlsplit(source)
    except ValueError:
        return False
    return not (parts.scheme == "file" or (local_root and not parts.scheme and not parts.netloc))


def process_images(html: str, dest: Path, fetcher, *, base_url: str | None = None,
                   local_root: Path | None = None, offline=False, ocr_engine=None,
                   cached_images=None, checkpoint_callback=None) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    dest.mkdir(parents=True, exist_ok=True)
    cached_images = cached_images or {}
    nodes = soup.find_all("img")
    # 远程图片提前并发下载；写盘、OCR 和检查点仍按文章顺序逐张进行。
    downloads = {}
    executor = ThreadPoolExecutor(max_workers=getattr(fetcher.config, "image_workers", 1))
    try:
        if not offline:
            for node in nodes:
                source = image_source(node)
                if source not in downloads and needs_download(source, local_root, cached_images):
                    resolved = urljoin(base_url or "https://mp.weixin.qq.com/", source)
                    downloads[source] = executor.submit(fetcher.get, resolved, image=True)
        return _process_nodes(soup, nodes, downloads, dest, fetcher, base_url=base_url,
                              local_root=local_root, offline=offline,
                              ocr_engine=ocr_engine, cached_images=cached_images,
                              checkpoint_callback=checkpoint_callback)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _process_nodes(soup, nodes, downloads, dest, fetcher, *, base_url, local_root, offline, ocr_engine,
                   cached_images, checkpoint_callback):
    by_source, by_content, records = {}, {}, []
    for number, node in enumerate(nodes, 1):
        source = image_source(node)
        record = {"position": number, "source": source, "path": None, "status": "failed", "error": None,
                  "ocr": None, "new_file": False, "downloaded": False, "ocr_executed": False}
        if source in by_source:
            original = by_source[source]
            record.update({key: original[key] for key in ("path", "status", "error", "ocr")})
        else:
            try:
                if not source:
                    raise ArchiveError("图片缺少 src/data-src")
                parts = urlsplit(source)
                if source.startswith("data:"):
                    header, value = source.split(",", 1)
                    if not header.startswith("data:image/") or ";base64" not in header:
                        raise ArchiveError("只支持 base64 编码的内嵌栅格图片")
                    if len(value) > fetcher.config.max_image_bytes * 4 // 3 + 16:
                        raise ArchiveError("内嵌图片超过大小限制")
                    data = base64.b64decode(value, validate=True)
                elif parts.scheme == "file" or (local_root and not parts.scheme and not parts.netloc):
                    path = local_resource(source, local_root)
                    if path.stat().st_size > fetcher.config.max_image_bytes:
                        raise ArchiveError("本地图片超过大小限制")
                    data = path.read_bytes()
                elif source in cached_images:
                    cached_path = cached_images[source]["file"]
                    data = cached_path.read_bytes()
                else:
                    if offline:
                        raise ArchiveError("离线导入未下载远程图片；去掉 --offline 可补充下载")
                    future = downloads.get(source)
                    data, _, _ = future.result() if future else fetcher.get(
                        urljoin(base_url or "https://mp.weixin.qq.com/", source), image=True)
                    record["downloaded"] = True
                extension, width, height = inspect_image(data)
                name = f"{hashlib.sha256(data).hexdigest()}.{extension}"
                path = dest / name
                record["new_file"] = not path.exists()
                if record["new_file"]:
                    path.write_bytes(data)
                record.update(path=f"assets/{name}", status="saved", width=width, height=height)
                # 小图、图标和明确标记为装饰的图片跳过 OCR，普通图片由 Vision 判断有无文字。
                decorative = width < 80 or height < 40 or node.get("role") == "presentation" or any(
                    hint in (str(node.get("class", [])) + " " + str(node.get("alt", ""))).lower()
                    for hint in ("emoji", "icon", "logo", "二维码", "装饰")
                )
                if ocr_engine and not decorative:
                    cached_ocr = by_content.get(name) or cached_images.get(source, {}).get("ocr")
                    if cached_ocr and cached_ocr.get("status") == "ok":
                        record["ocr"] = cached_ocr
                        record["ocr_reused"] = True
                    else:
                        record["ocr"] = ocr_engine.recognize(path)
                        record["ocr_executed"] = True
                    by_content[name] = record["ocr"]
                elif ocr_engine and decorative:
                    record["ocr"] = {"status": "skipped_decorative", "text": ""}
            except (ArchiveError, ValueError, OSError, binascii.Error) as exc:
                record["error"] = str(exc)
            by_source[source] = record.copy()
        if record["path"]:
            node["src"] = record["path"]
            for attribute in ("data-src", "data-original", "srcset", "data-srcset"):
                node.attrs.pop(attribute, None)
            result = record.get("ocr")
            if result and result.get("text"):
                note = soup.new_tag("blockquote")
                label = soup.new_tag("p")
                label.string = "OCR 提取内容（请对照原图核对）" + (
                    "；低置信度" if result.get("low_confidence") else ""
                )
                note.append(label)
                pre = soup.new_tag("pre")
                code = soup.new_tag("code")
                code.string = result["text"]
                pre.append(code)
                note.append(pre)
                node.insert_after(note)
        else:
            # 不保留失效远程图片链接冒充本地归档，位置与失败原因仍保留在文章中。
            note = soup.new_tag("p")
            note.string = f"[图片 {number} 保存失败：{record['error']}；原地址见 quality.json]"
            node.replace_with(note)
        records.append(record)
        if checkpoint_callback: checkpoint_callback(records)
    return str(soup), records


def markdown_images(body: str) -> list[str]:
    from markdown_it import MarkdownIt
    references = []

    def visit(tokens):
        for token in tokens:
            if token.type == "image":
                references.append(token.attrGet("src"))
            elif token.type in ("html_block", "html_inline"):
                references.extend(image.get("src", "") for image in BeautifulSoup(
                    token.content, "lxml").find_all("img"))
            if token.children:
                visit(token.children)

    visit(MarkdownIt("commonmark", {"html": True}).enable("table").parse(body))
    return references

