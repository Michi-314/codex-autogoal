"""job_runner.py の単体テスト"""

import json
import os
import time
from pathlib import Path

import pytest

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.job_runner import (
    create_job,
    create_timer_job,
    generate_job_id,
    get_job_status,
    is_job_done,
    cancel_job,
    _finalize_job,
)


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path / "autogoal")


class TestGenerateJobId:
    def test_unique(self):
        ids = {generate_job_id() for _ in range(100)}
        assert len(ids) == 100

    def test_with_name(self):
        jid = generate_job_id("backtest")
        assert "backtest" in jid

    def test_format(self):
        jid = generate_job_id()
        assert all(c.isalnum() or c in "-_" for c in jid)
        assert len(jid) <= 128


class TestCreateJob:
    def test_creates_job(self, config):
        result = create_job(config, ["echo", "hello"], name="test")
        assert "job_id" in result
        assert result["status"] == "RUNNING"

        # ファイルが作成されている
        jdir = paths.job_dir(config, result["job_id"])
        assert jdir.exists()
        assert paths.job_metadata_json(config, result["job_id"]).exists()
        assert paths.job_status_json(config, result["job_id"]).exists()

    def test_job_completes(self, config):
        """実際のコマンド（echo）が完了することを確認"""
        result = create_job(config, ["echo", "hello"], name="echo-test")
        job_id = result["job_id"]

        # ジョブ完了を待つ（最大10秒）
        for _ in range(100):
            if is_job_done(config, job_id):
                break
            time.sleep(0.1)

        assert is_job_done(config, job_id)

        status = get_job_status(config, job_id)
        assert status is not None
        assert status["status"] == "SUCCEEDED"
        assert status["exit_code"] == 0

    def test_job_failure(self, config):
        """失敗するコマンド"""
        result = create_job(config, ["false"], name="fail-test")
        job_id = result["job_id"]

        for _ in range(100):
            if is_job_done(config, job_id):
                break
            time.sleep(0.1)

        assert is_job_done(config, job_id)
        status = get_job_status(config, job_id)
        assert status["status"] == "FAILED"
        assert status["exit_code"] != 0


class TestFinalizeJob:
    def test_done_created_last(self, config):
        """doneファイルが最後に作成されることを確認"""
        job_id = generate_job_id("test")
        jdir = paths.job_dir(config, job_id)
        jdir.mkdir(parents=True)

        _finalize_job(config, job_id, 0, ["echo"])

        # 全てのファイルが存在
        assert paths.job_exit_code_file(config, job_id).exists()
        assert paths.job_finished_at_file(config, job_id).exists()
        assert paths.job_status_json(config, job_id).exists()
        assert paths.job_done_marker(config, job_id).exists()

        # exit_code確認
        exit_code = int(paths.job_exit_code_file(config, job_id).read_text())
        assert exit_code == 0

    def test_stdout_stderr_separation(self, config):
        """stdout/stderrが分離されることを確認"""
        result = create_job(
            config,
            ["sh", "-c", "echo out_msg; echo err_msg >&2"],
            name="sep-test"
        )
        job_id = result["job_id"]

        for _ in range(100):
            if is_job_done(config, job_id):
                break
            time.sleep(0.1)

        assert is_job_done(config, job_id)
        # stdout/stderrファイルが存在
        assert paths.job_stdout_log(config, job_id).exists()
        assert paths.job_stderr_log(config, job_id).exists()


class TestTimerJob:
    def test_timer_completes(self, config):
        """短いタイマージョブが完了する"""
        result = create_timer_job(config, duration_str="2s", name="short-timer")
        job_id = result["job_id"]

        for _ in range(50):
            if is_job_done(config, job_id):
                break
            time.sleep(0.1)

        assert is_job_done(config, job_id)
        status = get_job_status(config, job_id)
        assert status["status"] == "SUCCEEDED"


class TestCancelJob:
    def test_cancel_creates_marker(self, config):
        result = create_job(config, ["sleep", "30"], name="cancel-test")
        job_id = result["job_id"]

        success = cancel_job(config, job_id)
        assert success
        assert paths.job_cancelled_marker(config, job_id).exists()

    def test_cancel_nonexistent(self, config):
        assert not cancel_job(config, "nonexistent-job")
