#!/usr/bin/env bash
# Корень репозитория ищется от самого хука, а не задаётся чужим путём.
# Раньше здесь стоял $HOME/GolandProjects/memory-encoder — каталога с таким
# именем нет, и оба хука молча звали пустоту, завершаясь с нулём.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.local/state/memory-encoder"
mkdir -p "$STATE_DIR"
