#!/usr/bin/env bash
# Einrichten am Mac: eigenes venv, Pakete, Git-Hooks, Eintrag im Stellwerk. ffmpeg kommt vom System (Homebrew).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python="${PYTHON:-python3.13}"
command -v ffmpeg >/dev/null || { echo "ffmpeg fehlt (brew install ffmpeg)"; exit 1; }
[ -x .venv/bin/python ] || "$python" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[entwicklung]"
git config core.hooksPath tools/git-hooks
if [ -d "$HOME/Stellwerk/apps" ]; then
  sed "s|@ORDNER@|$(pwd)|" stellwerk.toml > "$HOME/Stellwerk/apps/propaganda-filter.toml"
  echo "Im Stellwerk eingetragen: $HOME/Stellwerk/apps/propaganda-filter.toml"
fi
