import platform
import re
import statistics
from pathlib import Path

# Vision 只按列表中第一个语言选择识别模型：英文在前会整行丢弃中文，中文在前会把代码里的
# 半角标点识别成全角。因此先以中文识别，再用英文识别替换不含中文的行。
CJK_LANGUAGES = ("zh-Hans", "zh-Hant", "en-US")
LATIN_LANGUAGES = ("en-US",)
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 两次识别的行框重叠比例达到此值，视为同一行。
SAME_LINE_OVERLAP = 0.5


class VisionOCR:
    """同步 Apple Vision 识别，不调用网络，不使用语言模型纠错。"""

    def __init__(self, min_confidence=0.4):
        self.min_confidence = min_confidence
        self.unavailable_reason = None
        try:
            if platform.system() != "Darwin":
                raise ImportError("Apple Vision 仅在 macOS 可用")
            import Vision
            from Foundation import NSURL
            self.vision, self.NSURL = Vision, NSURL
        except ImportError as exc:
            self.unavailable_reason = f"本地 OCR 不可用：{exc}；运行 uv sync --extra ocr 安装，或使用 --no-ocr"

    def recognize(self, path: Path) -> dict:
        if self.unavailable_reason:
            return {"status": "error", "text": "", "error": self.unavailable_reason}
        try:
            lines = self.recognize_lines(path, CJK_LANGUAGES)
            passes = ["cjk"]
            if not lines or any(not CJK.search(line["text"]) for line in lines):
                lines = merge_latin_lines(lines, self.recognize_lines(path, LATIN_LANGUAGES))
                passes.append("latin")
            text = layout_lines(lines)
            # 保留 Vision 原始文字及坐标，不修正疑似拼写/代码错误。
            return {"status": "ok", "text": text, **summarize_confidence(lines, self.min_confidence),
                    "lines": lines, "passes": passes,
                    "layout": "estimated_from_bounding_boxes; indentation_requires_review"}
        except Exception as exc:
            return {"status": "error", "text": "", "error": f"{type(exc).__name__}: {exc}"}

    def recognize_lines(self, path: Path, languages) -> list[dict]:
        from Foundation import NSAutoreleasePool
        pool = NSAutoreleasePool.alloc().init()
        try:
            request = self.vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(self.vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(False)
            supported, error = request.supportedRecognitionLanguagesAndReturnError_(None)
            if error:
                raise RuntimeError(str(error))
            preferred = [name for name in languages if name in supported]
            if preferred:
                request.setRecognitionLanguages_(preferred)
            handler = self.vision.VNImageRequestHandler.alloc().initWithURL_options_(
                self.NSURL.fileURLWithPath_(str(path.resolve())), {})
            success, error = handler.performRequests_error_([request], None)
            if not success:
                raise RuntimeError(str(error))
            lines = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                candidate = candidates[0]
                box = observation.boundingBox()
                lines.append({"text": str(candidate.string()), "confidence": float(candidate.confidence()),
                              "x": float(box.origin.x), "y": float(box.origin.y),
                              "width": float(box.size.width), "height": float(box.size.height)})
            return lines
        finally:
            del pool


def overlap(a: dict, b: dict) -> float:
    """两个行框的交集占较小框面积的比例。"""
    width = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    height = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min(a["width"] * a["height"], b["width"] * b["height"])
    return width * height / smaller if smaller > 0 else 0.0


def merge_latin_lines(cjk_lines: list[dict], latin_lines: list[dict]) -> list[dict]:
    """含中文的行保留中文识别结果；其余行改用英文识别结果，英文识别额外找到的行也补上。"""
    merged, used = [], set()
    for line in cjk_lines:
        if CJK.search(line["text"]):
            merged.append(line)
            continue
        best = max(range(len(latin_lines)), key=lambda index: overlap(line, latin_lines[index]), default=None)
        if best is not None and best not in used and overlap(line, latin_lines[best]) >= SAME_LINE_OVERLAP:
            used.add(best)
            merged.append(latin_lines[best])
        else:
            merged.append(line)
    for index, line in enumerate(latin_lines):
        if index not in used and CJK.search(line["text"]) is None and not any(
                overlap(line, other) >= SAME_LINE_OVERLAP for other in merged):
            merged.append(line)
    return merged


def summarize_confidence(lines: list[dict], threshold: float) -> dict:
    """以中位数代表整图置信度，个别低分行单独计数，避免一行误识别拖低整张图。

    Vision 对中文只给出 0.3、0.5、1.0 几档：0.5 的行基本正确，0.3 多为图标、界面元素或水印，
    因此以中位数低于阈值（过半行为 0.3 档）作为整图需要核对的信号。
    """
    if not lines:
        return {"confidence": 1.0, "min_confidence": 1.0, "low_confidence_lines": 0, "low_confidence": False}
    scores = [line["confidence"] for line in lines]
    median = statistics.median(scores)
    low_lines = sum(score < threshold for score in scores)
    return {"confidence": median, "min_confidence": min(scores), "low_confidence_lines": low_lines,
            "low_confidence": median < threshold}


def layout_lines(lines: list[dict]) -> str:
    """以边框坐标估计阅读顺序和缩进；原始文字与坐标另行保留。"""
    if not lines:
        return ""
    ordered = sorted(lines, key=lambda line: (-round(line["y"] + line["height"] / 2, 3), line["x"]))
    rows = []
    for line in ordered:
        center = line["y"] + line["height"] / 2
        if rows and abs(rows[-1][0] - center) <= line["height"] * 0.45:
            rows[-1][1].append(line)
        else:
            rows.append((center, [line]))
    char_width = statistics.median(line["width"] / max(len(line["text"]), 1) for line in lines)
    left = min(line["x"] for line in lines)
    output = []
    for _, row in rows:
        row.sort(key=lambda line: line["x"])
        indent = min(40, max(0, round((row[0]["x"] - left) / max(char_width, 0.001))))
        output.append(" " * indent + "  ".join(line["text"] for line in row))
    return "\n".join(output)

