import re
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .models import ArchiveError, Article, SharedArticle, UnavailableArticle
from .urls import validate_url

# 微信对不可访问文章返回的提示；出现在缺少正文的页面中时，视为永久失败。
UNAVAILABLE_MARKERS = {
    "链接已过期": "搜狗等搜索结果中的临时链接（src=11&signature=…）会过期；请在微信中打开文章，复制带 __biz 的永久链接",
    "该内容已被发布者删除": "文章已被作者删除，无法归档",
    "此内容因违规无法查看": "文章因违规被屏蔽，无法归档",
    "此内容被多人投诉": "文章因投诉被屏蔽，无法归档",
    "该公众号已迁移": "公众号已迁移，请在新公众号中找到文章的永久链接",
    "系统出错": "链接可能不完整或参数有误（例如复制时混入空格），请检查后重新复制",
}


def unavailable_article(text: str) -> UnavailableArticle | None:
    for marker, suggestion in UNAVAILABLE_MARKERS.items():
        if marker in text:
            return UnavailableArticle(f"微信返回“{marker}”，文章不可访问", suggestion)
    return None


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?", value)
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        return None


def parse_article(raw: bytes | str, source_url: str | None = None, *, local=False, title_override=None) -> Article:
    # bytes 输入交给 BeautifulSoup 识别声明的字符集，原始字节另存，不重新编码。
    soup = BeautifulSoup(raw, "lxml")
    root = soup.select_one("#js_content")
    if root is None and local:
        root = soup.select_one("article, main")
    if root is None and (error := unavailable_article(soup.get_text(" ", strip=True))):
        raise error
    if root is not None and root.find_parent(id="js_share_content"):
        source = soup.select_one("#js_share_source")
        original = source and (source.get("data-url") or source.get("href"))
        if not original:
            raise ArchiveError("这是转发的分享卡片，只含摘要，且页面中没有原文链接")
        raise SharedArticle(validate_url(original, article=True))
    if root is None:
        raise ArchiveError("未找到文章正文容器；可能是登录页、删除页或验证页，请手动保存真实文章 HTML 后导入")

    def text(selector):
        node = soup.select_one(selector)
        return node.get_text(" ", strip=True) if node else None

    def meta(name):
        node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return node.get("content") if node else None

    title = text("#activity-name") or meta("og:title") or text("h1") or (title_override if local else None)
    if not title or title in ("微信公众平台", "提示", "Site Unavailable"):
        raise ArchiveError("未找到可信文章标题")
    account = text("#js_name") or text(".profile_nickname") or meta("article:author")
    author = text("#js_author_name_text") or text("#js_author_name") or meta("author")
    if not author:
        # 日期和 IP 位置也使用 rich_media_meta_text；只接受顶层 author span。
        author = text("#meta_content > span.rich_media_meta_text")
    published_at, evidence = None, None
    for selector in ("#publish_time", "time"):
        node = soup.select_one(selector)
        value = (node.get("datetime") or node.get_text(" ", strip=True)) if node else None
        if parsed := parse_date(value):
            published_at, evidence = parsed, f"{selector}: {value}"
            break
    if not published_at:
        for name in ("article:published_time", "publish_date", "date"):
            if parsed := parse_date(meta(name)):
                published_at, evidence = parsed, f"meta {name}: {meta(name)}"
                break
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    if not published_at:
        match = re.search(r'''\b(?:createTime|publish_time)\s*=\s*["']([^"']+)["']''', scripts)
        if match and (parsed := parse_date(match.group(1))):
            published_at, evidence = parsed, match.group(0)
    if not published_at:
        match = re.search(r'''\bct\s*=\s*["']?(\d{10})\b''', scripts)
        if match:
            published_at = datetime.fromtimestamp(int(match.group(1)), ZoneInfo("Asia/Shanghai")).date().isoformat()
            evidence = match.group(0)
    player_wrappers = 0
    # 浏览器会把微信视频容器展开为分享/关注/倍速等控件，甚至带空头像 img。
    # 对已识别的微信视频容器保留 video 本体，移除非正文播放器界面。
    for wrapper in list(root.select('.video_iframe[id^="js_mp_video_container_"]')):
        video = wrapper.find("video")
        if video:
            wrapper.replace_with(copy.deepcopy(video))
            player_wrappers += 1
    for node in root.select("script, style, noscript, iframe, .qr_code_pc, .reward_area, .js_ad_link"):
        node.decompose()
    warnings = []
    if player_wrappers:
        warnings.append(f"已移除 {player_wrappers} 个微信视频播放器的导航、操作控件和头像，视频标记保留，媒体未下载")
    def image_source(image):
        return image.get("data-src") or image.get("data-original") or image.get("src")

    def is_empty_placeholder(image):
        if image_source(image):
            return False
        # 只识别微信生成的空图片壳；其他无源图片仍须报告失败，不能默默丢弃。
        bare = image.attrs == {"data-src": ""} or (
            set(image.attrs) == {"class"} and {"rich_pages", "wxw-img"}.issubset(image.get("class", []))
        )
        if not bare:
            return False
        # 情况一：空壳和真实图片是直接兄弟节点。
        if any(image_source(other) for other in image.parent.find_all("img", recursive=False) if other is not image):
            return True
        # 情况二：微信在真实图片后又追加了 <span><img/><img/>...</span>。
        group = image.parent
        if group.name != "span" or group.get_text(strip=True) or any(
            child.name != "img" for child in group.find_all(True)
        ):
            return False
        previous = group.previous_sibling
        while isinstance(previous, str) and not previous.strip():
            previous = previous.previous_sibling
        return getattr(previous, "name", None) == "img" and bool(image_source(previous))

    placeholders = 0
    for image in list(root.find_all("img")):
        if is_empty_placeholder(image):
            image.decompose()
            placeholders += 1
    if placeholders:
        warnings.append(f"忽略 {placeholders} 个真实图片旁的空 img 占位节点，原始 HTML 已保留")
    body_text = root.get_text("", strip=True)
    if not body_text and not root.find("img"):
        raise ArchiveError("正文容器为空，不能视为成功获取文章")
    if len(body_text) < 100:
        warnings.append("正文少于 100 字，需人工检查是否为短文、图片文章或内容缺失")
    if not published_at:
        warnings.append("发布日期未知，未根据搜索摘要或归档日期推算")
    if not account:
        warnings.append("公众号名称未知")
    if root.find(["video", "audio", "mpvideo", "mpvoice"]) or soup.select_one("#js_pc_qr_code + video"):
        warnings.append("文章包含音视频，当前只保存正文与图片，媒体需人工检查")
    # 微信代码框通常逐行存放于列表中，不能当作普通有序列表转换。
    for code_root in root.select(".code-snippet__fix"):
        if code_root.find_parent(class_="code-snippet__fix"):
            continue
        lines = code_root.select(".code-snippet__line")
        if lines:
            pre = soup.new_tag("pre")
            code = soup.new_tag("code")
            code.string = "\n".join(line.get_text().replace("\xa0", " ") for line in lines)
            pre.append(code)
            code_root.replace_with(pre)
    for pre in root.find_all("pre"):
        if pre.find("br"):
            for br in pre.find_all("br"):
                br.replace_with("\n")
        # 微信另一种代码结构把每行放在独立的直接子 code 中，通过 CSS 逐行显示。
        lines = pre.find_all("code", recursive=False)
        if len(lines) > 1 and "code-snippet__js" in pre.get("class", []):
            content = "\n".join(line.get_text().replace("\xa0", " ") for line in lines)
            language = pre.get("data-lang", "")
            pre.clear()
            code = soup.new_tag("code")
            if re.fullmatch(r"[a-zA-Z0-9_+-]+", language):
                code["class"] = [f"language-{language}"]
            code.string = content
            pre.append(code)
    # 复杂表格保留 HTML，避免合并单元格静默丢失。
    for table in root.find_all("table"):
        if table.find(attrs={"rowspan": True}) or table.find(attrs={"colspan": True}):
            warnings.append("复杂表格已保留 HTML 结构，需人工检查渲染")
    return Article(title=title.strip(), html=str(root), account=account, author=author,
                   published_at=published_at, source_url=source_url, date_evidence=evidence, warnings=warnings)

