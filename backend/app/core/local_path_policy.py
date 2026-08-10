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
            descriptor = self._open_read_descriptor(path)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise LocalPathAccessError("input", "not_regular_file")
        except LocalPathAccessError:
            raise
        except (OSError, RuntimeError):
            raise LocalPathAccessError("input", "unavailable") from None
        try:
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                yield source
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _open_read_descriptor(path: Path) -> int:
        if os.name != "nt":
            return os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))

        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        invalid_handle = wintypes.HANDLE(-1).value
        handle = create_file(
            str(path),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00000080 | 0x00200000,
            None,
        )
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except Exception:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            raise
        return descriptor
