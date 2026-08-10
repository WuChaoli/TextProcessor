import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

LocalPathAccessKind = Literal["input", "output"]


class LocalPathAccessError(RuntimeError):
    def __init__(self, kind: LocalPathAccessKind, reason: str) -> None:
        super().__init__("本地路径不可访问")
        self.kind = kind
        self.reason = reason


class LocalPathAccessPolicy:
    def preflight_input(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            raise LocalPathAccessError("input", "not_absolute")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise LocalPathAccessError("input", "unavailable") from None
        with self.open_regular_input(resolved):
            pass
        return resolved

    def preflight_output(
        self,
        raw_path: str,
        *,
        suffixes: frozenset[str],
    ) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            raise LocalPathAccessError("output", "not_absolute")
        if path.suffix.lower() not in suffixes:
            raise LocalPathAccessError("output", "unsupported_suffix")
        try:
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            raise LocalPathAccessError("output", "parent_unavailable") from None
        if not parent.is_dir():
            raise LocalPathAccessError("output", "parent_not_directory")
        if not os.access(parent, os.W_OK):
            raise LocalPathAccessError("output", "parent_not_writable")
        return parent / path.name

    @contextmanager
    def open_regular_input(self, path: Path) -> Generator[BinaryIO]:
        descriptor = -1
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise LocalPathAccessError("input", "not_regular_file")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise LocalPathAccessError("input", "not_regular_file")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                yield source
        except LocalPathAccessError:
            raise
        except (OSError, RuntimeError):
            raise LocalPathAccessError("input", "unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
