import platform
import statistics
from pathlib import Path


class VisionOCR:
    """同步 Apple Vision 识别，不调用网络，不使用语言模型纠错。"""

    def __init__(self, min_confidence=0.65):
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
            from Foundation import NSAutoreleasePool
            pool = NSAutoreleasePool.alloc().init()
            try:
                request = self.vision.VNRecognizeTextRequest.alloc().init()
                request.setRecognitionLevel_(self.vision.VNRequestTextRecognitionLevelAccurate)
                request.setUsesLanguageCorrection_(False)
                languages, error = request.supportedRecognitionLanguagesAndReturnError_(None)
                if error:
                    raise RuntimeError(str(error))
                preferred = [name for name in ("en-US", "zh-Hans") if name in languages]
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
                text = layout_lines(lines)
                confidence = min((line["confidence"] for line in lines), default=1.0)
                # 保留 Vision 原始文字及坐标，不修正疑似拼写/代码错误。
                return {"status": "ok", "text": text, "confidence": confidence,
                        "low_confidence": confidence < self.min_confidence, "lines": lines,
                        "layout": "estimated_from_bounding_boxes; indentation_requires_review"}
            finally:
                del pool
        except Exception as exc:
            return {"status": "error", "text": "", "error": f"{type(exc).__name__}: {exc}"}


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

