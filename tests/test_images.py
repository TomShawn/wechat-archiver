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
