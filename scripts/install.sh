#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_ROOT="${CODEX_AUTOGOAL_INSTALL_ROOT:-$HOME/.local/share/codex-autogoal}"
VENV="$INSTALL_ROOT/venv"

choose_bin_dir() {
    local candidate
    IFS=':' read -r -a path_parts <<< "${PATH:-}"
    for candidate in "${path_parts[@]}"; do
        case "$candidate" in
            "$HOME"/*|/opt/homebrew/bin|/usr/local/bin)
                if [ -d "$candidate" ] && [ -w "$candidate" ]; then
                    printf '%s\n' "$candidate"
                    return
                fi
                ;;
        esac
    done
    printf '%s\n' "$HOME/.local/bin"
}

BIN_DIR="${CODEX_AUTOGOAL_BIN_DIR:-$(choose_bin_dir)}"
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

echo "=== Codex AutoGoal インストール ==="
echo "1. 専用venvを作成: $VENV"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install "$PROJECT_DIR" >/dev/null

# Uninstall refuses recursive deletion without this exact ownership marker.
printf '%s\n' 'codex-autogoal-managed-install-v1' > "$INSTALL_ROOT/.codex-autogoal-install-root"
chmod 600 "$INSTALL_ROOT/.codex-autogoal-install-root"

ln -sfn "$VENV/bin/autogoal" "$BIN_DIR/autogoal"
ln -sfn "$VENV/bin/autogoal-job" "$BIN_DIR/autogoal-job"
echo "2. コマンド配置: $BIN_DIR"

PATH="$BIN_DIR:$PATH" "$BIN_DIR/autogoal" install
PATH="$BIN_DIR:$PATH" "$BIN_DIR/autogoal" doctor

echo "=== インストール完了 ==="
echo "autogoal start --prompt-file task.md"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "注意: PATHへ追加が必要: export PATH=\"$BIN_DIR:\$PATH\""
fi
