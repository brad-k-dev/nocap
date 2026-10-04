"""Replay your own Claude Code history through nocap: how often did your agent claim
"tests pass" with nothing behind it?

    python3 replay.py                      # all of ~/.claude/projects and ~/.codex/sessions
    python3 replay.py path/to/session.jsonl
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
import nocap  # noqa: E402


def turn_ends(lines):
    """Indexes where a new real user prompt starts, i.e. where the previous turn stopped."""
    ends = []
    for i, line in enumerate(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if (d.get("payload") or {}).get("type") == "task_complete":  # Codex: the turn ended here
            ends.append(i + 1)
        elif d.get("type") == "user" and isinstance((d.get("message") or {}).get("content"), str) and not d.get("isMeta"):
            ends.append(i)
    if lines and '"session_meta"' in lines[0]:
        return ends
    return ends[1:] + [len(lines)]


def main(paths):
    turns = blocked = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        for end in turn_ends(lines):
            out = nocap.verdict({"transcript_path": path}, upto=end)
            turns += 1
            if out:
                blocked += 1
                print("🧢 {} (line {})\n   {}\n".format(path, end, out["reason"]))
    print("{} turns replayed, {} would have been blocked.".format(turns, blocked))


if __name__ == "__main__":
    main(sys.argv[1:] or glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
         + glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True))
