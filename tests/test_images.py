import threading
import time

from conftest import png

from wechat_archiver.images import process_images
from wechat_archiver.models import ArchiveError

COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


class SlowFetcher:
    def __init__(self, config, fail=()):
        self.config = config
        self.fail = set(fail)
        self.calls = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def get(self, url, *, image=False, **kwargs):
        with self.lock:
            self.calls.append(url)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.1)
            if url in self.fail:
                raise ArchiveError("图片请求被拒绝：HTTP 403")
            index = int(url.rsplit("/", 1)[1].split(".")[0])
            return png(COLORS[index]), "image/png", url
        finally:
            with self.lock:
                self.active -= 1


def article(*names):
    return "<div>" + "".join(f'<img data-src="https://mmbiz.qpic.cn/{name}.png">' for name in names) + "</div>"


def test_downloads_run_concurrently_and_keep_order(config, tmp_path):
    config.image_workers = 4
    fetcher = SlowFetcher(config)
    html, records = process_images(article(0, 1, 2, 3), tmp_path / "assets", fetcher)
    assert fetcher.peak > 1
    assert [record["position"] for record in records] == [1, 2, 3, 4]
    assert all(record["status"] == "saved" for record in records)
    assert len({record["path"] for record in records}) == 4
    for record in records:
        assert record["path"] in html
        assert (tmp_path / record["path"]).is_file()


def test_single_worker_is_serial(config, tmp_path):
    config.image_workers = 1
    fetcher = SlowFetcher(config)
    process_images(article(0, 1, 2), tmp_path / "assets", fetcher)
    assert fetcher.peak == 1


def test_duplicate_sources_downloaded_once(config, tmp_path):
    fetcher = SlowFetcher(config)
    _, records = process_images(article(0, 1, 0), tmp_path / "assets", fetcher)
    assert len(fetcher.calls) == 2
    assert records[0]["path"] == records[2]["path"]


def test_one_failed_image_does_not_affect_others(config, tmp_path):
    fetcher = SlowFetcher(config, fail={"https://mmbiz.qpic.cn/1.png"})
    html, records = process_images(article(0, 1, 2), tmp_path / "assets", fetcher)
    assert [record["status"] for record in records] == ["saved", "failed", "saved"]
    assert "HTTP 403" in records[1]["error"]
    assert "图片 2 保存失败" in html


def test_offline_skips_remote_downloads(config, tmp_path):
    fetcher = SlowFetcher(config)
    _, records = process_images(article(0), tmp_path / "assets", fetcher, offline=True)
    assert fetcher.calls == []
    assert records[0]["status"] == "failed" and "离线" in records[0]["error"]


def test_cached_images_are_not_downloaded(config, tmp_path):
    cached = tmp_path / "cached.png"
    cached.write_bytes(png(COLORS[2]))
    fetcher = SlowFetcher(config)
    _, records = process_images(article(0, 1), tmp_path / "assets", fetcher,
                                cached_images={"https://mmbiz.qpic.cn/0.png": {"file": cached}})
    assert fetcher.calls == ["https://mmbiz.qpic.cn/1.png"]
    assert records[0]["status"] == "saved" and not records[0]["downloaded"]


def test_checkpoint_written_for_new_downloads(config, tmp_path):
    seen = []
    process_images(article(0, 1, 2), tmp_path / "assets", SlowFetcher(config),
                   checkpoint_callback=lambda records: seen.append(len(records)), checkpoint_interval=0)
    assert seen == [1, 2, 3]


def test_checkpoint_writes_are_throttled(config, tmp_path):
    seen = []
    process_images(article(0, 1, 2), tmp_path / "assets", SlowFetcher(config),
                   checkpoint_callback=lambda records: seen.append(len(records)), checkpoint_interval=60)
    assert seen == []


def test_no_checkpoint_when_nothing_new(config, tmp_path):
    cached = tmp_path / "cached.png"
    cached.write_bytes(png(COLORS[0]))
    seen = []
    process_images(article(0), tmp_path / "assets", SlowFetcher(config),
                   cached_images={"https://mmbiz.qpic.cn/0.png": {"file": cached}},
                   checkpoint_callback=lambda records: seen.append(len(records)), checkpoint_interval=0)
    assert seen == []


class FakeOCR:
    def recognize(self, path):
        return {"status": "ok", "text": f"文字{path.name[:4]}\n第二行", "low_confidence": False}


def test_ocr_block_placed_outside_inline_formatting(config, tmp_path):
    from wechat_archiver.markdown import convert_html
    html = ('<p><strong><span><img data-src="https://mmbiz.qpic.cn/0.png">'
            '<img data-src="https://mmbiz.qpic.cn/1.png"></span></strong>后文</p>')
    out, records = process_images(html, tmp_path / "assets", SlowFetcher(config), ocr_engine=FakeOCR())
    body = convert_html(out)
    assert "```**" not in body and "**```" not in body
    fences = [line for line in body.splitlines() if line.lstrip("> ").startswith("```")]
    assert all(line.strip("> ") == "```" for line in fences) and len(fences) == 4
    # 两张图的 OCR 块保持图片顺序。
    first, second = (record["path"].split("/")[1][:4] for record in records)
    assert body.index("文字" + first) < body.index("文字" + second)
