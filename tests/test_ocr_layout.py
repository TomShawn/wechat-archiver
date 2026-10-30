from wechat_archiver.ocr import layout_lines


def line(text, x, y, width=None, height=0.05):
    return {"text": text, "x": x, "y": y, "width": width or len(text) * 0.02, "height": height}


def test_empty():
    assert layout_lines([]) == ""


def test_orders_top_to_bottom_and_merges_rows():
    # Vision 坐标原点在左下角，y 越大越靠上。
    lines = [line("second", 0.1, 0.5), line("first", 0.1, 0.8), line("right", 0.6, 0.805)]
    assert layout_lines(lines).splitlines() == ["first  right", "second"]


def test_estimates_indentation():
    lines = [line("def f():", 0.1, 0.8), line("return 1", 0.18, 0.7)]
    output = layout_lines(lines).splitlines()
    assert output[0] == "def f():"
    assert output[1].startswith("    return 1")
