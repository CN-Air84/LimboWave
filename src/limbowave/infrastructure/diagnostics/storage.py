"""单写者 JSONL 存储：字节轮转、OS 文件锁、批量写；只由后台线程写日志。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import BinaryIO

from limbowave.infrastructure.diagnostics.models import LogConfig


class LogTargetInUseError(RuntimeError):
    """同一目录/名称已有写者；文件锁由 OS 在进程退出时释放。"""


class RotatingSink:
    def __init__(self, config: LogConfig) -> None:
        self.config = config
        self.path = config.directory / f"{config.name}.jsonl"
        self._lock_file: BinaryIO | None = None
        self._stream: BinaryIO | None = None
        self._size = 0
        self.write_calls = 0
        self.bytes_written = 0
        self.records_written = 0

    def acquire(self) -> None:
        self.config.directory.mkdir(parents=True, exist_ok=True)
        lock = (self.config.directory / f"{self.config.name}.lock").open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise LogTargetInUseError("Log target is locked or unavailable") from exc
        self._lock_file = lock

    def _open(self) -> None:
        if self._stream is not None:
            return
        # 断电留下半行时先补分隔符，避免下一条完整记录也粘进坏行。
        with self.path.open("a+b") as probe:
            self._size = probe.tell()
            if self._size:
                probe.seek(-1, os.SEEK_END)
                if probe.read(1) != b"\n":
                    probe.write(b"\n")
                    self._size += 1
        self._stream = self.path.open("ab", buffering=0)

    def _rotate(self) -> None:
        self.close_stream()
        if self.config.backup_count:
            for number in range(self.config.backup_count - 1, 0, -1):
                source = Path(f"{self.path}.{number}")
                if source.exists():
                    source.replace(Path(f"{self.path}.{number + 1}"))
            if self.path.exists():
                self.path.replace(Path(f"{self.path}.1"))
        else:
            with self.path.open("wb"):
                pass
        self._size = 0
        self._stream = self.path.open("ab", buffering=0)

    def write_batch(self, payloads: list[bytes]) -> None:
        self._open()
        chunk = bytearray()
        count = 0
        for payload in payloads:
            if len(payload) > self.config.max_file_bytes:
                raise ValueError("Record exceeds log file size")
            if self._size + len(chunk) + len(payload) > self.config.max_file_bytes:
                self._write(chunk, count)
                chunk.clear()
                count = 0
                self._rotate()
            chunk.extend(payload)
            count += 1
        self._write(chunk, count)

    def _write(self, chunk: bytearray, count: int) -> None:
        if not chunk:
            return
        assert self._stream is not None
        written = self._stream.write(chunk)
        self.bytes_written += written or 0
        self.write_calls += 1
        if written != len(chunk):
            raise OSError("Short log write")
        self._size += written
        self.records_written += count
        # FileIO 无 Python 用户态缓冲；不调用 fsync，持久化由 OS 页缓存负责。

    def close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        try:
            self.close_stream()
        finally:
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None
