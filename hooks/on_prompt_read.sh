#!/usr/bin/env bash
# Точка 2: сообщение человека. Чтение в горячем пути, поэтому жёсткий срок.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
PAYLOAD=$(cat)
echo "$PAYLOAD" | timeout 10 python3 "$ROOT/suggest.py" --hook 2>/dev/null
exit 0
