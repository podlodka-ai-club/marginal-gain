#!/usr/bin/env bash
# Точка 5: конец хода агента.
# Сохраняем ТОЛЬКО текущий разговор, а не весь архив: архив это 21 тысяча
# записей, разгребать его фоном значит жечь лимит впустую.
# Разбор архива при надобности запускается руками: python3 save.py --send
DIR="$HOME/.local/state/memory-encoder"
mkdir -p "$DIR"
PAYLOAD=$(cat)
TRANSCRIPT=$(printf '%s' "$PAYLOAD" | python3 -c \
  "import json,sys; print((json.load(sys.stdin).get('transcript_path') or ''))" 2>/dev/null)
[ -z "$TRANSCRIPT" ] && exit 0
nohup flock -n "$DIR/save.lock" \
  python3 "$HOME/GolandProjects/memory-encoder/save.py" --send --only "$TRANSCRIPT" --limit 200 \
  >> "$DIR/save.log" 2>&1 &
exit 0
