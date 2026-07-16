"""autogoal CLI（メインエントリポイント）"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from codex_autogoal.config import Config, load_config
from codex_autogoal import paths
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    TERMINAL_STATUSES,
    now_iso,
)
from codex_autogoal.runner import build_prompt, run_codex_session
from codex_autogoal.locking import FileLock, is_lock_stale
from codex_autogoal.job_runner import get_job_status, is_job_done, cancel_job
from codex_autogoal.process import process_fingerprint


# デフォルトプロトコルテンプレート
_DEFAULT_PROTOCOL = """\
あなたはAutoGoalモードで動作している。

与えられた目的が検証済みで達成されるまで、調査、実装、テスト、修正、再検証を自律的に進めること。

各ターンの終了時には、最終メッセージの最後の非空行に、必ず次の形式のシグナルを1つだけ出力すること。

AUTOGOAL_SIGNAL: {"state":"continue","reason":"..."}
AUTOGOAL_SIGNAL: {"state":"wait","job_id":"...","reason":"..."}
AUTOGOAL_SIGNAL: {"state":"done","reason":"..."}
AUTOGOAL_SIGNAL: {"state":"blocked","reason":"..."}

状態の選択規則:

1. 即座に実行できる有益な作業が残っている場合はcontinue。
2. 外部の長時間ジョブが完了するまで有益な作業がない場合はwait。
3. 目的と完了条件が検証済みで満たされた場合のみdone。
4. ユーザー入力なしでは進めない場合のみblocked。

長時間処理について:

- 長時間かかる可能性のあるコマンドをフォアグラウンドで実行し続けてはならない。
- sleep、watch、tail -f、ポーリングループを使って待機してはならない。
- 同じ状態確認コマンドを繰り返してはならない。
- sandbox内から`autogoal-job start/timer`を起動してはならない。
- control stateはCodexへ書き込み許可されない。長時間ジョブが必要ならblockedを返し、
  ユーザーが信頼済みターミナルから明示的に起動・接続するまで待つこと。
- 処理時間の予測だけで再開時刻を決めてはならない。

doneを返す前に以下を確認すること:

- 要求された変更が実装されている
- 関連テストが実行されている
- テスト結果が確認されている
- 未解決事項が隠されていない
- バックグラウンドジョブが放置されていない
- 最終結果と検証内容を説明している
"""


def main() -> None:
    paths.secure_umask()
    parser = argparse.ArgumentParser(
        prog="autogoal",
        description="Codex AutoGoal Supervisor",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # install
    sub.add_parser("install", help="Codex Hookを設定する")

    # start
    p_start = sub.add_parser("start", help="AutoGoalセッションを開始する")
    p_start.add_argument("--prompt", help="タスクのプロンプト")
    p_start.add_argument("--prompt-file", help="プロンプトファイル")
    p_start.add_argument("--cwd", help="作業ディレクトリ")
    p_start.add_argument("--sandbox", default="workspace-write",
                         choices=["read-only", "workspace-write", "danger-full-access"],
                         help="サンドボックスモード")
    p_start.add_argument("--model", help="使用するモデル")
    p_start.add_argument(
        "--bypass-hook-trust",
        action="store_true",
        help="自動化用に未trustのHookをこの実行だけ許可する",
    )

    # status
    p_status = sub.add_parser("status", help="セッション状態を確認する")
    p_status.add_argument("session_id", help="セッションID")
    p_status.add_argument("--json", dest="output_json", action="store_true")

    # list
    p_list = sub.add_parser("list", help="セッション一覧を表示する")
    p_list.add_argument("--json", dest="output_json", action="store_true")
    p_list.add_argument("--status", dest="filter_status", help="状態でフィルタ")

    # logs
    p_logs = sub.add_parser("logs", help="セッションログを表示する")
    p_logs.add_argument("session_id", help="セッションID")
    p_logs.add_argument("--follow", "-f", action="store_true", help="リアルタイム表示")
    p_logs.add_argument("--events", action="store_true", help="イベントログ表示")

    # cancel
    p_cancel = sub.add_parser("cancel", help="セッションをキャンセルする")
    p_cancel.add_argument("session_id", help="セッションID")
    p_cancel.add_argument("--kill-jobs", action="store_true",
                          help="バックグラウンドジョブも停止する")

    # recover
    sub.add_parser("recover", help="WAITINGセッションを復旧する")

    # doctor
    sub.add_parser("doctor", help="環境を診断する")

    args = parser.parse_args()
    config = load_config()
    paths.harden_runtime_permissions(config)

    try:
        if args.command == "install":
            _cmd_install(config)
        elif args.command == "start":
            _cmd_start(config, args)
        elif args.command == "status":
            _cmd_status(config, args)
        elif args.command == "list":
            _cmd_list(config, args)
        elif args.command == "logs":
            _cmd_logs(config, args)
        elif args.command == "cancel":
            _cmd_cancel(config, args)
        elif args.command == "recover":
            _cmd_recover(config)
        elif args.command == "doctor":
            _cmd_doctor(config)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n中断されました", file=sys.stderr)
        sys.exit(130)


# ============================================================
# install
# ============================================================

def _cmd_install(config: Config) -> None:
    """Codex Hookを設定する"""
    config_path = paths.codex_config_toml()

    # バックアップ作成
    if config_path.exists():
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = config_path.parent / f"config.toml.autogoal-backup-{ts}"
        shutil.copy2(config_path, backup)
        print(f"バックアップ作成: {backup}")

    # Hookスクリプトのパス
    stop_hook_script = Path(__file__).parent / "hooks" / "stop.py"
    pre_tool_hook_script = Path(__file__).parent / "hooks" / "pre_tool_use.py"
    python_bin = sys.executable

    # 既存設定を読み取り
    existing_content = ""
    if config_path.exists():
        existing_content = config_path.read_text()

    # Hook設定が既に存在するか確認
    if "# --- AutoGoal Hook設定 ---" in existing_content:
        print("AutoGoal Hookは既にインストール済みです")
        return

    # Hook設定を追加。hooksは現行Codexでdefault-onなので、既存の
    # [features] tableを書き換えず、ユーザーの明示設定を尊重する。
    hook_config = f"""
# --- AutoGoal Hook設定 ---
[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "{python_bin} {stop_hook_script}"
timeout = 10
statusMessage = "Processing AutoGoal state"

[[hooks.PreToolUse]]
matcher = "^Bash$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "{python_bin} {pre_tool_hook_script}"
timeout = 5
statusMessage = "Checking AutoGoal command policy"
# --- End AutoGoal Hook設定 ---
"""

    combined = existing_content.rstrip() + "\n" + hook_config
    try:
        tomllib.loads(combined)
    except tomllib.TOMLDecodeError as exc:
        print(f"エラー: Hook追記後のconfig.tomlが不正です: {exc}", file=sys.stderr)
        print("既存config.tomlは変更していません", file=sys.stderr)
        sys.exit(1)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(combined)

    # Runtime state is control-plane data. Codex never receives write access
    # to this directory, and the protocol remains an immutable package resource.
    paths.ensure_private_dir(config.home)

    # ディレクトリ作成
    paths.ensure_private_dir(paths.state_dir(config))
    paths.ensure_private_dir(paths.jobs_dir(config))
    paths.harden_runtime_permissions(config)

    print(f"✓ Hook設定を追加しました: {config_path}")
    print("✓ プロトコルはパッケージ内read-only resourceを使用します")
    print(f"✓ 状態ディレクトリを作成しました: {config.home}")
    print()
    print("次のステップ:")
    print("  autogoal doctor  # 環境診断")
    print('  autogoal start --prompt "タスク内容"  # セッション開始')


# ============================================================
# start
# ============================================================

def _cmd_start(config: Config, args) -> None:
    """AutoGoalセッションを開始する"""
    # プロンプト取得
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.exists():
            print(f"エラー: プロンプトファイルが見つかりません: {prompt_path}", file=sys.stderr)
            sys.exit(1)
        user_prompt = prompt_path.read_text()
    elif args.prompt:
        user_prompt = args.prompt
    else:
        print("エラー: --prompt または --prompt-file を指定してください", file=sys.stderr)
        sys.exit(1)

    # Never load prompt instructions from the model-adjacent runtime home.
    protocol = _DEFAULT_PROTOCOL

    # プロンプト組み立て
    full_prompt = build_prompt(user_prompt, protocol)

    cwd = args.cwd or os.getcwd()

    print(f"[autogoal] AutoGoalセッションを開始します", file=sys.stderr)
    print(f"[autogoal] cwd: {cwd}", file=sys.stderr)
    print(f"[autogoal] sandbox: {args.sandbox}", file=sys.stderr)

    # Codexセッション起動
    exit_code = run_codex_session(
        config,
        full_prompt,
        cwd=cwd,
        sandbox=args.sandbox,
        model=args.model,
        bypass_hook_trust=args.bypass_hook_trust,
    )

    sys.exit(exit_code)


# ============================================================
# status
# ============================================================

def _cmd_status(config: Config, args) -> None:
    """セッション状態を表示する"""
    sdir = paths.session_dir(config, args.session_id)
    mgr = StateManager(sdir)
    state = mgr.read()

    if state is None:
        print(f"エラー: セッションが見つかりません: {args.session_id}", file=sys.stderr)
        sys.exit(1)

    # watcher/codex プロセス確認
    watcher_alive = False
    codex_alive = False
    if state.watcher_pid:
        try:
            os.kill(state.watcher_pid, 0)
            watcher_alive = True
        except (ProcessLookupError, PermissionError):
            pass
    if state.codex_pid:
        try:
            os.kill(state.codex_pid, 0)
            codex_alive = True
        except (ProcessLookupError, PermissionError):
            pass

    # ジョブ状態
    job_info = None
    if state.current_job_id:
        job_info = get_job_status(config, state.current_job_id)

    info = {
        "session_id": state.session_id,
        "cwd": state.cwd,
        "status": state.status.value,
        "current_job_id": state.current_job_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "continuation_count": state.continuation_count,
        "resume_count": state.resume_count,
        "token_usage": {
            "input_tokens": state.token_usage.input_tokens,
            "output_tokens": state.token_usage.output_tokens,
        },
        "last_reason": state.last_reason,
        "watcher_pid": state.watcher_pid,
        "watcher_alive": watcher_alive,
        "codex_alive": codex_alive,
        "job_status": job_info,
    }

    if args.output_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        for k, v in info.items():
            if isinstance(v, dict):
                print(f"{k}:")
                for kk, vv in v.items():
                    print(f"  {kk}: {vv}")
            else:
                print(f"{k}: {v}")


# ============================================================
# list
# ============================================================

def _cmd_list(config: Config, args) -> None:
    """セッション一覧を表示する"""
    state_root = paths.state_dir(config)
    if not state_root.exists():
        print("セッションはありません")
        return

    sessions = []
    for d in sorted(state_root.iterdir()):
        if not d.is_dir():
            continue
        mgr = StateManager(d)
        state = mgr.read()
        if state is None:
            continue
        if args.filter_status and state.status.value != args.filter_status.upper():
            continue
        sessions.append({
            "session_id": state.session_id,
            "status": state.status.value,
            "cwd": state.cwd,
            "created_at": state.created_at,
            "continuation_count": state.continuation_count,
            "last_reason": state.last_reason[:60],
        })

    if args.output_json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
    else:
        if not sessions:
            print("セッションはありません")
            return
        # テーブル表示
        print(f"{'STATUS':<25} {'TURNS':>5} {'SESSION_ID':<40} {'REASON'}")
        print("-" * 100)
        for s in sessions:
            print(
                f"{s['status']:<25} {s['continuation_count']:>5} "
                f"{s['session_id']:<40} {s['last_reason']}"
            )


# ============================================================
# logs
# ============================================================

def _cmd_logs(config: Config, args) -> None:
    """セッションログを表示する"""
    sdir = paths.session_dir(config, args.session_id)

    if args.events:
        log_path = paths.events_jsonl(config, args.session_id)
    else:
        log_path = paths.codex_jsonl(config, args.session_id)

    if not paths.is_private_regular_file(log_path):
        print(f"エラー: ログが見つかりません: {log_path}", file=sys.stderr)
        sys.exit(1)

    if args.follow:
        # tail -f 的な動作（人間用）
        import subprocess
        subprocess.run(["tail", "-f", str(log_path)])
    else:
        print(paths.read_private_text(log_path, max_bytes=100 * 1024 * 1024), end="")


# ============================================================
# cancel
# ============================================================

def _cmd_cancel(config: Config, args) -> None:
    """セッションをキャンセルする"""
    sdir = paths.session_dir(config, args.session_id)
    mgr = StateManager(sdir)
    state = mgr.read()

    if state is None:
        print(f"エラー: セッションが見つかりません: {args.session_id}", file=sys.stderr)
        sys.exit(1)

    # cancelledマーカー作成
    paths.touch_private(paths.cancelled_marker(config, args.session_id))

    # 状態更新
    if state.status not in TERMINAL_STATUSES:
        mgr.transition(state, SessionStatus.CANCELLED, reason="ユーザーキャンセル")

    # watcherを停止
    if state.watcher_pid:
        try:
            if (
                state.watcher_fingerprint
                and process_fingerprint(state.watcher_pid) == state.watcher_fingerprint
            ):
                os.kill(state.watcher_pid, signal.SIGTERM)
                print(f"watcher (PID {state.watcher_pid}) にSIGTERM送信")
            else:
                print("watcherのprocess identity不一致のため停止を拒否しました")
        except (ProcessLookupError, PermissionError):
            pass

    # ジョブ停止（オプション）
    if args.kill_jobs and state.current_job_id:
        cancel_job(config, state.current_job_id, kill=True)
        print(f"ジョブ {state.current_job_id} をキャンセルしました")

    print(f"セッション {args.session_id} をキャンセルしました")


# ============================================================
# recover
# ============================================================

def _cmd_recover(config: Config) -> None:
    """WAITINGセッションを復旧する"""
    state_root = paths.state_dir(config)
    if not state_root.exists():
        print("セッションはありません")
        return

    recovered = 0
    for d in state_root.iterdir():
        if not d.is_dir():
            continue

        mgr = StateManager(d)
        state = mgr.read()
        if state is None:
            continue

        if state.status != SessionStatus.WAITING:
            continue

        session_id = state.session_id
        job_id = state.current_job_id

        # キャンセル確認
        if paths.cancelled_marker(config, session_id).exists():
            print(f"  スキップ (キャンセル済み): {session_id}")
            continue

        # watcher lockの確認
        lock_path = paths.watcher_lock(config, session_id)
        lock = FileLock(lock_path)
        if not lock.acquire(blocking=False):
            # 誰かがロック中 → watcherが生きている可能性
            if not is_lock_stale(lock_path):
                print(f"  スキップ (watcher稼働中): {session_id}")
                continue
            # stale lock
            print(f"  stale lock検出: {session_id}")
        else:
            lock.release()

        if job_id and is_job_done(config, job_id):
            # ジョブ完了済み → watcher再起動
            print(f"  復旧 (ジョブ完了済み): {session_id}, job={job_id}")
            _relaunch_watcher(config, session_id, job_id)
            recovered += 1
        elif job_id:
            # ジョブ未完了 → watcher再起動
            print(f"  復旧 (ジョブ実行中): {session_id}, job={job_id}")
            _relaunch_watcher(config, session_id, job_id)
            recovered += 1
        else:
            # ジョブIDなし → 不整合
            print(f"  不整合: {session_id} (job_idなし)")
            mgr.transition(state, SessionStatus.BLOCKED_STATE_CORRUPT,
                           reason="WAITINGだがjob_idがありません")

    print(f"\n復旧完了: {recovered}件")


def _relaunch_watcher(config: Config, session_id: str, job_id: str) -> None:
    """watcherを再起動する"""
    from codex_autogoal.hooks.stop import _launch_watcher

    import logging
    logger = logging.getLogger(f"autogoal.recover.{session_id}")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

    _launch_watcher(config, session_id, job_id, logger)


# ============================================================
# doctor
# ============================================================

def _cmd_doctor(config: Config) -> None:
    """環境を診断する"""
    results: list[tuple[str, bool, str]] = []

    # Python バージョン
    import platform
    py_ver = platform.python_version()
    py_ok = tuple(int(x) for x in py_ver.split(".")[:2]) >= (3, 11)
    results.append(("Python >= 3.11", py_ok, f"Python {py_ver}"))

    # Codex CLI
    codex_bin = config.codex_bin
    codex_path = shutil.which(codex_bin)
    results.append(("Codex CLI", codex_path is not None, codex_path or "見つかりません"))

    # Codex バージョン
    if codex_path:
        import subprocess
        try:
            ver = subprocess.run(
                [codex_bin, "--version"],
                capture_output=True, text=True, timeout=10
            ).stdout.strip()
            results.append(("Codex バージョン", True, ver))
        except Exception as e:
            results.append(("Codex バージョン", False, str(e)))

    # codex exec
    if codex_path:
        try:
            r = subprocess.run(
                [codex_bin, "exec", "--help"],
                capture_output=True, text=True, timeout=10
            )
            results.append(("codex exec", r.returncode == 0, "利用可能"))
        except Exception:
            results.append(("codex exec", False, "実行失敗"))

    # codex exec resume
    if codex_path:
        try:
            r = subprocess.run(
                [codex_bin, "exec", "resume", "--help"],
                capture_output=True, text=True, timeout=10
            )
            results.append(("codex exec resume", r.returncode == 0, "利用可能"))
        except Exception:
            results.append(("codex exec resume", False, "実行失敗"))

    # config.toml
    config_toml = paths.codex_config_toml()
    results.append(("config.toml", config_toml.exists(), str(config_toml)))

    # Hook設定確認
    if config_toml.exists():
        content = config_toml.read_text()
        has_hook = "codex_autogoal" in content or "autogoal" in content.lower()
        results.append(("Hook設定", has_hook, "設定済み" if has_hook else "未設定"))

        hooks_disabled = "hooks = false" in content
        results.append(("hooks feature", not hooks_disabled,
                         "無効設定あり" if hooks_disabled else "有効(default含む)"))

    # Hookスクリプト実行権限
    stop_hook = Path(__file__).parent / "hooks" / "stop.py"
    results.append(("Stop Hook", stop_hook.exists(), str(stop_hook)))

    pre_tool_hook = Path(__file__).parent / "hooks" / "pre_tool_use.py"
    results.append(("PreToolUse Hook", pre_tool_hook.exists(), str(pre_tool_hook)))

    # 状態ディレクトリ
    state_root = paths.state_dir(config)
    state_writable = _check_writable(state_root)
    results.append(("状態ディレクトリ", state_writable, str(state_root)))

    # autogoal-job がPATH上にあるか
    job_bin = shutil.which("autogoal-job")
    results.append(("autogoal-job", job_bin is not None,
                     job_bin or "PATHに見つかりません"))

    # Gitリポジトリ（subdirectoryからの実行も許可）
    try:
        git_probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        git_root = git_probe.stdout.strip()
        git_ok = git_probe.returncode == 0 and bool(git_root)
    except (OSError, subprocess.SubprocessError):
        git_ok = False
        git_root = os.getcwd()
    results.append(("Gitリポジトリ", git_ok, git_root))

    # stale lock
    stale_locks = _find_stale_locks(config)
    results.append(("stale lock", len(stale_locks) == 0,
                     f"{len(stale_locks)}個" if stale_locks else "なし"))

    # 結果表示
    print("=== AutoGoal 環境診断 ===\n")
    all_ok = True
    for name, ok, detail in results:
        icon = "✓" if ok else "✗"
        print(f"  {icon} {name}: {detail}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("全チェック通過 ✓")
    else:
        print("一部のチェックに失敗しました。上記を確認してください。")


def _check_writable(path: Path) -> bool:
    """ディレクトリが書き込み可能か確認する"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
        return True
    except OSError:
        return False


def _find_stale_locks(config: Config) -> list[str]:
    """stale lockを検出する"""
    stale = []
    state_root = paths.state_dir(config)
    if not state_root.exists():
        return stale

    for d in state_root.iterdir():
        if not d.is_dir():
            continue
        lock_path = d / "watcher.lock"
        if is_lock_stale(lock_path):
            stale.append(str(lock_path))

    return stale


if __name__ == "__main__":
    main()
