from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


@dataclass(frozen=True)
class PreparedMarkdownResult:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class PublishedMarkdownResult:
    path: Path
    sha256: str
    size_bytes: int
    recovered: bool


class MarkdownCleaningResultPublisher:
    def __init__(
        self,
        *,
        output_roots: tuple[Path, ...],
        max_output_bytes: int | None = None,
        copy_chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if not output_roots:
            raise ValueError("输出根目录不能为空")
        if max_output_bytes is not None and max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须为正整数")
        if copy_chunk_bytes <= 0:
            raise ValueError("copy_chunk_bytes 必须为正整数")
        self._output_roots = tuple(self._normalize_root(root) for root in output_roots)
        self._max_output_bytes = max_output_bytes
        self._copy_chunk_bytes = copy_chunk_bytes

    def prepare(self, source: Path) -> PreparedMarkdownResult:
        try:
            descriptor = os.open(
                source,
                os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY,
            )
            with os.fdopen(descriptor, "rb") as stream:
                content = stream.read()
            content.decode("utf-8", errors="strict")
        except OSError as exc:
            raise _invalid_output("清洗结果读取失败") from exc
        except UnicodeDecodeError as exc:
            raise _invalid_output("清洗结果包含非法 UTF-8") from exc
        if not content:
            raise _invalid_output("清洗结果为空")
        if self._max_output_bytes is not None and len(content) > self._max_output_bytes:
            raise _invalid_output("清洗结果超过允许大小")
        return PreparedMarkdownResult(
            path=source,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def publish(
        self,
        prepared: PreparedMarkdownResult,
        target: Path,
        *,
        allow_recovery: bool,
    ) -> PublishedMarkdownResult:
        normalized_target = self._normalize_target(target)
        normalized_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            parent_handle = type(self)._open_directory_no_follow(
                normalized_target.parent
            )
        except OSError as exc:
            raise _output_conflict("目标目录无法安全打开") from exc
        temporary_name = f".markdown-cleaning-publish-{uuid.uuid4()}.tmp"
        temporary_fd: int | None = None
        try:
            try:
                existing_fd = self._open_relative(parent_handle, normalized_target.name)
            except FileNotFoundError:
                existing_fd = None
            if existing_fd is not None:
                return self._resolve_existing_fd(
                    prepared,
                    normalized_target,
                    existing_fd,
                    allow_recovery=allow_recovery,
                )

            temporary_fd = self._copy_to_exclusive_temporary(
                prepared.path,
                normalized_target.parent,
                temporary_name,
                parent_fd=parent_handle,
            )
            try:
                self._link_no_replace(
                    parent_handle,
                    temporary_name,
                    normalized_target.name,
                    temporary_fd,
                )
            except FileExistsError:
                existing_fd = self._open_relative(parent_handle, normalized_target.name)
                return self._resolve_existing_fd(
                    prepared,
                    normalized_target,
                    existing_fd,
                    allow_recovery=allow_recovery,
                )
            self._fsync_fd(temporary_fd)
            self._fsync_directory(parent_handle)
            return PublishedMarkdownResult(
                path=normalized_target,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                recovered=False,
            )
        except MarkdownCleaningProcessorError:
            raise
        finally:
            if temporary_fd is not None:
                self._remove_temporary(parent_handle, temporary_name, temporary_fd)
                os.close(temporary_fd)
            self._close_parent(parent_handle)

    @classmethod
    def _normalize_root(cls, root: Path) -> Path:
        normalized_root = root.resolve(strict=False)
        if not normalized_root.is_absolute():
            raise ValueError("输出根目录必须是绝对路径")
        if cls._has_link_or_junction_component(normalized_root):
            raise ValueError("输出根目录不安全")
        return normalized_root

    def _normalize_target(self, target: Path) -> Path:
        if not target.is_absolute():
            raise _output_conflict("目标路径不在允许输出目录")
        normalized = Path(os.path.abspath(target))
        if normalized.suffix.lower() != ".md" or not any(
            normalized.is_relative_to(root) for root in self._output_roots
        ):
            raise _output_conflict("目标路径不在允许输出目录")
        if self._has_link_or_junction_component(normalized.parent):
            raise _output_conflict("目标路径不在允许输出目录")
        return normalized

    @classmethod
    def _has_link_or_junction_component(cls, path: Path) -> bool:
        absolute = Path(os.path.abspath(path))
        cursor = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            cursor /= part
            if cursor.is_symlink() or (
                hasattr(os.path, "isjunction") and os.path.isjunction(cursor)
            ):
                return True
        return False

    @staticmethod
    def _is_regular_file(descriptor: int) -> None:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _output_conflict("结果文件不安全")

    @staticmethod
    def _open_directory_no_follow(path: Path) -> int:
        if os.name != "nt":
            return os.open(
                path,
                os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW,
            )
        return _WindowsPinnedDirectory.open(path)

    @staticmethod
    def _close_parent(parent_handle: int) -> None:
        if os.name == "nt":
            _WindowsPinnedDirectory.close(parent_handle)
        else:
            os.close(parent_handle)

    @staticmethod
    def _open_relative(parent_handle: int, name: str) -> int:
        if os.name == "nt":
            return _WindowsPinnedDirectory.open_relative(
                parent_handle, name, create=False
            )
        return os.open(
            name,
            os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY,
            dir_fd=parent_handle,
        )

    @classmethod
    def _resolve_existing_fd(
        cls,
        prepared: PreparedMarkdownResult,
        target: Path,
        descriptor: int,
        *,
        allow_recovery: bool,
    ) -> PublishedMarkdownResult:
        try:
            if not allow_recovery:
                raise _output_conflict("结果文件已存在")
            cls._is_regular_file(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                current = stream.read()
            if (
                len(current) == prepared.size_bytes
                and hashlib.sha256(current).hexdigest() == prepared.sha256
            ):
                return PublishedMarkdownResult(
                    path=target,
                    sha256=prepared.sha256,
                    size_bytes=prepared.size_bytes,
                    recovered=True,
                )
            raise _output_conflict("结果文件已存在")
        finally:
            os.close(descriptor)

    def _copy_to_exclusive_temporary(
        self,
        source: Path,
        target_parent: Path,
        temporary_name: str,
        parent_fd: int | None = None,
    ) -> int:
        assert parent_fd is not None
        if os.name == "nt":
            descriptor = _WindowsPinnedDirectory.open_relative(
                parent_fd,
                temporary_name,
                create=True,
            )
        else:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_BINARY,
                0o600,
                dir_fd=parent_fd,
            )
        try:
            source_fd = os.open(
                source, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
            )
            with os.fdopen(source_fd, "rb") as input_file:
                while chunk := input_file.read(self._copy_chunk_bytes):
                    os.write(descriptor, chunk)
            self._fsync_fd(descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _link_no_replace(
        parent_handle: int,
        temporary_name: str,
        target_name: str,
        temporary_fd: int,
    ) -> None:
        try:
            if os.name == "nt":
                _WindowsPinnedDirectory.link_no_replace(
                    temporary_fd,
                    parent_handle,
                    target_name,
                )
            else:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent_handle,
                    dst_dir_fd=parent_handle,
                )
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileExistsError(errno.EEXIST, "target exists") from exc
            raise _output_conflict("文件系统不支持安全的无覆盖发布") from exc

    @staticmethod
    def _remove_temporary(parent_handle: int, name: str, descriptor: int) -> None:
        try:
            if os.name == "nt":
                _WindowsPinnedDirectory.mark_delete(descriptor)
            else:
                os.unlink(name, dir_fd=parent_handle)
        except OSError:
            pass

    @staticmethod
    def _fsync_fd(descriptor: int) -> None:
        os.fsync(descriptor)

    @classmethod
    def _fsync_directory(cls, parent_handle: int) -> None:
        if os.name != "nt":
            cls._fsync_fd(parent_handle)


class _WindowsPinnedDirectory:
    """Windows NTFS operations relative to a pinned, non-reparse parent handle."""

    @staticmethod
    def open(path: Path) -> int:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000 | 0x00010000,  # read/write/delete
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    @staticmethod
    def close(handle: int) -> None:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)

    @staticmethod
    def open_relative(parent: int, name: str, *, create: bool) -> int:
        import msvcrt
        from ctypes import wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        buffer = ctypes.create_unicode_buffer(name)
        string = UnicodeString(
            len(name) * 2,
            (len(name) + 1) * 2,
            ctypes.cast(buffer, wintypes.LPWSTR),
        )
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            parent,
            ctypes.pointer(string),
            0x40,  # OBJ_CASE_INSENSITIVE
            None,
            None,
        )
        handle = wintypes.HANDLE()
        iosb = (ctypes.c_size_t * 2)()
        status = ctypes.WinDLL("ntdll").NtCreateFile(
            ctypes.byref(handle),
            0x0012019F if create else 0x00120089,
            ctypes.byref(attributes),
            ctypes.byref(iosb),
            None,
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            2 if create else 1,  # FILE_CREATE / FILE_OPEN
            0x00000020 | 0x00000040,  # synchronous, non-directory
            None,
            0,
        )
        if status < 0:
            if status & 0xFFFFFFFF in {0xC0000034, 0xC000003A}:
                raise FileNotFoundError(errno.ENOENT, "entry missing")
            if status & 0xFFFFFFFF in {0xC0000035, 0xC00000BA}:
                raise FileExistsError(errno.EEXIST, "entry exists")
            raise OSError(errno.EIO, "relative NT file operation failed")
        flags = os.O_BINARY | (os.O_RDWR if create else os.O_RDONLY)
        if handle.value is None:
            raise OSError(errno.EIO, "relative NT file operation returned no handle")
        return msvcrt.open_osfhandle(handle.value, flags)

    @staticmethod
    def link_no_replace(file_fd: int, parent: int, target_name: str) -> None:
        import msvcrt
        from ctypes import wintypes

        name_bytes = target_name.encode("utf-16-le")

        class FileLinkInfoHeader(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
            ]

        name_offset = FileLinkInfoHeader.FileNameLength.offset + ctypes.sizeof(
            wintypes.DWORD
        )
        size = name_offset + len(name_bytes)
        buffer = ctypes.create_string_buffer(size)
        header = FileLinkInfoHeader.from_buffer(buffer)
        header.ReplaceIfExists = False
        header.RootDirectory = parent
        header.FileNameLength = len(name_bytes)
        ctypes.memmove(
            ctypes.addressof(buffer) + name_offset,
            name_bytes,
            len(name_bytes),
        )
        iosb = (ctypes.c_size_t * 2)()
        status = ctypes.WinDLL("ntdll").NtSetInformationFile(
            msvcrt.get_osfhandle(file_fd),
            ctypes.byref(iosb),
            buffer,
            size,
            11,  # FileLinkInformation
        )
        if status < 0:
            error = status & 0xFFFFFFFF
            if error in {0xC0000035, 0xC00000BA}:
                raise FileExistsError(errno.EEXIST, "target exists")
            raise OSError(errno.ENOTSUP, "safe hardlink unavailable")

    @staticmethod
    def mark_delete(file_fd: int) -> None:
        import msvcrt

        delete = ctypes.c_ubyte(1)
        ok = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle(
            msvcrt.get_osfhandle(file_fd),
            4,  # FileDispositionInfo
            ctypes.byref(delete),
            ctypes.sizeof(delete),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())


def _invalid_output(message: str) -> MarkdownCleaningProcessorError:
    return MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        message,
    )


def _output_conflict(message: str) -> MarkdownCleaningProcessorError:
    return MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        message,
    )
