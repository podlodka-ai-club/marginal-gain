#!/usr/bin/env python3
"""Memory Encoder: fon reads Claude Code transcripts and writes episodes to xmemory."""
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

TRANSCRIPTS = Path.home() / ".claude" / "projects"
STATE = Path.home() / ".local" / "state" / "memory-encoder" / "state.json"
OUTBOX = Path.home() / ".local" / "state" / "memory-encoder" / "outbox.jsonl"
INSTANCE = os.environ.get("XMEM_INSTANCE_ID", "")

MAX_TEXT = 1200
MAX_TOOL = 500
BATCH_EVENTS = 40

SECRETS = [
    (re.compile(r"\bxmem_[A-Za-z0-9_\-]{6,}"), "<xmem-key>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"), "<gitlab-token>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "<github-token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), "<jwt>"),
    (re.compile(r"(?i)\b(authorization|bearer|private-token|api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*\S+"), r"\1: <redacted>"),
    (re.compile(r"(?i)machine\s+\S+\s+login\s+\S+\s+password\s+\S+"), "<netrc-entry>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<private-key>"),
    (re.compile(r"\bhvs\.[A-Za-z0-9_\-]{10,}"), "<vault-token>"),
    (re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----"), "<private-key-marker>"),
]

def redact(text):
    for pat, repl in SECRETS:
        text = pat.sub(repl, text)
    return text

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"files": {}}

def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)

def flatten_content(msg):
    """Turn an assistant/user message body into plain text plus tool names."""
    content = msg.get("content")
    tools, parts = [], []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(block.get("text", ""))
            elif kind == "thinking":
                continue
            elif kind == "tool_use":
                tools.append(block.get("name", "tool"))
                parts.append("tool_use %s %s" % (block.get("name"), json.dumps(block.get("input", {}), ensure_ascii=False)[:MAX_TOOL]))
            elif kind == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                parts.append("tool_result %s" % str(body)[:MAX_TOOL])
    return "\n".join(p for p in parts if p), tools

def has_tool_result(msg):
    """A tool result is a block type, not a phrase. Scanning the rendered text
    mislabels a human message that merely mentions the words, and misses a real
    result that follows a text block."""
    body = msg.get("content")
    if not isinstance(body, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in body)

def event_from_line(rec, seq):
    kind = rec.get("type")
    if kind not in ("user", "assistant"):
        return None
    msg = rec.get("message") or {}
    text, tools = flatten_content(msg)
    if not text.strip():
        return None
    ts = rec.get("timestamp") or ""
    dt = None
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            dt = None
    usage = msg.get("usage") or {}
    tokens = sum(int(usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    etype = "user_message" if kind == "user" else ("tool_call" if tools else "agent_response")
    if kind == "user" and has_tool_result(msg):
        etype = "tool_result"
    return {
        "session_id": rec.get("sessionId") or rec.get("session_id") or "",
        "sequence_number": seq,
        "event_type": etype,
        "content": redact(text)[:MAX_TEXT],
        "tool_name": tools[0] if tools else None,
        "occurred_at": dt.isoformat() if dt else None,
        "hour_of_day": dt.hour if dt else None,
        "day_of_week": dt.strftime("%A").lower() if dt else None,
        "working_directory": rec.get("cwd"),
        "git_branch": rec.get("gitBranch"),
        "project": Path(rec.get("cwd")).name if rec.get("cwd") else None,
        "tokens": tokens or None,
        "duration_seconds": round(rec["durationMs"] / 1000, 2) if isinstance(rec.get("durationMs"), int) else None,
    }

def read_new_events(path, cursor):
    """Read from the stored offset. Returns (events, new_offset, seq_start)."""
    st = path.stat()
    offset = cursor.get("offset", 0)
    if st.st_size < offset or cursor.get("inode") not in (None, st.st_ino):
        offset = 0
    seq = cursor.get("seq", 0)
    events = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        fh.seek(offset)
        for line in fh:
            if not line.endswith("\n"):
                break
            offset += len(line.encode("utf-8"))
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = event_from_line(rec, seq)
            if ev and ev["session_id"]:
                events.append(ev)
                seq += 1
    return events, {"offset": offset, "inode": st.st_ino, "seq": seq}

def render_batch(events):
    """One write per batch: plain text that the xmemory write path extracts from."""
    first = events[0]
    head = "Session %s in project %s (%s), branch %s." % (
        first["session_id"], first.get("project") or "unknown",
        first.get("working_directory") or "unknown path", first.get("git_branch") or "none")
    if first.get("occurred_at"):
        head += " Started at %s, %s, hour %s." % (first["occurred_at"], first.get("day_of_week"), first.get("hour_of_day"))
    lines = [head, "Events:"]
    for ev in events:
        lines.append("#%d %s at %s%s: %s" % (
            ev["sequence_number"], ev["event_type"], ev.get("occurred_at") or "unknown time",
            (" via %s" % ev["tool_name"]) if ev.get("tool_name") else "",
            ev["content"].replace("\n", " ")[:MAX_TEXT]))
        if ev.get("tokens"):
            lines[-1] += " [tokens %d]" % ev["tokens"]
    return "\n".join(lines)

def send(text, dry_run):
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "sent": not dry_run, "text": text}, ensure_ascii=False) + "\n")
    if dry_run:
        return "dry-run"
    env = dict(os.environ, XMEM_INSTANCE_ID=INSTANCE)
    proc = subprocess.run(["xmemcli", "write", text, "--no-wait"], capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("xmemcli write failed: %s" % (proc.stdout or proc.stderr)[:300])
    return proc.stdout.strip()[:200]

def run_once(args):
    state = load_state()
    files = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    if args.only:
        files = [f for f in files if args.only in str(f)]
    if args.reset:
        for f in files:
            state["files"].pop(str(f), None)
    total_events = total_batches = 0
    for path in files:
        key = str(path)
        cursor = state["files"].get(key, {})
        events, new_cursor = read_new_events(path, cursor)
        if not events:
            state["files"][key] = new_cursor or cursor
            continue
        stopped = False
        for i in range(0, len(events), BATCH_EVENTS):
            if args.max_batches and total_batches >= args.max_batches:
                stopped = True
                break
            batch = events[i:i + BATCH_EVENTS]
            text = render_batch(batch)
            result = send(text, args.dry_run)
            total_batches += 1
            if args.verbose:
                print("%s: %d events -> %s" % (path.name, len(batch), result))
        # Cursor moves only when the whole file went out. Otherwise the events
        # we never sent would be marked as handled and lost for good. The price
        # is re-sending the batches already sent from this file on the next run,
        # the same trade save.py makes: a duplicate beats a silent loss.
        if stopped:
            save_state(state)
            break
        total_events += len(events)
        state["files"][key] = new_cursor
        save_state(state)
    print("files %d, new events %d, batches %d, mode %s" % (len(files), total_events, total_batches, "dry-run" if args.dry_run else "send"))

def main():
    ap = argparse.ArgumentParser(description="Memory Encoder: transcripts to xmemory")
    ap.add_argument("--send", dest="dry_run", action="store_false", default=True, help="actually write to xmemory (default is dry-run)")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="loop with this interval instead of a single pass")
    ap.add_argument("--only", metavar="SUBSTRING", help="process only transcripts whose path contains this")
    ap.add_argument("--reset", action="store_true", help="forget cursors for the selected files and reread them")
    ap.add_argument("--max-batches", type=int, help="stop after this many batches")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.watch:
        while True:
            run_once(args)
            time.sleep(args.watch)
    else:
        run_once(args)

if __name__ == "__main__":
    main()
