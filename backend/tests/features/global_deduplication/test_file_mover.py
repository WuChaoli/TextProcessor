from pathlib import Path

from app.features.global_deduplication.file_mover import move_file, sha256


def test_existing_flat_destination_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "original" / "nested" / "same.txt"
    destination = tmp_path / "duplicate"
    source.parent.mkdir(parents=True)
    destination.mkdir()
    source.write_text("duplicate", encoding="utf-8")
    (destination / "same.txt").write_text("existing", encoding="utf-8")

    failure = move_file(
        source,
        destination,
        relative_path="nested/same.txt",
        expected_sha256=sha256(source),
    )

    assert failure is not None
    assert failure.code == "OUTPUT_CONFLICT"
    assert source.exists()


def test_move_file_moves_to_flat_destination(tmp_path: Path) -> None:
    source = tmp_path / "original" / "nested" / "same.txt"
    destination = tmp_path / "duplicate"
    source.parent.mkdir(parents=True)
    destination.mkdir()
    source.write_text("duplicate", encoding="utf-8")

    failure = move_file(
        source,
        destination,
        relative_path="nested/same.txt",
        expected_sha256=sha256(source),
    )

    assert failure is None
    assert not source.exists()
    assert (destination / "same.txt").read_text(encoding="utf-8") == "duplicate"
