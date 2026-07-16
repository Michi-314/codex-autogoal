#!/usr/bin/env bash
set -euo pipefail

# AutoGoal アンインストールスクリプト
# 使い方: bash scripts/uninstall.sh

echo "=== Codex AutoGoal アンインストール ==="
echo ""

INSTALL_ROOT="${CODEX_AUTOGOAL_INSTALL_ROOT:-$HOME/.local/share/codex-autogoal}"
INSTALL_ROOT_CANONICAL=$(python3 - "$INSTALL_ROOT" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
)
HOME_CANONICAL=$(python3 - <<'PY'
from pathlib import Path
print(Path.home().resolve())
PY
)
case "$INSTALL_ROOT_CANONICAL" in
    /|"$HOME_CANONICAL"|"$HOME_CANONICAL/.local"|"$HOME_CANONICAL/.local/share")
        echo "安全のため削除を拒否しました: $INSTALL_ROOT_CANONICAL" >&2
        exit 1
        ;;
esac
SENTINEL="$INSTALL_ROOT_CANONICAL/.codex-autogoal-install-root"
if [ ! -f "$SENTINEL" ] || [ -L "$SENTINEL" ] || \
   [ "$(cat "$SENTINEL")" != "codex-autogoal-managed-install-v1" ]; then
    echo "管理対象sentinelを確認できないため削除を拒否しました: $INSTALL_ROOT_CANONICAL" >&2
    exit 1
fi
INSTALL_ROOT="$INSTALL_ROOT_CANONICAL"
for BIN_DIR in "${CODEX_AUTOGOAL_BIN_DIR:-}" "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin; do
    [ -n "$BIN_DIR" ] || continue
    for NAME in autogoal autogoal-job; do
        LINK="$BIN_DIR/$NAME"
        if [ -L "$LINK" ]; then
            TARGET=$(readlink "$LINK")
            case "$TARGET" in
                "$INSTALL_ROOT"/*) rm -f "$LINK" ;;
            esac
        fi
    done
done

CONFIG="$HOME/.codex/config.toml"

# config.toml からHook設定を削除
if [ -f "$CONFIG" ]; then
    echo "1. config.toml からAutoGoal Hook設定を削除..."
    # バックアップ作成
    TIMESTAMP=$(date +%Y%m%dT%H%M%S)
    cp "$CONFIG" "${CONFIG}.autogoal-uninstall-backup-${TIMESTAMP}"
    echo "   バックアップ: ${CONFIG}.autogoal-uninstall-backup-${TIMESTAMP}"

    # AutoGoal marker範囲だけを削除（macOS/Linux共通）
    python3 - "$CONFIG" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
start = "# --- AutoGoal Hook設定 ---"
end = "# --- End AutoGoal Hook設定 ---"
lines = path.read_text().splitlines(keepends=True)
output = []
inside = False
for line in lines:
    if line.strip() == start:
        inside = True
        continue
    if inside and line.strip() == end:
        inside = False
        continue
    if not inside:
        output.append(line)
path.write_text("".join(output))
PY
    echo "   ✓ Hook設定を削除しました"
else
    echo "1. config.toml が見つかりません。スキップ。"
fi

echo ""

# 専用venvを削除
echo "2. 専用venvを削除..."
python3 - "$INSTALL_ROOT" <<'PY'
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1])
sentinel = root / ".codex-autogoal-install-root"
if root.is_symlink() or sentinel.is_symlink() or not sentinel.is_file():
    raise SystemExit("refusing unverified install root")
if sentinel.read_text().strip() != "codex-autogoal-managed-install-v1":
    raise SystemExit("refusing invalid install sentinel")
shutil.rmtree(root)
PY
echo "   ✓ パッケージアンインストール完了"
echo ""

echo "=== アンインストール完了 ==="
echo ""
echo "注意: ~/.codex/autogoal/ の状態ファイルは残してあります。"
echo "完全に削除する場合: rm -rf ~/.codex/autogoal/"
