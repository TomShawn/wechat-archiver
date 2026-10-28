import re
import json
from pathlib import Path

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt


def archive_is_intact(record) -> bool:
    directory = Path(record.get("local_path") or "")
    try:
        if not (directory / "index.md").is_file():
            return False
        quality = json.loads((directory / "quality.json").read_text("utf-8"))
        if quality["status"] != "success":
            return False
        return all((directory / image["path"]).is_file() for image in quality.get("images", []) if image["path"])
    except (OSError, ValueError, KeyError):
        return False


def check_content(source_html: str, markdown: str) -> dict:
    """检查 Markdown 渲染后的正文及代码，忽略排版空白和插入的 OCR。"""
    original = BeautifulSoup(source_html, "lxml")
    rendered = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).enable(
        ["table", "strikethrough"]).render(markdown), "lxml")
    for block in list(rendered.find_all("blockquote")):
        if block.get_text(strip=True).startswith("OCR 提取内容（请对照原图核对）"):
            block.decompose()
    for paragraph in list(rendered.find_all("p")):
        if re.fullmatch(r"\[图片 \d+ 保存失败：.*；原地址见 quality\.json\]", paragraph.get_text()):
            paragraph.decompose()

    def normalized(text):
        return re.sub(r"\s+", "", text).replace("\u200b", "")

    expected = normalized(original.get_text())
    actual = normalized(rendered.get_text())
    source_codes = [node.get_text().replace("\xa0", " ").strip("\n") for node in original.find_all("pre")]
    archived_codes = [node.get_text().strip("\n") for node in rendered.find_all("pre")]
    return {"body_text_equal_ignoring_whitespace": expected == actual,
            "code_blocks_equal": source_codes == archived_codes,
            "source_text_length": len(expected), "archived_text_length": len(actual),
            "source_code_blocks": len(source_codes), "archived_code_blocks": len(archived_codes)}

