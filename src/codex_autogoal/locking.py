"""fcntl.flockベースのファイルロック（watcher二重起動防止）"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from types import TracebackType


class FileLock:
    """排他ファイルロック。コンテキストマネージャで使用する。

    Usage:
        lock = FileLock(path)
        if lock.acquire():
            try:
                ...  # 排他処理
            finally:
                lock.release()

    または:
        with FileLock(path) as locked:
            if locked:
                ...  # 排他処理
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._locked = False

    def acquire(self, blocking: bool = False) -> bool:
        """ロックを取得する。

        Args:
            blocking: Trueなら取得できるまで待機。Falseなら即座に諦める。

        Returns:
            ロック取得成功ならTrue
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(
            str(self.path),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )

        try:
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"lock path is not a regular file: {self.path}")
            if info.st_uid != os.getuid():
                raise PermissionError(f"lock file is not owned by current user: {self.path}")
            if info.st_nlink != 1:
                raise ValueError(f"lock file has multiple hard links: {self.path}")
            os.fchmod(self._fd, 0o600)
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(self._fd, flags)
            # ロック取得成功 → PIDを書き込む
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, str(os.getpid()).encode())
            self._locked = True
            return True
        except BlockingIOError:
            # ロック取得失敗（別プロセスが保持中）
            os.close(self._fd)
            self._fd = None
            return False
        except Exception:
            os.close(self._fd)
            self._fd = None
            raise

    def release(self) -> None:
        """ロックを解放する。"""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._locked = False

    @property
    def locked(self) -> bool:
        return self._locked

    def __enter__(self) -> FileLock:
        self.acquire(blocking=False)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


def is_lock_stale(lock_path: Path) -> bool:
    """ロックファイルが存在し、記録されたPIDのプロセスが死んでいるか確認する。

    Returns:
        stale（=プロセスが存在しない）ならTrue
    """
    if not lock_path.exists():
        return False

    try:
        from codex_autogoal import paths
        pid_str = paths.read_private_text(lock_path).strip()
        if not pid_str:
            return True
        pid = int(pid_str)
        # PIDの存在確認（signal 0は実際にはシグナルを送らない）
        os.kill(pid, 0)
        return False  # プロセスは生きている
    except (ValueError, ProcessLookupError):
        return True  # PIDが不正またはプロセスが存在しない
    except PermissionError:
        return False  # プロセスは存在するがアクセス権がない
