#!/usr/bin/env bash
# Корень репозитория ищется от самого хука, а не задаётся чужим путём.
# Раньше здесь стоял $HOME/GolandProjects/memory-encoder — каталога с таким
# именем нет, и оба хука молча звали пустоту, завершаясь с нулём.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.local/state/memory-encoder"
mkdir -p "$STATE_DIR"
# Точки входа зовутся модулями: python3 -m pipeline.drain. Корень в путь
# поиска кладём здесь, чтобы хук работал из любого рабочего каталога.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Живой контур пишет в SQLite, а не по сети. Сеть в горячем пути стоит
# задержки, ключа и квоты, а квота кончается ровно тогда, когда идёт замер.
# Пачка тоже упирается в потолок: хранилище за сетью берёт не больше сотни
# записей за вызов, а за ход их набегает вдвое больше, и отвергается пачка
# целиком. Ручные прогоны остаются на пути из .env — решение касается хода.
# Заданное снаружи сильнее умолчания: половину сравнения переключает окружение.
# Режим хода переключается файлом, а не правкой кода и не правкой .env:
#
#   echo sdk   > $STATE_DIR/backend   вернуть ход в хранилище за сетью
#   echo local > $STATE_DIR/backend   вернуть ход в локальную базу
#   rm           $STATE_DIR/backend   умолчание, то есть local
#
# Окружение сильнее файла — так переключается половина A/B-сравнения. Путь из
# .env на ход не действует вовсе: он про ручные прогоны, а ход не должен
# менять режим оттого, что кто-то поправил файл ради разовой отправки.
if [ -z "${XMEM_BACKEND:-}" ] && [ -f "$STATE_DIR/backend" ]; then
  XMEM_BACKEND="$(head -n 1 "$STATE_DIR/backend" | tr -d '[:space:]')"
fi
export XMEM_BACKEND="${XMEM_BACKEND:-local}"

# Хранилище названо в .env репозитория, а не в окружении пользователя: хук
# запускается из любого проекта и ничего оттуда не наследует. Пока этого не
# было, запись падала каждый ход на «не задан XMEM_INSTANCE_ID», и падала
# тихо — в журнал. Заданное снаружи сильнее файла: проверке нужна подмена.
if [ -f "$ROOT/.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"                       # файл мог прийти из Windows
    line="${line#"${line%%[![:space:]]*}"}"    # пробелы слева
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in export[[:space:]]*) line="${line#export}"
                                        line="${line#"${line%%[![:space:]]*}"}" ;; esac
    case "$line" in *=*) : ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    # Имя переменной, а не что попало: строку «не-ключ=1» bash встретил бы
    # руганью «not a valid identifier» в поток хука, то есть в разговор.
    case "$key" in [A-Za-z_]*) : ;; *) continue ;; esac
    case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in '"'*'"') value="${value#\"}" ; value="${value%\"}" ;; esac
    case "$value" in "'"*"'") value="${value#\'}" ; value="${value%\'}" ;; esac
    [ -n "${!key:-}" ] && continue
    export "$key=$value"
  done < "$ROOT/.env"
fi

# Ворота. Точки заняты у всего пользователя, значит хук зовут в каждом
# проекте и на каждом ходе. Работать он должен там, где это названо вслух:
#
#   $STATE_DIR/live-projects  каталоги, где память живая, по одному на строку
#   $STATE_DIR/off            выключатель: пока файл есть, молчат все хуки
#   XMEM_LIVE=1 / 0           перебивает и список, и выключатель
#
# Нет списка — молчим везде: установка, включающая себя сама, включается и
# там, где её не звали. Проверка стоит в bash до запуска питона: молчание
# ценой миллисекунд и молчание ценой интерпретатора на каждое сообщение это
# разные вещи.
live() {
  [ "${XMEM_LIVE:-}" = "0" ] && return 1
  [ "${XMEM_LIVE:-}" = "1" ] && return 0
  [ -e "$STATE_DIR/off" ] && return 1
  local here="${CLAUDE_PROJECT_DIR:-$PWD}"
  # Сравниваем настоящие пути, а не строки: на macOS /tmp это ссылка на
  # /private/tmp, и та же папка, названная двумя способами, читалась бы как
  # два разных места. Запертые ворота выглядят ровно как исправная работа.
  here="$(cd "$here" 2>/dev/null && pwd -P || printf '%s' "$here")"
  local list="$STATE_DIR/live-projects"
  [ -f "$list" ] || return 1
  local line real
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line/#\~/$HOME}"
    line="${line%/}"
    [ -z "$line" ] && continue
    real="$(cd "$line" 2>/dev/null && pwd -P || printf '%s' "$line")"
    case "$here" in "$real"|"$real"/*) return 0 ;; esac
  done < "$list"
  return 1
}
