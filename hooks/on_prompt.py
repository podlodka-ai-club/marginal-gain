#!/usr/bin/env python3
"""UserPromptSubmit hook: put the message plus its context into a local queue. No network, always exit 0."""
import json, sys, os
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from pipeline.drain import QUEUE          # адрес очереди задаёт её потребитель
except Exception:
    # Тот же адрес и та же переменная, что у потребителя. Иначе при сбое
    # импорта производитель и потребитель разошлись бы по разным файлам,
    # и молча: хук глотает любое исключение и выходит с нулём.
    QUEUE = Path(os.environ.get("XMEM_QUEUE_PATH")
                 or Path.home() / ".local" / "state" / "memory-encoder" / "queue.jsonl")

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from infra.scrub import redact
    except Exception:
        redact = lambda s: s
    now = datetime.now(timezone.utc)
    cwd = payload.get("cwd") or os.getcwd()
    item = {
        "kind": "user_message",
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "content": redact(payload.get("prompt") or ""),
        "occurred_at": now.isoformat(),
        "hour_of_day": now.hour,
        "day_of_week": now.strftime("%A").lower(),
        "working_directory": cwd,
        "project": Path(cwd).name,
        "git_branch": git_branch(cwd),
        "permission_mode": payload.get("permission_mode"),
    }
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")

def git_branch(cwd):
    head = Path(cwd) / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
    except OSError:
        return None
    return ref.split("/", 2)[-1] if ref.startswith("ref:") else ref[:8]

if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
