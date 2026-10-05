---
description: Replay your Claude Code and Codex history through nocap — how often did your agent claim "tests pass" with nothing behind it?
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/replay.py" --summary`

Show the report above to the user as is: the most recent catches (what the agent said, what was missing, where) and the totals line. Don't re-run it and don't summarize it away. If it found nothing, say so plainly.
