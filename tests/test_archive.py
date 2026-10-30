from pathlib import Path

from conftest import png

from wechat_archiver.archive import Archiver
from wechat_archiver.storage import digest

PAGE = """<html><body><h1 id="activity-name">本地文章</h1><a id="js_name">号</a>
<em id="publish_time">2026-10-01</em>
<div id="js_content"><p>{body}</p><img src="img/a.png"></div></body></html>"""


def write_page(directory: Path, body="这是一段用于测试归档流程的正文。") -> Path:
    (directory / "img").mkdir(parents=True, exist_ok=True)
    (directory / "img" / "a.png").write_bytes(png())
    page = directory / "page.html"
    page.write_text(PAGE.format(body=body), "utf-8")
    return page


def run(config, *specs):
    archiver = Archiver(config)
    try:
        return archiver.run([{"kind": "html", "offline": True, **spec} for spec in specs])
    finally:
        archiver.close()


def test_import_html_archives_markdown_and_images(config, tmp_path):
    page = write_page(tmp_path / "src")
    summary = run(config, {"path": str(page)})
    assert (summary.succeeded, summary.failed) == (1, 0), summary.errors
    [target] = [path for path in config.path("output_dir").iterdir() if not path.name.startswith(".")]
    document = (target / "index.md").read_text("utf-8")
    assert "title: 本地文章" in document
    assert "用于测试归档流程的正文" in document
    [image] = (target / "assets").iterdir()
    assert f"assets/{image.name}" in document


def test_rerun_skips_intact_archive(config, tmp_path):
    page = write_page(tmp_path / "src")
    run(config, {"path": str(page)})
    summary = run(config, {"path": str(page)})
    assert (summary.succeeded, summary.skipped) == (0, 1)


def test_same_content_at_other_path_is_duplicate(config, tmp_path):
    run(config, {"path": str(write_page(tmp_path / "a"))})
    summary = run(config, {"path": str(write_page(tmp_path / "b"))})
    assert (summary.succeeded, summary.skipped) == (0, 1)


def test_missing_file_fails_without_crashing(config, tmp_path):
    summary = run(config, {"path": str(tmp_path / "missing.html")})
    assert summary.failed == 1 and "不存在" in summary.errors[0]["reason"]


def status_of(config, path):
    archiver = Archiver(config)
    try:
        return archiver.database.get(digest(str(Path(path).resolve()).encode()))
    finally:
        archiver.close()


def test_failed_reprocess_keeps_duplicate_status(config, tmp_path):
    run(config, {"path": str(write_page(tmp_path / "a"))})
    page_b = write_page(tmp_path / "b")
    run(config, {"path": str(page_b)})
    before = status_of(config, page_b)
    assert before["status"] == "duplicate"
    page_b.write_text("<html><body>不是文章</body></html>", "utf-8")
    archiver = Archiver(config)
    try:
        summary = archiver.run([{"kind": "html", "offline": True, "path": str(page_b)}], force=True)
    finally:
        archiver.close()
    after = status_of(config, page_b)
    assert summary.failed == 1
    assert after["status"] == "duplicate" and after["local_path"] == before["local_path"]
    assert after["error"]


def test_failed_reprocess_keeps_partial_status(config, tmp_path):
    page = write_page(tmp_path / "src")
    (tmp_path / "src" / "img" / "a.png").unlink()
    run(config, {"path": str(page)})
    before = status_of(config, page)
    assert before["status"] == "partial"
    page.write_text("<html><body>不是文章</body></html>", "utf-8")
    archiver = Archiver(config)
    try:
        archiver.run([{"kind": "html", "offline": True, "path": str(page)}], force=True)
    finally:
        archiver.close()
    after = status_of(config, page)
    assert after["status"] == "partial" and after["local_path"] == before["local_path"]


def test_first_failure_is_recorded_as_failed(config, tmp_path):
    page = tmp_path / "bad.html"
    page.write_text("<html><body>不是文章</body></html>", "utf-8")
    run(config, {"path": str(page)})
    assert status_of(config, page)["status"] == "failed"
