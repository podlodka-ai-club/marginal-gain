#!/usr/bin/env bash
# Точка 2: сообщение человека. Чтение в горячем пути, поэтому жёсткий срок.
#
# Срок держит сам модуль, а не timeout(1): на macOS такой команды нет, и
# конвейер молча падал со 127, а хук выходил нулём. Читающая половина не
# делала ничего и не жаловалась.
#
# Ошибки уводим в журнал, а не в /dev/null: хук обязан молчать в разговоре,
# но не обязан молчать вообще.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
live || { cat >/dev/null; exit 0; }
PAYLOAD=$(cat)
echo "$PAYLOAD" | python3 -m pipeline.suggest --hook 2>>"$STATE_DIR/suggest.log"
exit 0
