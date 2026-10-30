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


def delivered(config):
    [target] = [path for path in config.path("output_dir").iterdir() if not path.name.startswith(".")]
    return target


def test_delivered_images_are_hardlinked_to_checkpoint(config, tmp_path):
    run(config, {"path": str(write_page(tmp_path / "src"))})
    [image] = (delivered(config) / "assets").iterdir()
    assert image.stat().st_nlink >= 2


def test_stale_checkpoint_images_not_delivered(config, tmp_path):
    page = write_page(tmp_path / "src")
    checkpoints = config.path("database").parent / "checkpoints"
    run(config, {"path": str(page)})
    [checkpoint] = checkpoints.iterdir()
    (checkpoint / "assets" / "stale.png").write_bytes(png((0, 0, 0)))
    archiver = Archiver(config)
    try:
        archiver.run([{"kind": "html", "offline": True, "path": str(page)}], force=True)
        latest = Path(archiver.database.get(digest(str(page.resolve()).encode()))["local_path"])
    finally:
        archiver.close()
    assert len(list((latest / "assets").iterdir())) == 1
    assert not (latest / "assets" / "stale.png").exists()


def test_link_or_copy_falls_back_to_copy(tmp_path, monkeypatch):
    from wechat_archiver import storage
    source = tmp_path / "a.png"
    source.write_bytes(b"data")

    def cross_device(*args):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(storage.os, "link", cross_device)
    storage.link_or_copy(source, tmp_path / "b.png")
    assert (tmp_path / "b.png").read_bytes() == b"data"
    assert source.stat().st_nlink == 1


def test_batch_export_hardlinks_images(config, tmp_path):
    from wechat_archiver.batch import export_batch
    page = write_page(tmp_path / "src")
    inputs = [{"kind": "html", "offline": True, "path": str(page)}]
    archiver = Archiver(config)
    try:
        summary = archiver.run(inputs)
        export_batch(archiver, inputs, tmp_path / "batch", summary)
    finally:
        archiver.close()
    [key_dir] = (tmp_path / "batch" / "assets").iterdir()
    [image] = key_dir.iterdir()
    assert image.stat().st_nlink >= 3
    assert f"assets/{key_dir.name}/{image.name}" in next((tmp_path / "batch").glob("*.md")).read_text("utf-8")


LAYOUT_PAGE = """<html><body><h1 id="activity-name">排版表格</h1><a id="js_name">号</a>
<em id="publish_time">2026-10-01</em>
<div id="js_content"><p>这是一段用于测试排版表格的正文。</p>
<table><tbody><tr><td><section><img src="img/a.png" alt="图片"></section></td></tr></tbody></table>
</div></body></html>"""


def test_images_in_layout_table_are_kept(config, tmp_path):
    page = write_page(tmp_path / "src")
    page.write_text(LAYOUT_PAGE, "utf-8")
    summary = run(config, {"path": str(page)})
    assert summary.succeeded == 1, summary.errors
    [image] = (delivered(config) / "assets").iterdir()
    assert f"](assets/{image.name})" in (delivered(config) / "index.md").read_text("utf-8")


def test_unreferenced_saved_image_marks_partial(config, tmp_path, monkeypatch):
    import wechat_archiver.archive as archive_module
    original = archive_module.convert_html
    monkeypatch.setattr(archive_module, "convert_html",
                        lambda html, base_url=None: original(html, base_url).split("![")[0] + "\n")
    page = write_page(tmp_path / "src")
    summary = run(config, {"path": str(page)})
    assert summary.partial == 1
    assert any(error["reason"] == "图片已保存但未出现在转换后的正文中" for error in summary.errors)
    quality = __import__("json").loads((delivered(config) / "quality.json").read_text("utf-8"))
    assert quality["checks"]["saved_images_referenced"] is False
    assert any("未出现在正文中（位置 1）" in warning for warning in quality["warnings"])


def test_batch_export_twice_to_same_directory(config, tmp_path):
    from wechat_archiver.batch import export_batch
    page = write_page(tmp_path / "src")
    inputs = [{"kind": "html", "offline": True, "path": str(page)}]
    archiver = Archiver(config)
    try:
        summary = archiver.run(inputs)
        export_batch(archiver, inputs, tmp_path / "batch", summary)
        summary = archiver.run(inputs, force=True)
        export_batch(archiver, inputs, tmp_path / "batch", summary)
    finally:
        archiver.close()
    [key_dir] = (tmp_path / "batch" / "assets").iterdir()
    assert len(list(key_dir.iterdir())) == 1


def test_link_or_copy_replaces_different_file(tmp_path):
    from wechat_archiver.storage import link_or_copy
    source, target = tmp_path / "a.png", tmp_path / "b.png"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    link_or_copy(source, target)
    assert target.read_bytes() == b"new" and target.samefile(source)
