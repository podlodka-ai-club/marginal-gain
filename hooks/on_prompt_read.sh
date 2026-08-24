#!/usr/bin/env bash
# Точка 2: сообщение человека. Чтение в горячем пути, поэтому жёсткий срок.
PAYLOAD=$(cat)
echo "$PAYLOAD" | timeout 10 python3 "$HOME/GolandProjects/memory-encoder/suggest.py" --hook 2>/dev/null
exit 0
