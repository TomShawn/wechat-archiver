"""接收外部检索工具输出的纯文本或 Markdown，只提取微信公众号文章 URL。"""
import html
import re
from dataclasses import dataclass

from .models import ArchiveError
from .urls import article_id, validate_url


@dataclass
class URLBatch:
    urls: list[str]
    occurrences: int
    duplicates: int


def parse_url_batch(text: str) -> URLBatch:
    urls, seen = [], set()
    occurrences = 0
    for line in text.lstrip("\ufeff").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 支持 Markdown 链接、表格、编号列表和普通 URL。只读取文章地址，不信任随附标题/日期。
        for match in re.finditer(r'''https?://[^\s<>\[\]"'`()|，。；：！？）】》]+''', line):
            # 外部工具可能转义 Markdown 标签中的下划线、链接参数中的 &。
            value = re.sub(r"\\([_&])", r"\1", match.group())
            # html.unescape 会把原始查询参数 &timestamp 误解为无分号的 &times。
            # 仅解码带分号的显式实体，保留普通 URL 的参数字节。
            value = re.sub(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);",
                           lambda entity: html.unescape(entity.group()), value)
            value = value.rstrip("),.;，。；：！？）】》")
            try:
                url = validate_url(value, article=True)
            except ArchiveError:
                continue
            occurrences += 1
            identity = article_id(url)
            if identity not in seen:
                seen.add(identity)
                urls.append(url)
    if not urls:
        raise ArchiveError("输入文件未找到有效微信文章 URL；支持纯文本和 Markdown，CSV 请选择 --selected")
    return URLBatch(urls=urls, occurrences=occurrences, duplicates=occurrences - len(urls))

