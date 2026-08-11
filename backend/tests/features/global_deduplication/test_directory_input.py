from pathlib import Path

from app.features.global_deduplication.directory_input import scan_batch


def test_scanner_recurses_sorts_and_skips_unsupported(tmp_path: Path) -> None:
    extraction = tmp_path / "batch" / "extraction"
    original = tmp_path / "batch" / "original"
    duplicate = tmp_path / "batch" / "duplicate"
    (extraction / "nested").mkdir(parents=True)
    original.mkdir()
    duplicate.mkdir()
    (extraction / "z.txt").write_text("z", encoding="utf-8")
    (extraction / "nested" / "a.md").write_text("a", encoding="utf-8")
    (extraction / "image.png").write_bytes(b"x")
    (original / "unscanned.txt").write_text("do not scan", encoding="utf-8")

    scanned = scan_batch(tmp_path / "batch")

    assert [item.relative_path.as_posix() for item in scanned.documents] == [
        "nested/a.md",
        "z.txt",
    ]
    assert scanned.extraction_root == extraction
