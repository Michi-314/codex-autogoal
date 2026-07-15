"""locking.py の単体テスト"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_autogoal.locking import FileLock, is_lock_stale


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "test.lock"


class TestFileLock:
    def test_acquire_and_release(self, lock_path):
        lock = FileLock(lock_path)
        assert lock.acquire()
        assert lock.locked
        assert lock_path.exists()
        lock.release()
        assert not lock.locked

    def test_double_acquire_fails(self, lock_path):
        """同じプロセスからは2回取得可能（flockの仕様）だが、
        別プロセスからは取得不可であることをテスト"""
        lock1 = FileLock(lock_path)
        assert lock1.acquire()

        # 別プロセスでロック取得を試みる
        code = f"""
import sys
sys.path.insert(0, "{Path(__file__).parent.parent / 'src'}")
from codex_autogoal.locking import FileLock
lock = FileLock("{lock_path}")
result = lock.acquire(blocking=False)
print("acquired" if result else "blocked")
lock.release()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=5
        )
        assert result.stdout.strip() == "blocked"

        lock1.release()

    def test_context_manager(self, lock_path):
        with FileLock(lock_path) as lock:
            if lock.locked:
                assert lock_path.exists()

    def test_pid_written(self, lock_path):
        lock = FileLock(lock_path)
        lock.acquire()
        content = lock_path.read_text().strip()
        assert content == str(os.getpid())
        lock.release()


class TestIsLockStale:
    def test_no_lock_file(self, lock_path):
        assert not is_lock_stale(lock_path)

    def test_empty_lock_file(self, lock_path):
        lock_path.write_text("")
        assert is_lock_stale(lock_path)

    def test_dead_pid(self, lock_path):
        # 存在しないPIDを書き込む
        lock_path.write_text("999999999")
        assert is_lock_stale(lock_path)

    def test_alive_pid(self, lock_path):
        # 自分のPID（生きている）
        lock_path.write_text(str(os.getpid()))
        assert not is_lock_stale(lock_path)

    def test_invalid_pid(self, lock_path):
        lock_path.write_text("not_a_number")
        assert is_lock_stale(lock_path)
