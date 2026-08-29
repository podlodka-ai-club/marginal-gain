#!/usr/bin/env bash
# Точка 3: печать ответа. Харнесс отдаёт очередную порцию завершённых строк и
# показывает то, что вернул хук; запись разговора и контекст модели остаются
# исходными. Так служебный блок разметки не доезжает до человека, а разбор
# по-прежнему читает его из транскрипта.
#
# Точка горячая как никакая другая: она срабатывает не раз в ход, а много раз
# за один ответ, пока он печатается. Поэтому питон зовётся только тогда, когда
# в дельте есть маркер или открыт блок, — а это единицы дельт из сотни. Всё
# остальное решается здесь, без единого порождённого процесса.
#
# Молчание означает «печатай как было». Это и есть поведение при любой
# поломке: испорченный вывод видно в каждом разговоре, и лучше не тронуть
# ничего, чем показать человеку кусок json.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
live || { cat >/dev/null; exit 0; }

# Рубильник срезания, отдельный от общего: он про то, что видно на экране, а
# не про то, работает ли память. Порядок силы прежний — окружение сильнее
# файла, файл сильнее умолчания, умолчание «срезать».
HIDE="${XMEM_HIDE_MARKS:-}"
if [ -z "$HIDE" ] && [ -f "$STATE_DIR/hide-marks" ]; then
  HIDE="$(head -n 1 "$STATE_DIR/hide-marks" | tr -d '[:space:]')"
fi
# Те же слова «нет», что понимает infra/config.SHOW.
case "$HIDE" in 0|no|off|show|false) cat >/dev/null; exit 0 ;; esac

PAYLOAD=$(cat)

# Маркеры называет схема, и знать их здесь неоткуда, кроме как спросить питон.
# Спрашиваем один раз и кладём рядом с состоянием: файл переписывается, только
# если схема сменилась или marks.py стал новее.
SCHEME="${XMEM_MARKS:-}"
if [ -z "$SCHEME" ] && [ -f "$STATE_DIR/marks" ]; then
  SCHEME="$(head -n 1 "$STATE_DIR/marks" | tr -d '[:space:]')"
fi
SCHEME="${SCHEME:-xmd1}"
MARKERS="$STATE_DIR/markers-$SCHEME"
if [ ! -s "$MARKERS" ] || [ "$ROOT/domain/marks.py" -nt "$MARKERS" ]; then
  python3 -m pipeline.display --markers >"$MARKERS.tmp" 2>>"$STATE_DIR/display.log" \
    && mv "$MARKERS.tmp" "$MARKERS" || rm -f "$MARKERS.tmp"
fi
[ -s "$MARKERS" ] || exit 0          # маркеров нет — трогать вывод не станем
{ read -r BEGIN; read -r END; } <"$MARKERS"

# Открытый блок с прошлой дельты. Проверяем перебором имён, а не запуском ls:
# каталог пуст в подавляющем большинстве вызовов.
shopt -s nullglob
OPEN=("$STATE_DIR"/display/*)
shopt -u nullglob

if [ ${#OPEN[@]} -eq 0 ] && [ -n "$BEGIN" ] && [[ "$PAYLOAD" != *"$BEGIN"* ]] \
   && [[ "$PAYLOAD" != *"$END"* ]]; then
  exit 0                             # ни маркера, ни открытого блока
fi

printf '%s' "$PAYLOAD" | python3 -m pipeline.display 2>>"$STATE_DIR/display.log"
exit 0
