#!/usr/bin/env python3
"""Точка врезки просьбы: то, что подмешивается к запросу перед ходом модели.

Общий знаменатель всех агентов и всех вендоров — текст, который уезжает в
запрос. Через него и врезаемся: просьба берётся из схемы, печатается в
стандартный вывод, и дальше её подхватывает кто угодно — хук Claude Code,
чужой обвес, ручной прогон. Ни одной ветки по вендору здесь нет и быть не
должно: `if provider == ...` привязал бы механику к одному агенту, а формат
блока — обычный текст ровно потому, что tool-call и structured output
поддержаны у провайдеров по-разному.

Запуск: python3 -m pipeline.prompt
"""
import argparse

from domain import marks


def text(name=None):
    """Просьба одной строкой. Схему называет настройка, см. infra/config.py."""
    return marks.ask(name)


def main():
    ap = argparse.ArgumentParser(description="Просьба к модели: разметка фактов")
    ap.add_argument("--scheme", help="имя схемы, если не то, что в настройке")
    args = ap.parse_args()
    print(text(args.scheme))


if __name__ == "__main__":
    main()
