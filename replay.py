"""Replay your own Claude Code and Codex history through nocap: how often did your
agent claim "tests pass" with nothing behind it?

    python3 replay.py                      # all of your Claude Code and Codex sessions
    python3 replay.py --summary            # totals + the 5 most recent catches
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


def default_paths():
    claude = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    codex = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
    return (glob.glob(os.path.join(claude, "projects", "*", "*.jsonl"))
            + glob.glob(os.path.join(codex, "sessions", "**", "*.jsonl"), recursive=True))


def judged_turns(path, lines):
    """One pass over the log; each turn is judged on the state it had when it ended,
    with the same judge() the hook uses."""
    ends = turn_ends(lines)
    marks = {end: None for end in ends}
    if lines and '"session_meta"' in lines[0]:
        events, final = nocap._codex_events(path, marks=marks)
    else:
        events, final = nocap._events(path, path[: -len(".jsonl")], 0, marks=marks)
    used, seen = [], False
    for line in lines:
        seen = seen or any(k in line for k in nocap.TOOL_MARKERS)
        used.append(seen)
    for end in ends:
        n, text = marks[end] if marks[end] is not None else (len(events), final)
        commands, edited = nocap.split(events[:n])
        text = "\n".join(text)
        yield end, nocap.judge(commands, edited, text, end > 0 and used[end - 1]), commands, edited, text


def said(commands, edited, text):
    """The sentence behind the first claim nocap blocked."""
    for claim, sentence in sorted(nocap.detect_claims_regex(text).items()):
        if nocap.missing_evidence(claim, commands, edited):
            return sentence
    return ""


def catches(paths):
    """Yields (turns_so_far, catch or None) per turn; a catch is (path, line, said, reason)."""
    turns = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for end, out, commands, edited, text in judged_turns(path, lines):
                turns += 1
                yield turns, (path, end, said(commands, edited, text), out["reason"]) if out else None
        except (OSError, ValueError) as e:  # one unreadable log shouldn't end the replay
            print("skipped {}: {}".format(path, e), file=sys.stderr)


def main(argv):
    summary = "--summary" in argv
    paths = [a for a in argv if a != "--summary"] or default_paths()
    turns, found = 0, []
    for turns, catch in catches(paths):
        if catch:
            found.append(catch)
            if not summary:
                print("🧢 {} (line {})\n   said: {}\n   {}\n".format(*catch))
    if summary:
        for path, line, said, reason in found[-5:]:
            print("🧢 \"{}\"\n   {}\n   {} (line {})\n".format(said[:140], reason.replace("nocap 🧢 — ", ""), path, line))
    rate = " (1 in {})".format(turns // len(found)) if found else ""
    print("nocap replay: {} turns, {} unverified claims{}.".format(turns, len(found), rate))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    main(sys.argv[1:])
