"""バックグラウンドジョブランナー（二段構成）"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from codex_autogoal.config import Config, load_config
from codex_autogoal import paths
from codex_autogoal.duration import parse_duration
from codex_autogoal.process import process_fingerprint, sanitized_environment


def generate_job_id(name: str | None = None) -> str:
    """一意のジョブIDを生成する。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = os.urandom(3).hex()
    if name:
        # 名前を安全な形に変換
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]
        return f"{ts}-{safe_name}-{suffix}"
    return f"{ts}-{suffix}"


def create_job(
    config: Config,
    command: list[str],
    *,
    name: str | None = None,
    cwd: str | None = None,
    inherit_env: bool = False,
) -> dict:
    """バックグラウンドジョブを作成し、detachedプロセスとして起動する。

    二段構成: このCLIプロセスがjob runnerスクリプトをdetachで起動し、
    job runnerが対象コマンドを子として実行・待機・結果保存する。

    Returns:
        ジョブ情報dict
    """
    job_id = generate_job_id(name)
    jdir = paths.job_dir(config, job_id)
    jdir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()

    # メタデータ保存
    metadata = {
        "job_id": job_id,
        "name": name,
        "command": command,
        "cwd": cwd or os.getcwd(),
        "created_at": now,
        "type": "command",
        "environment": "inherited" if inherit_env else "allowlist",
    }
    _atomic_write(paths.job_metadata_json(config, job_id), metadata)
    _atomic_write(paths.job_command_json(config, job_id), {"command": command})

    # started_at記録
    paths.write_private_text(paths.job_started_at_file(config, job_id), now)

    # 初期status
    _atomic_write(paths.job_status_json(config, job_id), {
        "job_id": job_id,
        "status": "RUNNING",
        "started_at": now,
        "command": command,
    })

    # job runnerをdetachで起動
    _launch_job_runner(
        config,
        job_id,
        command,
        cwd=cwd,
        inherit_env=inherit_env,
    )

    return {
        "job_id": job_id,
        "status": "RUNNING",
        "stdout_path": str(paths.job_stdout_log(config, job_id)),
        "stderr_path": str(paths.job_stderr_log(config, job_id)),
    }


def create_timer_job(
    config: Config,
    duration_str: str | None = None,
    at_str: str | None = None,
    *,
    name: str | None = None,
) -> dict:
    """タイマージョブを作成する。指定時間後にdoneになるだけのジョブ。

    Returns:
        ジョブ情報dict
    """
    if duration_str:
        td = parse_duration(duration_str)
        seconds = int(td.total_seconds())
    elif at_str:
        from codex_autogoal.duration import parse_datetime_or_duration
        target_dt = parse_datetime_or_duration(at_str)
        now_dt = datetime.now(timezone.utc)
        seconds = max(0, int((target_dt - now_dt).total_seconds()))
    else:
        raise ValueError("--after または --at のいずれかを指定してください")

    job_id = generate_job_id(name or "timer")
    jdir = paths.job_dir(config, job_id)
    jdir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()

    metadata = {
        "job_id": job_id,
        "name": name or "timer",
        "command": ["timer", str(seconds)],
        "cwd": os.getcwd(),
        "created_at": now,
        "type": "timer",
        "timer_seconds": seconds,
    }
    _atomic_write(paths.job_metadata_json(config, job_id), metadata)

    paths.write_private_text(paths.job_started_at_file(config, job_id), now)

    _atomic_write(paths.job_status_json(config, job_id), {
        "job_id": job_id,
        "status": "RUNNING",
        "started_at": now,
        "command": ["timer", str(seconds)],
        "type": "timer",
    })

    # タイマー用job runnerをdetachで起動
    _launch_timer_runner(config, job_id, seconds)

    return {
        "job_id": job_id,
        "status": "RUNNING",
        "stdout_path": str(paths.job_stdout_log(config, job_id)),
        "stderr_path": str(paths.job_stderr_log(config, job_id)),
    }


def _launch_job_runner(
    config: Config,
    job_id: str,
    command: list[str],
    *,
    cwd: str | None = None,
    inherit_env: bool = False,
) -> None:
    """job runnerプロセスをdetachで起動する。"""
    runner_cmd = [
        sys.executable, "-m", "codex_autogoal.job_runner",
        "--mode", "command",
        "--job-id", job_id,
        "--home", str(config.home),
        "--",
    ] + command

    if cwd:
        runner_cmd.insert(-len(command) - 1, "--cwd")
        runner_cmd.insert(-len(command) - 1, cwd)

    log_path = paths.job_stdout_log(config, job_id)
    err_path = paths.job_stderr_log(config, job_id)

    # ログファイル作成
    env = _detached_python_env(inherit_env=inherit_env)
    with paths.open_private_write(log_path) as stdout_f, \
         paths.open_private_write(err_path) as stderr_f:
        proc = subprocess.Popen(
            runner_cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            start_new_session=True,
            close_fds=True,
            cwd=cwd,
            env=env,
        )

    # PID記録
    paths.write_private_text(paths.job_pid_file(config, job_id), str(proc.pid))


def _launch_timer_runner(
    config: Config,
    job_id: str,
    seconds: int,
) -> None:
    """タイマー用job runnerプロセスをdetachで起動する。"""
    runner_cmd = [
        sys.executable, "-m", "codex_autogoal.job_runner",
        "--mode", "timer",
        "--job-id", job_id,
        "--home", str(config.home),
        "--seconds", str(seconds),
    ]

    log_path = paths.job_stdout_log(config, job_id)
    err_path = paths.job_stderr_log(config, job_id)
    env = _detached_python_env()
    with paths.open_private_write(log_path) as stdout_f, \
         paths.open_private_write(err_path) as stderr_f:
        proc = subprocess.Popen(
            runner_cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            start_new_session=True,
            close_fds=True,
            env=env,
        )

    paths.write_private_text(paths.job_pid_file(config, job_id), str(proc.pid))


def _detached_python_env(*, inherit_env: bool = False) -> dict[str, str]:
    """Keep the package importable for detached source/editable runs."""
    env = sanitized_environment(inherit=inherit_env)
    src_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (src_root, current) if part
    )
    return env


def run_job_runner_main() -> None:
    """job runnerのメインエントリポイント（detachedプロセスとして実行される）。

    このプロセスが:
    1. 対象コマンドを子プロセスとして実行
    2. waitpidで終了を待機
    3. 結果ファイルを書き込み
    4. 最後にdoneマーカーをatomicに作成
    """
    import argparse
    paths.secure_umask()

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["command", "timer"])
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--seconds", type=int, default=0)
    parser.add_argument("command_args", nargs="*")

    # "--" の後のコマンドを取得
    args, remaining = parser.parse_known_args()
    if remaining and remaining[0] == "--":
        remaining = remaining[1:]
    command_args = args.command_args + remaining

    config = Config(home=Path(args.home))
    paths.harden_runtime_permissions(config)
    job_id = paths.validate_identifier(args.job_id, kind="job ID")

    if args.mode == "timer":
        _run_timer(config, job_id, args.seconds)
    else:
        _run_command(config, job_id, command_args, cwd=args.cwd)


def _run_command(config: Config, job_id: str, command: list[str], *, cwd: str | None = None) -> None:
    """対象コマンドを実行し、結果を保存する。"""
    stdout_path = paths.job_stdout_log(config, job_id)
    stderr_path = paths.job_stderr_log(config, job_id)

    exit_code = 1
    try:
        with paths.open_private_write(stdout_path) as stdout_f, \
             paths.open_private_write(stderr_path) as stderr_f:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=cwd,
                start_new_session=True,
            )
            # PID更新（実際の子プロセスPID）
            paths.write_private_text(paths.job_pid_file(config, job_id), str(proc.pid))
            try:
                pgid = os.getpgid(proc.pid)
                fingerprint = process_fingerprint(proc.pid)
                if fingerprint:
                    _atomic_write(paths.job_process_identity_json(config, job_id), {
                        "pid": proc.pid,
                        "pgid": pgid,
                        "fingerprint": fingerprint,
                    })
            except ProcessLookupError:
                # A short-lived command may exit before identity capture. Its result
                # remains valid, but a later kill request will fail closed.
                pass
            exit_code = _wait_with_log_limit(
                proc,
                stdout_path,
                stderr_path,
                config.max_job_log_bytes,
            )
    except Exception as e:
        # コマンド実行失敗
        paths.write_private_text(stderr_path, f"ジョブ実行エラー: {e}\n")
        exit_code = 127

    _finalize_job(config, job_id, exit_code, command)


def _run_timer(config: Config, job_id: str, seconds: int) -> None:
    """タイマーを実行する。"""
    stdout_path = paths.job_stdout_log(config, job_id)

    # キャンセル検知用のループ
    elapsed = 0
    while elapsed < seconds:
        # キャンセル確認
        if paths.job_cancelled_marker(config, job_id).exists():
            paths.write_private_text(
                stdout_path,
                f"タイマーがキャンセルされました ({elapsed}/{seconds}秒)\n",
            )
            _finalize_job(config, job_id, 130, ["timer", str(seconds)])
            return
        sleep_time = min(1, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time

    paths.write_private_text(stdout_path, f"タイマー完了: {seconds}秒\n")
    _finalize_job(config, job_id, 0, ["timer", str(seconds)])


def _finalize_job(
    config: Config,
    job_id: str,
    exit_code: int,
    command: list[str],
) -> None:
    """ジョブ完了処理。doneマーカーは最後にatomicに作成する。"""
    now = datetime.now(timezone.utc).isoformat()

    # 1. 終了コード
    paths.write_private_text(paths.job_exit_code_file(config, job_id), str(exit_code))

    # 2. finished_at
    paths.write_private_text(paths.job_finished_at_file(config, job_id), now)

    # 3. status.json更新
    started_at = ""
    try:
        started_at = paths.read_private_text(
            paths.job_started_at_file(config, job_id)
        ).strip()
    except (OSError, ValueError):
        pass

    status = "SUCCEEDED" if exit_code == 0 else "FAILED"
    _atomic_write(paths.job_status_json(config, job_id), {
        "job_id": job_id,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": now,
        "command": command,
    })

    # 4. doneマーカー（最後にatomicに作成）
    done_path = paths.job_done_marker(config, job_id)
    fd, tmp = tempfile.mkstemp(dir=str(done_path.parent), prefix=".done_")
    os.write(fd, now.encode())
    os.fsync(fd)
    os.close(fd)
    os.replace(tmp, str(done_path))


def get_job_status(config: Config, job_id: str) -> dict | None:
    """ジョブの状態を取得する。"""
    status_path = paths.job_status_json(config, job_id)
    if not paths.is_private_regular_file(status_path):
        return None
    try:
        return json.loads(paths.read_private_text(status_path))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def is_job_done(config: Config, job_id: str) -> bool:
    """ジョブが完了しているか確認する。"""
    return paths.is_private_regular_file(paths.job_done_marker(config, job_id))


def cancel_job(config: Config, job_id: str, *, kill: bool = False) -> bool:
    """ジョブをキャンセルする。

    Args:
        kill: Trueならプロセスも停止する

    Returns:
        キャンセル成功ならTrue
    """
    jdir = paths.job_dir(config, job_id)
    if not jdir.exists():
        return False

    # cancelledマーカー作成
    paths.touch_private(paths.job_cancelled_marker(config, job_id))

    if kill:
        pid_path = paths.job_pid_file(config, job_id)
        if pid_path.exists():
            try:
                pid = int(paths.read_private_text(pid_path).strip())
                identity = _read_process_identity(config, job_id)
                if not identity or identity.get("pid") != pid:
                    return False
                # 対象コマンド専用のプロセスグループへ送る。job runner
                # 自身は別グループなので、終了結果を確実にfinalizeできる。
                pgid = os.getpgid(pid)
                if pgid != pid or pgid != identity.get("pgid") or pgid == os.getpgrp():
                    return False
                if process_fingerprint(pid) != identity.get("fingerprint"):
                    return False
                os.killpg(pgid, signal.SIGTERM)
                # 5秒待ってまだ生きていればSIGKILL
                for _ in range(50):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except (ValueError, ProcessLookupError, PermissionError):
                pass

    return True


def _read_process_identity(config: Config, job_id: str) -> dict | None:
    try:
        data = json.loads(paths.read_private_text(
            paths.job_process_identity_json(config, job_id)
        ))
        if not isinstance(data, dict) or not data.get("fingerprint"):
            return None
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _wait_with_log_limit(
    proc: subprocess.Popen,
    stdout_path: Path,
    stderr_path: Path,
    max_bytes: int,
) -> int:
    """Stop a detached job if its retained stdout/stderr exceed the configured cap."""
    if max_bytes <= 0:
        raise ValueError("CODEX_AUTOGOAL_MAX_JOB_LOG_BYTES must be positive")
    while proc.poll() is None:
        total = sum(
            path.stat().st_size if path.exists() else 0
            for path in (stdout_path, stderr_path)
        )
        if total > max_bytes:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait()
            with paths.open_private_append(stderr_path) as stream:
                stream.write(
                    f"\nAutoGoal log limit exceeded: {total} > {max_bytes} bytes\n"
                )
            return 125
        time.sleep(0.1)
    return int(proc.returncode or 0)


def _atomic_write(path: Path, data: dict) -> None:
    """JSONファイルをatomicに書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        content = json.dumps(data, ensure_ascii=False, indent=2)
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# __main__ エントリポイント（python -m codex_autogoal.job_runner で呼ばれる）
if __name__ == "__main__":
    run_job_runner_main()
