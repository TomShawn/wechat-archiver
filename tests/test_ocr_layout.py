import pytest

from wechat_archiver.ocr import layout_lines, merge_latin_lines, overlap, summarize_confidence


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


def scored(*scores):
    return [{"confidence": score} for score in scores]


def test_single_bad_line_does_not_flag_image():
    result = summarize_confidence(scored(*[1.0] * 9, 0.3), 0.4)
    assert result["confidence"] == 1.0 and result["min_confidence"] == 0.3
    assert result["low_confidence_lines"] == 1 and not result["low_confidence"]


def test_vision_mid_level_is_not_low():
    # Vision 中文常见的 0.5 档基本识别正确，不应整图标记。
    assert not summarize_confidence(scored(0.5, 0.5, 0.3, 1.0, 0.3), 0.4)["low_confidence"]


def test_mostly_lowest_level_flags_image():
    assert summarize_confidence(scored(0.3, 0.3, 0.3, 0.5, 1.0), 0.4)["low_confidence"]


def test_no_lines_is_confident():
    assert summarize_confidence([], 0.65) == {"confidence": 1.0, "min_confidence": 1.0,
                                              "low_confidence_lines": 0, "low_confidence": False}


def boxed(text, y, x=0.1, width=0.6, confidence=0.9):
    return {"text": text, "confidence": confidence, "x": x, "y": y, "width": width, "height": 0.1}


def test_overlap():
    assert overlap(boxed("a", 0.5), boxed("b", 0.5)) == pytest.approx(1.0)
    assert overlap(boxed("a", 0.5), boxed("b", 0.8)) == 0.0


def test_merge_replaces_only_latin_lines():
    cjk = [boxed("数据库集群", 0.8), boxed("SELECT count（*）FROM orders：", 0.5)]
    latin = [boxed("SELECT count(*) FROM orders;", 0.51)]
    assert [line["text"] for line in merge_latin_lines(cjk, latin)] == [
        "数据库集群", "SELECT count(*) FROM orders;"]


def test_merge_keeps_cjk_line_without_latin_match():
    cjk = [boxed("ok：", 0.5)]
    assert merge_latin_lines(cjk, [boxed("elsewhere", 0.1)])[0]["text"] == "ok："


def test_merge_adds_lines_only_latin_pass_found():
    cjk = [boxed("标题", 0.8)]
    latin = [boxed("Title", 0.8), boxed("footer note", 0.1)]
    assert [line["text"] for line in merge_latin_lines(cjk, latin)] == ["标题", "footer note"]


def test_merge_uses_latin_when_cjk_pass_empty():
    assert [line["text"] for line in merge_latin_lines([], [boxed("hello", 0.5)])] == ["hello"]
