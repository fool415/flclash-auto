#!/usr/bin/env bash
# Git Bash 快捷入口: px <命令...>   例如  px curl -sI https://www.google.com
_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python >/dev/null 2>&1; then
  exec python -u "$_DIR/flclash_proxy.py" "$@"
elif command -v py >/dev/null 2>&1; then
  exec py -3 -u "$_DIR/flclash_proxy.py" "$@"
else
  echo "!! 未找到 Python(3),请先安装:https://www.python.org/downloads/" >&2
  exit 127
fi
