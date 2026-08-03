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


class InvalidPreparedOutputError(MarkdownCleaningProcessorError):
    pass


class OutputConflictError(MarkdownCleaningProcessorError):
    pass


class PublicationSystemError(MarkdownCleaningProcessorError):
    pass


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
        self._root_identities = {
            root: self._path_identity(root) if root.is_dir() else None
            for root in self._output_roots
        }
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
        parent_handle = self._open_target_parent(normalized_target)
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
            self._verify_prepared_temporary(prepared, temporary_fd)
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
        except OSError as exc:
            raise _publication_system_error("文件系统发布失败") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
                self._remove_temporary(parent_handle, temporary_name)
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
            raise _invalid_output("目标路径不在允许输出目录")
        normalized = Path(os.path.abspath(target))
        if normalized.suffix.lower() != ".md" or not any(
            normalized.is_relative_to(root) for root in self._output_roots
        ):
            raise _invalid_output("目标路径不在允许输出目录")
        if self._has_link_or_junction_component(normalized.parent):
            raise _invalid_output("目标路径不在允许输出目录")
        return normalized

    @staticmethod
    def _path_identity(path: Path) -> tuple[int, int]:
        if os.name == "nt":
            handle = _WindowsPinnedDirectory.open(path)
            try:
                return _WindowsPinnedDirectory.identity(handle)
            finally:
                _WindowsPinnedDirectory.close(handle)
        metadata = path.stat(follow_symlinks=False)
        return metadata.st_dev, metadata.st_ino

    def _open_target_parent(self, target: Path) -> int:
        root = max(
            (
                candidate
                for candidate in self._output_roots
                if target.is_relative_to(candidate)
            ),
            key=lambda candidate: len(candidate.parts),
        )
        expected_identity = self._root_identities[root]
        if expected_identity is None:
            raise _publication_system_error("允许输出根目录不存在")
        current: int | None = None
        try:
            current = type(self)._open_directory_no_follow(root)
            if self._descriptor_identity(current) != expected_identity:
                raise _publication_system_error("允许输出根目录已变化")
            for component in target.parent.relative_to(root).parts:
                child = self._open_child_directory(current, component)
                self._close_parent(current)
                current = child
            result = current
            current = None
            return result
        except MarkdownCleaningProcessorError:
            raise
        except OSError as exc:
            raise _publication_system_error("目标目录无法安全打开") from exc
        finally:
            if current is not None:
                self._close_parent(current)

    @staticmethod
    def _descriptor_identity(descriptor: int) -> tuple[int, int]:
        if os.name == "nt":
            return _WindowsPinnedDirectory.identity(descriptor)
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _open_child_directory(parent_handle: int, name: str) -> int:
        if name in {"", ".", ".."} or Path(name).name != name:
            raise _invalid_output("目标目录组件不安全")
        if os.name == "nt":
            try:
                return _WindowsPinnedDirectory.open_directory_relative(
                    parent_handle, name, create=False
                )
            except FileNotFoundError:
                return _WindowsPinnedDirectory.open_directory_relative(
                    parent_handle, name, create=True
                )
        try:
            return os.open(
                name,
                os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=parent_handle,
            )
        except FileNotFoundError:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_handle)
            except FileExistsError:
                pass
            return os.open(
                name,
                os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=parent_handle,
            )

    @staticmethod
    def _verify_prepared_temporary(
        prepared: PreparedMarkdownResult,
        descriptor: int,
    ) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        if size != prepared.size_bytes or digest.hexdigest() != prepared.sha256:
            raise _invalid_output("待发布结果摘要与准备记录不一致")

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
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_BINARY,
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
            raise _publication_system_error("文件系统发布失败") from exc

    @staticmethod
    def _remove_temporary(parent_handle: int, name: str) -> None:
        try:
            if os.name == "nt":
                _WindowsPinnedDirectory.delete_relative(parent_handle, name)
            else:
                os.unlink(name, dir_fd=parent_handle)
        except FileNotFoundError:
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
        kernel32.CreateFileW.restype = wintypes.HANDLE
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
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(handle))

    @staticmethod
    def identity(handle: int) -> tuple[int, int]:
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("CreationTime", wintypes.FILETIME),
                ("LastAccessTime", wintypes.FILETIME),
                ("LastWriteTime", wintypes.FILETIME),
                ("VolumeSerialNumber", wintypes.DWORD),
                ("FileSizeHigh", wintypes.DWORD),
                ("FileSizeLow", wintypes.DWORD),
                ("NumberOfLinks", wintypes.DWORD),
                ("FileIndexHigh", wintypes.DWORD),
                ("FileIndexLow", wintypes.DWORD),
            ]

        information = ByHandleFileInformation()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        ]
        ok = kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(information)
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        file_index = (information.FileIndexHigh << 32) | information.FileIndexLow
        return information.VolumeSerialNumber, file_index

    @staticmethod
    def open_relative(parent: int, name: str, *, create: bool) -> int:
        import msvcrt

        handle = _WindowsPinnedDirectory._nt_open_relative(
            parent, name, create=create, directory=False
        )
        flags = os.O_BINARY | (os.O_RDWR if create else os.O_RDONLY)
        return msvcrt.open_osfhandle(handle, flags)

    @staticmethod
    def open_directory_relative(parent: int, name: str, *, create: bool) -> int:
        handle = _WindowsPinnedDirectory._nt_open_relative(
            parent, name, create=create, directory=True
        )
        if _WindowsPinnedDirectory.is_reparse_point(handle):
            _WindowsPinnedDirectory.close(handle)
            raise OSError(errno.ELOOP, "relative directory is a reparse point")
        return handle

    @staticmethod
    def _nt_open_relative(
        parent: int,
        name: str,
        *,
        create: bool,
        directory: bool,
    ) -> int:
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
        desired_access = (
            0x001F01FF if directory else (0x0012019F if create else 0x00120089)
        )
        create_options = 0x00000020 | (
            0x00000001 | 0x00200000 if directory else 0x00000040
        )
        status = ctypes.WinDLL("ntdll").NtCreateFile(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(iosb),
            None,
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            2 if create else 1,  # FILE_CREATE / FILE_OPEN
            create_options,
            None,
            0,
        )
        if status < 0:
            if status & 0xFFFFFFFF in {0xC0000034, 0xC000003A}:
                raise FileNotFoundError(errno.ENOENT, "entry missing")
            if status & 0xFFFFFFFF in {0xC0000035, 0xC00000BA}:
                raise FileExistsError(errno.EEXIST, "entry exists")
            raise OSError(errno.EIO, "relative NT file operation failed")
        if handle.value is None:
            raise OSError(errno.EIO, "relative NT file operation returned no handle")
        return handle.value

    @staticmethod
    def is_reparse_point(handle: int) -> bool:
        from ctypes import wintypes

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            ]

        information = FileAttributeTagInfo()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        ok = kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle),
            9,  # FileAttributeTagInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(information.FileAttributes & 0x00000400)

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
    def delete_relative(parent: int, name: str) -> None:
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
        status = ctypes.WinDLL("ntdll").NtDeleteFile(ctypes.byref(attributes))
        if status < 0:
            error = status & 0xFFFFFFFF
            if error in {0xC0000034, 0xC000003A}:
                raise FileNotFoundError(errno.ENOENT, "entry missing")
            raise OSError(errno.EIO, "relative NT delete failed")


def _invalid_output(message: str) -> InvalidPreparedOutputError:
    return InvalidPreparedOutputError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        message,
    )


def _output_conflict(message: str) -> OutputConflictError:
    return OutputConflictError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        message,
    )


def _publication_system_error(message: str) -> PublicationSystemError:
    return PublicationSystemError(MarkdownCleaningErrorCode.INTERNAL_ERROR, message)
