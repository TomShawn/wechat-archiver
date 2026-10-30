from wechat_archiver.markdown import convert_html
from wechat_archiver.quality import check_content

LAYOUT = """<table><tbody>
<tr><td><section style="text-align:center"><img src="assets/a.png" alt="图片">
<blockquote><p>OCR 提取内容（请对照原图核对）</p><pre><code>第一行
第二行</code></pre></blockquote></section></td></tr>
<tr><td><p>说明文字</p></td></tr>
</tbody></table>"""


def test_single_column_layout_table_becomes_blocks():
    body = convert_html(LAYOUT)
    assert "|" not in body
    assert "![图片](assets/a.png)" in body
    assert "> ```\n> 第一行\n> 第二行\n> ```" in body
    assert "说明文字" in body


def test_layout_table_passes_content_check():
    source = LAYOUT.replace(
        '<blockquote><p>OCR 提取内容（请对照原图核对）</p><pre><code>第一行\n第二行</code></pre></blockquote>', "")
    checks = check_content(source, convert_html(LAYOUT))
    assert checks["body_text_equal_ignoring_whitespace"] and checks["code_blocks_equal"]


def test_nested_layout_tables():
    body = convert_html(f"<table><tr><td>{LAYOUT}</td></tr></table>")
    assert "|" not in body and "![图片](assets/a.png)" in body


def test_data_table_kept_as_markdown_table():
    body = convert_html("<table><tr><th>名称</th><th>值</th></tr><tr><td>a</td><td>1</td></tr></table>")
    assert "| 名称 | 值 |" in body and "| a | 1 |" in body


def test_empty_layout_table_dropped():
    assert convert_html("<p>前</p><table><tbody><tr></tr></tbody></table><p>后</p>").count("|") == 0
