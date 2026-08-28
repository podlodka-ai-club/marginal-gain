#!/usr/bin/env bash
# Точка 5: конец хода агента.
# Разбираем ТОЛЬКО очередь и текущий разговор, а не весь архив: архив это
# десятки тысяч записей, разгребать его фоном значит жечь лимит впустую.
# Разбор архива при надобности запускается руками: python3 -m pipeline.save --send
#
# Зовём потребителя очереди, а не сохранение напрямую: иначе очередь, которую
# наполняет хук на сообщение человека, так и остаётся непрочитанной.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
live || { cat >/dev/null; exit 0; }
PAYLOAD=$(cat)
TRANSCRIPT=$(printf '%s' "$PAYLOAD" | python3 -c \
  "import json,sys; print((json.load(sys.stdin).get('transcript_path') or ''))" 2>/dev/null)
ARGS=(--send --limit 200)
[ -n "$TRANSCRIPT" ] && ARGS+=(--transcript "$TRANSCRIPT")
nohup python3 -m pipeline.drain "${ARGS[@]}" >> "$STATE_DIR/save.log" 2>&1 &
exit 0
