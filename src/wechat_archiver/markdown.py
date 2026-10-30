import re
import unicodedata
from urllib.parse import urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


class Converter(MarkdownConverter):
    def convert_strong(self, el, text, parent_tags):
        if not text.strip():
            return ""
        plain = el.get_text().strip()
        # 中英文混排时，标点紧贴 ** 会导致 CommonMark 把分隔符当成可见星号。
        # 对边界为标点/符号的强调保留 HTML，避免增删原文文字或加入虚构空格。
        if plain and any(unicodedata.category(character).startswith(("P", "S"))
                         for character in (plain[0], plain[-1])):
            return str(el)
        # 相邻 <strong>...</strong><strong>...</strong> 若都转成 **...****...**，
        # CommonMark 会把中间的 **** 当作正文文字，故保留 HTML。
        if any(getattr(neighbor, "name", None) in ("strong", "b")
               for neighbor in (el.previous_sibling, el.next_sibling)):
            return str(el)
        return super().convert_strong(el, text, parent_tags)

    def convert_video(self, el, text, parent_tags):
        # markdownify 默认把视频 poster 变为远程 Markdown 图片，绕过图片本地化。
        # 视频不在下载范围内，保留 HTML 和真实链接，不创建虚假的本地图片引用。
        el.attrs.pop("autoplay", None)
        el["preload"] = "none"
        return "\n\n<!-- 嵌入视频未下载，播放依赖原地址；请查看原文。 -->\n" + str(el) + "\n\n"

    def convert_pre(self, el, text, parent_tags):
        code = el.find("code")
        # 即使上游 HTML 没经过微信结构规范化，也不能因存在多个 code 子节点而只取第一段。
        content = el.get_text().replace("\xa0", " ").rstrip("\n")
        language = ""
        for item in (code or el).get("class", []):
            if item.startswith("language-"):
                language = re.sub(r"[^a-zA-Z0-9_+-]", "", item[9:])
        fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", content)), default=3))
        return f"\n\n{fence}{language}\n{content}\n{fence}\n\n"

    def convert_table(self, el, text, parent_tags):
        # 微信文章可出现 <table><tbody><tr></tr></tbody></table> 这样的空布局节点。
        # markdownify 会为它输出可见的 | 符号；没有文字或媒体时可安全忽略。
        if not el.get_text(strip=True) and not el.find(
            ["img", "picture", "svg", "video", "audio", "iframe", "object", "embed", "canvas"]
        ):
            return ""
        if el.find(attrs={"rowspan": True}) or el.find(attrs={"colspan": True}):
            return "\n\n<!-- 复杂表格：保留原始 HTML，请人工检查 -->\n" + str(el) + "\n\n"
        # 微信常用每行只有一个单元格的表格做版面排版。按数据表转换会把图片降级为 alt 文字、
        # 把 OCR 和视频压成一行，因此逐个单元格按普通正文块转换。
        cells = [row.find_all(["td", "th"], recursive=False) for row in el.find_all("tr")]
        if all(len(row) <= 1 for row in cells):
            blocks = [type(self)(**self.options).convert(row[0].decode_contents()).strip() for row in cells if row]
            return "\n\n" + "\n\n".join(block for block in blocks if block) + "\n\n"
        return super().convert_table(el, text, parent_tags)


def convert_html(html: str, base_url: str | None = None) -> str:
    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if urlsplit(href).scheme.lower() in ("javascript", "data", "vbscript"):
            del link["href"]
        elif base_url:
            link["href"] = urljoin(base_url, href)
    # markdownify 默认会忽略标题和表格中的图片，显式保留所有位置。
    # 微信表格不用 th，按首行推断表头，避免生成空表头行把真实表头挤进数据行。
    return Converter(heading_style="ATX", bullets="-", escape_misc=True, table_infer_header=True,
                     keep_inline_images_in=["td", "th", "a", "p", "h1", "h2", "h3", "h4", "h5", "h6"]
                     ).convert(str(soup)).strip() + "\n"


def render_document(metadata: dict, body: str) -> str:
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front_matter}\n---\n\n{body}"

