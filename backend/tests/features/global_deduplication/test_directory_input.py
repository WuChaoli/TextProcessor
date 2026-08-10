from pathlib import Path

from app.features.global_deduplication.directory_input import scan_batch


def test_scanner_recurses_sorts_and_skips_unsupported(tmp_path: Path) -> None:
    original = tmp_path / "batch" / "original"
    duplicate = tmp_path / "batch" / "duplicate"
    (original / "nested").mkdir(parents=True)
    duplicate.mkdir()
    (original / "z.txt").write_text("z", encoding="utf-8")
    (original / "nested" / "a.md").write_text("a", encoding="utf-8")
    (original / "image.png").write_bytes(b"x")

    scanned = scan_batch(tmp_path / "batch")

    assert [item.relative_path.as_posix() for item in scanned.documents] == [
        "nested/a.md",
        "z.txt",
    ]
