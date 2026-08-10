import os
from pathlib import Path

import pytest

from app.core.local_path_policy import LocalPathAccessError, LocalPathAccessPolicy


@pytest.fixture(autouse=True)
def db() -> None:
    """本地路径策略单测不依赖 PostgreSQL。"""


def test_preflight_input_accepts_readable_file_without_root_membership(
    tmp_path: Path,
) -> None:
    source = tmp_path / "business" / "input.txt"
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")

    validated = LocalPathAccessPolicy().preflight_input(str(source))

    assert validated == source.resolve(strict=True)


@pytest.mark.parametrize("value", ["relative.txt", "../escape.txt"])
def test_preflight_input_rejects_relative_path(value: str) -> None:
    with pytest.raises(LocalPathAccessError) as captured:
        LocalPathAccessPolicy().preflight_input(value)

    assert captured.value.kind == "input"
    assert captured.value.reason == "not_absolute"
    assert value not in str(captured.value)


def test_open_regular_input_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(LocalPathAccessError) as captured:
        with LocalPathAccessPolicy().open_regular_input(tmp_path):
            pass

    assert captured.value.kind == "input"
    assert captured.value.reason == "not_regular_file"


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有 os.mkfifo")
def test_open_regular_input_rejects_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)

    with pytest.raises(LocalPathAccessError) as captured:
        with LocalPathAccessPolicy().open_regular_input(fifo):
            pass

    assert captured.value.reason == "not_regular_file"


def test_preflight_input_accepts_valid_symlink_and_rejects_broken_link(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")
    valid = tmp_path / "valid.txt"
    broken = tmp_path / "broken.txt"
    try:
        valid.symlink_to(source)
        broken.symlink_to(tmp_path / "missing.txt")
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")

    assert LocalPathAccessPolicy().preflight_input(str(valid)) == source.resolve()
    with pytest.raises(LocalPathAccessError) as captured:
        LocalPathAccessPolicy().preflight_input(str(broken))
    assert captured.value.reason == "unavailable"


def test_preflight_output_accepts_existing_parent_without_creating_probe(
    tmp_path: Path,
) -> None:
    output = tmp_path / "business"
    output.mkdir()
    target = output / "result.md"

    validated = LocalPathAccessPolicy().preflight_output(
        str(target), suffixes=frozenset({".md"})
    )

    assert validated == target.resolve(strict=False)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("target_name", "reason"),
    [("relative.md", "not_absolute"), ("C:/missing/result.md", "parent_unavailable")],
)
def test_preflight_output_rejects_unusable_target(
    target_name: str,
    reason: str,
) -> None:
    with pytest.raises(LocalPathAccessError) as captured:
        LocalPathAccessPolicy().preflight_output(
            target_name, suffixes=frozenset({".md"})
        )

    assert captured.value.kind == "output"
    assert captured.value.reason == reason


def test_preflight_output_rejects_wrong_suffix(tmp_path: Path) -> None:
    with pytest.raises(LocalPathAccessError) as captured:
        LocalPathAccessPolicy().preflight_output(
            str(tmp_path / "result.json"), suffixes=frozenset({".md"})
        )

    assert captured.value.reason == "unsupported_suffix"
