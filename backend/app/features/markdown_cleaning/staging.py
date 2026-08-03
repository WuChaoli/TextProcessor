from __future__ import annotations

import errno
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(path)
    )


def _has_link_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_link_or_junction(current):
            return True
    return False


class _StagingRootMissingError(Exception):
    """The configured staging root does not exist during cleanup."""


@dataclass(frozen=True, slots=True)
class StagingLayout:
    """Task-owned staging paths derived exclusively from server configuration."""

    staging_root: Path
    task_id: uuid.UUID

    @classmethod
    def for_task(cls, staging_root: Path, task_id: uuid.UUID) -> StagingLayout:
        if not isinstance(task_id, uuid.UUID):
            raise TypeError("task_id 必须是 UUID")
        lexical_root = Path(os.path.abspath(staging_root))
        if _has_link_component(lexical_root):
            raise ValueError("staging 根目录不安全")
        return cls(staging_root=lexical_root.resolve(strict=False), task_id=task_id)

    @property
    def root(self) -> Path:
        return self.staging_root / str(self.task_id)

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def original_source(self) -> Path:
        return self.input_dir / "source.original.md"

    @property
    def processor_source(self) -> Path:
        return self.input_dir / "source.md"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def result(self) -> Path:
        return self.output_dir / "result.md"

    @property
    def publish_part(self) -> Path:
        return self.output_dir / "publish.md.part"

    def prepare(self) -> None:
        with self._guard_staging_root(create=True) as root_fd:
            if root_fd is None:
                self._prepare_windows()
            else:
                self._prepare_posix(root_fd)

    def _prepare_windows(self) -> None:
        for directory in (self.root, self.input_dir, self.output_dir):
            if _is_link_or_junction(directory):
                raise ValueError("staging 任务目录不安全")
            directory.mkdir(mode=0o700, parents=False, exist_ok=True)
            if _is_link_or_junction(directory):
                raise ValueError("staging 任务目录不安全")
            try:
                directory.chmod(0o700)
            except OSError:
                # Windows ACLs do not implement POSIX modes; containment remains enforced.
                if os.name != "nt":
                    raise
        self._assert_safe()

    def _prepare_posix(self, root_fd: int) -> None:
        task_fd = self._open_or_create_directory(root_fd, str(self.task_id))
        try:
            for name in ("input", "output"):
                child_fd = self._open_or_create_directory(task_fd, name)
                os.close(child_fd)
        finally:
            os.close(task_fd)

    @staticmethod
    def _open_or_create_directory(parent_fd: int, name: str) -> int:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError:
            raise ValueError("staging 任务目录不安全") from None
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("staging 任务目录不安全")
            os.fchmod(descriptor, 0o700)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def cleanup(self) -> None:
        """Delete only the configured task directory, never a persisted DB path."""

        try:
            with self._guard_staging_root(create=False) as root_fd:
                if root_fd is None:
                    self._cleanup_windows()
                else:
                    self._cleanup_posix(root_fd)
        except _StagingRootMissingError:
            return

    def _cleanup_windows(self) -> None:
        task_root = self.staging_root / str(self.task_id)
        if _is_link_or_junction(task_root):
            raise ValueError("staging 任务目录不安全")
        if not task_root.exists():
            return
        quarantine = self.staging_root / (f".cleanup-{self.task_id}-{uuid.uuid4().hex}")
        try:
            os.replace(task_root, quarantine)
        except FileNotFoundError:
            return
        if _is_link_or_junction(quarantine):
            self._unlink_reparse_entry(quarantine)
            raise ValueError("staging 任务目录在清理时被替换")
        self._remove_tree_no_follow(quarantine)

    def _cleanup_posix(self, root_fd: int) -> None:
        task_name = str(self.task_id)
        try:
            task_metadata = os.stat(
                task_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(task_metadata.st_mode):
            raise ValueError("staging 任务目录不安全")
        quarantine_name = f".cleanup-{self.task_id}-{uuid.uuid4().hex}"
        try:
            os.rename(
                task_name,
                quarantine_name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        quarantine_metadata = os.stat(
            quarantine_name,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(quarantine_metadata.st_mode):
            os.unlink(quarantine_name, dir_fd=root_fd)
            raise ValueError("staging 任务目录在清理时被替换")
        self._remove_tree_no_follow_fd(root_fd, quarantine_name)

    @classmethod
    def _remove_tree_no_follow_fd(cls, parent_fd: int, name: str) -> None:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno not in {errno.ELOOP, errno.ENOTDIR}:
                raise
            os.unlink(name, dir_fd=parent_fd)
            return
        try:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    child_name = entry.name
                    if entry.is_symlink():
                        os.unlink(child_name, dir_fd=directory_fd)
                    elif entry.is_dir(follow_symlinks=False):
                        cls._remove_tree_no_follow_fd(directory_fd, child_name)
                    else:
                        os.unlink(child_name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)

    @classmethod
    def _remove_tree_no_follow(cls, root: Path) -> None:
        if _is_link_or_junction(root):
            cls._unlink_reparse_entry(root)
            return
        with os.scandir(root) as entries:
            for entry in entries:
                child = Path(entry.path)
                if entry.is_symlink() or _is_link_or_junction(child):
                    cls._unlink_reparse_entry(child)
                elif entry.is_dir(follow_symlinks=False):
                    cls._remove_tree_no_follow(child)
                else:
                    child.unlink()
        root.rmdir()

    @staticmethod
    def _unlink_reparse_entry(path: Path) -> None:
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            os.rmdir(path)
        else:
            path.unlink()

    def assert_safe_path(self, path: Path, *, must_exist: bool = False) -> Path:
        self._assert_safe()
        lexical = Path(os.path.abspath(path))
        try:
            lexical.relative_to(self.root)
        except ValueError:
            raise ValueError("staging 路径不安全") from None
        if _has_link_component(lexical):
            raise ValueError("staging 路径不安全")
        try:
            resolved = lexical.resolve(strict=must_exist)
        except OSError:
            raise ValueError("staging 路径不安全") from None
        if not resolved.is_relative_to(self.root):
            raise ValueError("staging 路径不安全")
        return resolved

    @contextmanager
    def _guard_staging_root(self, *, create: bool) -> Iterator[int | None]:
        self._assert_safe()
        if create:
            self.staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "nt":
            handle = self._open_windows_root_handle()
            try:
                self._validate_windows_root_handle(handle)
                yield None
                self._validate_windows_root_handle(handle)
            finally:
                self._close_windows_handle(handle)
            return

        descriptor = self._open_posix_root_fd()
        try:
            self._validate_posix_root_fd(descriptor)
            yield descriptor
            self._validate_posix_root_fd(descriptor)
        finally:
            os.close(descriptor)

    def _open_posix_root_fd(self) -> int:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(self.staging_root, flags)
        except FileNotFoundError:
            raise _StagingRootMissingError from None
        except OSError:
            raise ValueError("staging 根目录不安全") from None

    def _validate_posix_root_fd(self, descriptor: int) -> None:
        try:
            handle_metadata = os.fstat(descriptor)
            path_metadata = os.stat(self.staging_root, follow_symlinks=False)
        except OSError:
            raise ValueError("staging 根目录不安全") from None
        if (
            not stat.S_ISDIR(handle_metadata.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
            or handle_metadata.st_dev != path_metadata.st_dev
            or handle_metadata.st_ino != path_metadata.st_ino
        ):
            raise ValueError("staging 根目录不安全")

    def _open_windows_root_handle(self) -> int:
        import ctypes
        from ctypes import wintypes

        file_list_directory = 0x0001
        file_read_attributes = 0x0080
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        file_flag_open_reparse_point = 0x00200000

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
        handle = create_file(
            str(self.staging_root),
            file_list_directory | file_read_attributes,
            file_share_read
            | file_share_write,  # Deliberately excludes FILE_SHARE_DELETE.
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            error_code = ctypes.get_last_error()
            if error_code in {2, 3}:
                raise _StagingRootMissingError
            raise ctypes.WinError(error_code)
        return int(handle)

    def _validate_windows_root_handle(self, handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        file_attribute_directory = 0x00000010
        file_attribute_reparse_point = 0x00000400
        file_attribute_tag_info = 9

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        get_info.restype = wintypes.BOOL
        info = FileAttributeTagInfo()
        if not get_info(
            handle,
            file_attribute_tag_info,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not info.FileAttributes & file_attribute_directory or info.FileAttributes & (
            file_attribute_reparse_point
        ):
            raise ValueError("staging 根目录不安全")

        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_final_path.restype = wintypes.DWORD
        required = get_final_path(handle, None, 0, 0)
        if required == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        if get_final_path(handle, buffer, len(buffer), 0) == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        final_path = self._normalize_windows_handle_path(buffer.value)
        expected_path = self.staging_root.resolve(strict=True)
        if os.path.normcase(str(final_path)) != os.path.normcase(str(expected_path)):
            raise ValueError("staging 根目录不安全")

    @staticmethod
    def _normalize_windows_handle_path(raw_path: str) -> Path:
        if raw_path.startswith("\\\\?\\UNC\\"):
            raw_path = "\\\\" + raw_path[8:]
        elif raw_path.startswith("\\\\?\\"):
            raw_path = raw_path[4:]
        return Path(os.path.abspath(raw_path))

    @staticmethod
    def _close_windows_handle(handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(handle)

    def _assert_safe(self) -> None:
        expected_root = self.staging_root / str(self.task_id)
        if expected_root.parent != self.staging_root:
            raise ValueError("staging 任务目录不安全")
        if _has_link_component(self.staging_root):
            raise ValueError("staging 根目录不安全")
        if _is_link_or_junction(expected_root):
            raise ValueError("staging 任务目录不安全")
        if expected_root.exists():
            try:
                resolved = expected_root.resolve(strict=True)
            except OSError:
                raise ValueError("staging 任务目录不安全") from None
            if resolved != expected_root or not resolved.is_relative_to(
                self.staging_root
            ):
                raise ValueError("staging 任务目录不安全")
