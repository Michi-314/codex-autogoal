# Codex AutoGoal Supervisor

> [!IMPORTANT]
> Experimental, unofficial community project. This project is not affiliated with or
> endorsed by OpenAI. AutoGoal can repeatedly invoke Codex CLI and run detached commands;
> review its hooks, sandbox, prompts, and expected API usage before enabling it.

> [!CAUTION]
> v0.1.0 through v0.1.2 have unsafe legacy-migration paths. Upgrade to v0.1.3, which
> quarantines control homes containing symlinks, special nodes, or hard-linked files. AutoGoal
> remains an alpha for trusted repositories: same-user Codex can still read control logs.

AutoGoal is a small, dependency-free supervisor for long-running Codex CLI tasks. It keeps
ordinary work moving through a Stop hook, moves long commands into detached OS processes,
and resumes the same Codex session once a job finishes. No model process remains alive while
AutoGoal is only waiting for that job.

The implementation and detailed documentation are currently Japanese. Issues and pull
requests in either Japanese or English are welcome.

Codex CLIの長時間タスクを、待機中のLLMポーリングなしで継続する外部スーパーバイザー。
通常作業はCodexのStop Hookで継続し、長時間コマンドは独立プロセスへ移す。完了後だけ
watcherが同じCodexセッションを一度resumeする。

`/goal`はCodex内の目標継続機能で、AutoGoalはOSプロセスと完了ファイルを使う外部監督層。
長時間処理中にCodexプロセスを残さない点が異なる。

## アーキテクチャ

```text
autogoal start -> codex exec -> Stop Hook
                                | continue -> continuation turn
                                | wait -> detached watcher -> process done
                                |                           -> codex exec resume
                                ` done/blocked -> stop

autogoal-job -> detached job runner -> target process
                                  `-> status.json -> done (last, atomic)
```

待機中に動くのはjob runnerと5秒間隔で`done`ファイルを見るwatcherだけ。モデルAPIや
Codex CLIは起動しないため、待機時間そのものによるトークン消費は発生しない。

## 要件と互換性

- macOSまたはLinux
- Python 3.11以上
- Gitリポジトリ
- Codex CLI 0.144系（実運用検証: 0.144.1、公開前ローカル確認: 0.144.3）

0.144系ではhooksは既定で有効。設定は`hooks.Stop`と`hooks.PreToolUse`のinline TOML、
Stop継続は`{"decision":"block"}`、停止は`{"continue":true}`、PreToolUse拒否は
`hookSpecificOutput.permissionDecision="deny"`を使う。CLIの実装と公式Hooks仕様が異なる
場合はインストール済みCLIを優先する。

## インストール

```bash
git clone https://github.com/Michi-314/codex-autogoal.git
cd codex-autogoal
bash scripts/install.sh
```

インストーラはPEP 668環境を壊さず専用venvへ入れ、書き込み可能なPATHディレクトリへ
コマンドをsymlinkする。開発用はvenv内で`python3 -m pip install -e .`でもよい。`autogoal install`は既存
`~/.codex/config.toml`をtimestamp付きでバックアップし、マーカーで囲んだHook設定だけを
追記する。既存の`[features]`や認証設定は変更しない。初回利用時はCodexの`/hooks`で
追加されたcommand hooksを確認してtrustする必要がある。

アンインストール:

```bash
bash scripts/uninstall.sh
```

状態データ`~/.codex/autogoal/`は診断用に残る。状態ディレクトリは`0700`、状態・ログ
ファイルは`0600`で保存される。起動前の再帰検査でsymlinkが1つでも見つかった旧control
homeは、同じ親ディレクトリの`autogoal.quarantine-<timestamp>-<random>`へ丸ごと移動し、
新しい空のcontrol homeを作る。隔離データは内容を確認してから手動で削除する。
regular fileのhard link数も検査し、`st_nlink != 1`なら同様に隔離する。

## 基本操作

```bash
autogoal start --cwd ~/src/project --prompt-file task.md
autogoal start --prompt "テストが通るまで修正と検証を続けること"
autogoal start --model MODEL --sandbox workspace-write --prompt-file task.md
autogoal start --bypass-hook-trust --prompt-file task.md

autogoal status SESSION_ID
autogoal list
autogoal logs SESSION_ID
autogoal logs SESSION_ID --events
autogoal cancel SESSION_ID
autogoal cancel SESSION_ID --kill-jobs
autogoal recover
```

長時間ジョブ:

```bash
autogoal-job start --name backtest -- python3 scripts/run_backtest.py --days 30
autogoal-job start --inherit-env --name trusted-job -- command-requiring-secrets
autogoal-job timer --name cooldown --after 30m
autogoal-job timer --at 2026-07-10T18:00:00+09:00
autogoal-job status JOB_ID --json
autogoal-job logs JOB_ID --tail 100
autogoal-job logs JOB_ID --stderr
autogoal-job cancel JOB_ID --kill
```

`autogoal-job start/timer`は信頼済みの通常ターミナルからのみ実行する。Codex sandbox内
からの起動は、control stateをモデル書込み可能にしないため拒否される。接続済みジョブの
完了後はheadlessな`codex exec resume`だけを使用し、端末への文字列やEnter送信は行わない。

detached jobはCodex sandbox外で通常ユーザー権限により実行される。既定では`PATH`、
`HOME`、localeなどのallowlist環境だけを継承し、tokenやcloud credentialなどの任意環境変数
は渡さない。全面継承が必要な監査済みコマンドだけ`--inherit-env`を明示する。この指定では
通常ターミナルの秘密情報、filesystem権限、network権限を対象コマンドが利用できる。

durationは`30s`、`10m`、`2h`、`1d`に対応。通常ジョブは予測時刻ではなく対象プロセスの
実終了を完了条件にする。終了コードが非0でもCodexはresumeされ、失敗ログを調査できる。
ジョブのstdoutとstderrは合計100 MiBが既定上限で、超過時はジョブを停止する。
`CODEX_AUTOGOAL_MAX_JOB_LOG_BYTES`で変更できる。

## AutoGoalシグナル

最終メッセージの最後の非空行に1個だけ置く。

```text
AUTOGOAL_SIGNAL: {"state":"continue","reason":"残りのテストを実行する"}
AUTOGOAL_SIGNAL: {"state":"wait","job_id":"...","reason":"バックテスト完了待ち"}
AUTOGOAL_SIGNAL: {"state":"done","reason":"実装と検証が完了した"}
AUTOGOAL_SIGNAL: {"state":"blocked","reason":"ユーザー入力が必要"}
```

欠落、壊れたJSON、未知state、不正job IDは安全側で`BLOCKED_PROTOCOL_ERROR`になる。
同じ出力の反復、同じreasonの反復、既定50 continuationも停止条件。

## 状態、復旧、再起動

状態は`~/.codex/autogoal/state/<session-id>/`、ジョブは
`~/.codex/autogoal/jobs/<job-id>/`へatomicに保存する。`done`は結果ファイルをすべて
書いた後、最後に作成される。

マシン再起動やwatcher異常終了後は次を実行する。

```bash
autogoal recover
```

自動化する場合、macOSはログイン時のLaunchAgent、Linuxは`systemd --user`のoneshot unitで
`autogoal recover`を起動する。初期版はOS設定を自動作成しない。

## セキュリティ

- 既定sandboxは`workspace-write`。`danger-full-access`を自動選択しない。
- `autogoal start`は`--add-dir`を使用せず、Codexにcontrol stateへの書込みを許可しない。
- control home内にsymlink、特殊ノード、複数hard linkがあればhome全体を隔離する。
- `shell=True`を使わずargv配列で起動する。
- job IDを検証し、jobs root外やsymlink脱出を拒否する。
- session IDも全CLI、Hook、watcher境界で検証する。
- 認証情報や環境変数全体をログへ保存しない。
- PreToolUseは明白な長時間pollを止める補助ガードで、完全なsecurity boundaryではない。
- `--kill-jobs`なしのsession cancelはジョブを殺さない。
- `--bypass-hook-trust`は全configured hooksのtrust確認をその実行だけ迂回する。内容を監査済みの自動化でのみ使う。
- terminal keystrokeによるvisible resumeは無効。headless resumeだけを使用する。
- protocolはパッケージ内のread-only定数で、runtime homeから読み込まない。
- Codex、watcher、detached jobの子環境はallowlistが既定。jobの全面継承は`--inherit-env`のみ。
- resumeメッセージへcontrol homeの絶対パスや生のjob logを含めない。

### 機密性の制約

`0600`は別OSユーザーからの読み取りを防ぐが、CodexはAutoGoalと同じOSユーザーで動く。
現行の対応Codex CLIにはAutoGoalから特定パスだけを確実にread-denyする公開オプションが
ないため、`state/`と`jobs/`の生ログを同一ユーザーのCodexが読める可能性は残る。未信頼の
README、Web内容、issue、ログを扱う用途では使用せず、必要なら専用OSユーザーまたは外部
コンテナで分離する。ログには秘密情報を出力せず、不要になったcontrol dataは削除する。

## Doctorとトラブルシューティング

```bash
autogoal doctor
codex doctor --summary
```

- Hookが動かない: Codex TUIの`/hooks`で定義をtrustし、`features.hooks=false`がないか確認。
- jobが進まない: `autogoal-job status`とstdout/stderr、PIDを確認。
- resumeされない: `watcher.log`、`resume.log`、`autogoal recover`を確認。
- cwd消失: 安全停止して`BLOCKED_CWD_MISSING`になる。
- ターン開始後にresumeが失敗: 重複実行を避け`BLOCKED_RESUME_AMBIGUOUS`になる。

## 検証

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m build
```

fake Codex統合試験はcontinue、wait/resume、失敗ジョブ、重複Hook、cancel、malformed signal、
通常セッション非干渉を扱う。実Codex smokeは課金・トークンを使うため、全ローカル試験通過後に
明示的な最小プロンプトで一度だけ行い、結果とusageを記録する。

## 既知の制約

- Windowsは未対応。
- Hook trustの初回確認はユーザー操作が必要。
- PreToolUseはCodexの全shell経路を捕捉する保証がない。
- OS起動時recoverの設定生成は未実装。
- resume開始後の通信断は自動再試行せず、安全側で停止する。

## 開発・公開情報

- 開発参加: [CONTRIBUTING.md](CONTRIBUTING.md)
- セキュリティ報告: [SECURITY.md](SECURITY.md)
- 変更履歴: [CHANGELOG.md](CHANGELOG.md)
- リリース手順: [docs/RELEASING.md](docs/RELEASING.md)
- ライセンス: [MIT](LICENSE)
