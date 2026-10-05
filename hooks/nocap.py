#!/usr/bin/env python3
"""nocap: Claude Code Stop hook. Blocks "tests pass" / "build ok" / "it's fixed"
claims that have no successful command behind them since the last file edit.

stdlib only (python 3.9+). Experimental Laya claim detection with NOCAP_LAYA=1.
"""
import json
import os
import re
import shlex
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# editing docs/notes after a test run doesn't make "tests pass" stale
NON_CODE = re.compile(
    r"\.(md|mdx|txt|rst|png|jpe?g|gif|svg|webp|ico)$|/\.claude/|/memory/|(^|/)LICENSE"
    r"|(^|/)\.(gitignore|gitattributes|editorconfig|python-version|nvmrc|node-version|tool-versions)$"
)
CODE_FILE = re.compile(r"\.(py|ts|tsx|js|jsx|mjs|cjs|swift|kt|go|rs|java|c|cc|cpp|h|rb|php|cs|vue|svelte)$")
# ponytail: edits done through Bash are found by their write targets (redirects, sed -i,
# open(...,'w') in interpreter heredocs); exotic writers slip through
TEMP_DIR = re.compile(r"/(private/)?(tmp|var/folders)/|/dev/")
REDIRECT = re.compile(r"(?:>>?|\btee(?: -a)?)\s*['\"]?([^\s'\";&|)<>]+)")
SED_INPLACE = re.compile(r"\b(?:sed -i|perl -pi)\b([^\n|;&]*)")
SCRIPT_WRITE = re.compile(r"open\([^)]*['\"][wa]['\"]|\.write_text\(|\.writeFileSync\(")
PATH_LITERAL = re.compile(r"(?:\b\w+\s*=\s*|open\(\s*|Path\(\s*)['\"]([^'\"\n]+)['\"]")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.S | re.M)
INTERPRETER_HEREDOC = re.compile(r"\b(python3?|node|ruby|perl|bash|sh)\b[^\n]*<<")
READ_ONLY = re.compile(r"^\s*(cd [^;&]+(&&|;)\s*)?(git|gh|ls|cat|grep|rg|find|echo|head|tail|wc|pwd|which|sleep)\b")

# A tool only counts in command position: `cd x && pytest` runs tests, `rg "pytest" README` doesn't.
_ARGS = r"[^\n]*"  # rest of the segment
TEST_CMD = re.compile(
    r"(pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest"
    r"|go test|cargo (test|nextest)|swift test|dotnet test|mix test|deno test|bun test"
    r"|(npm|pnpm|yarn|bun)( run)? test|(npm|pnpm|yarn|bun) run (check|verify|ci|validate)\b|node( --[\w-]+)* --test"
    r"|(make|xcodebuild|mvn|(\./)?gradlew?)" + _ARGS + r"\b(test|verify)"
    r"|\S*test_\w+\.py|\S*_test\.py)\b"
)
BUILD_CMD = re.compile(
    r"(tsc|cargo (build|check)|go (build|vet)|swift build|xcodebuild|cmake --build"
    r"|(npm|pnpm|yarn|bun)( run)? (build|typecheck)|(npm|pnpm|yarn|bun) run (check|verify|ci|validate)\b|make|vite build|next build|webpack|dotnet build"
    r"|(mvn|(\./)?gradlew?)" + _ARGS + r"\b(package|compile|install|build|assemble)"
    r"|mypy|pyright|javac|gcc|clang|rustc|swiftc|py_compile|compileall)\b"
)
REMOTE_CHECK = re.compile(r"gh (run (watch|view)|pr checks)|gh api \S*(pages/builds|actions/runs|check-runs|status)")
_SEGMENT = re.compile(r"&&|\|\||[;|\n(`]|\$\(")
_QUOTED_ARG = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
_LAUNCHER = re.compile(
    r"^(?:\w+=\S*\s+)*(?:(?:time|env|sudo|exec|timeout\s+\S+|npx(?:\s+(?:--yes|-y))?|bunx|pnpm\s+(?:exec|dlx)"
    r"|yarn\s+dlx|uv\s+run|poetry\s+run|pipenv\s+run|bundle\s+exec|python3?(?:\s+-m)?|xcrun"
    r"|\.?/?node_modules/\.bin/|\.?/?\.?venv/bin/)\s*)*"
)


_INFO_ONLY = re.compile(r"\s(--?version|--help|-list|-showdestinations|-showsdks|-showBuildSettings)\b")


def runs(pattern, command):
    """Does any command segment start with a tool `pattern` matches? (`xcodebuild -list` only asks.)"""
    for seg in _SEGMENT.split(_QUOTED_ARG.sub("_", command)):
        seg = _LAUNCHER.sub("", seg.strip())
        if pattern.match(seg) and not _INFO_ONLY.search(seg):
            return True
    return False
PASS_OUTPUT = re.compile(
    r"\b\d+ (passed|passing)\b|\*\* (BUILD|TEST) SUCCEEDED \*\*|with 0 failures|\bBUILD SUCCESSFUL\b"
    r"|^(# |ℹ )fail 0\b",
    re.M,
)
# output that shows tests or a build ran, whatever the script was called (`npm run check`)
TEST_OUTPUT = re.compile(
    r"\b\d+ (passed|failed|passing|failing)\b|Tests?:\s+\d+|Test Suites:|\bRan \d+ tests?\b"
    r"|Executed \d+ tests?|test result: |Test run with \d+ tests|^(# |ℹ )(tests|pass|fail) \d+"
)
BUILD_OUTPUT = re.compile(
    r"\*\* BUILD (SUCCEEDED|FAILED) \*\*|✓ built in|[Cc]ompiled successfully|Build complete|BUILD SUCCESSFUL"
    r"|Finished `?(dev|release|test)`? profile"
)
# A build can pass inside a command whose tests failed: `xcodebuild test` reaching TEST FAILED compiled fine.
BUILD_REACHED_TESTS = re.compile(r"\*\* TEST (SUCCEEDED|FAILED) \*\*")
BUILD_FAIL = re.compile(r"\*\* BUILD FAILED \*\*|BUILD FAILED|\berror TS\d+|(^|: )error(\[\w+\])?: ", re.M)
BUILD_PASS = re.compile(
    r"\*\* BUILD SUCCEEDED \*\*|✓ built in|[Cc]ompiled successfully|Build complete|BUILD SUCCESSFUL|Finished `?(dev|release|test)`? profile"
)
# The tool never ran (no simulator, no such command): evidence neither way.
ENV_ERROR = re.compile(
    r"command not found|is not recognized as an internal or external command|Unable to find a device"
    r"|xcodebuild: error: Unable to|npm (ERR!|error) Missing script"
)
# exit code 0 can lie (`pytest | tail`), so also read the output
FAIL_OUTPUT = re.compile(
    r"\b\d+ (failed|failing|errors?)\b|\bFAILED\b|\bFAIL\b|Tests?:\s+\d+ failed"
    r"|(^|: )error(\[\w+\])?: |^(# |ℹ )fail [1-9]|BUILD FAILED|\*\* (BUILD|TEST) FAILED \*\*",
    re.M,
)

_KO_ASSERT = r"(했|합|됐|됩|되었|입니|이에|해요|함|됨|중|\s*(확인|완료)|\s*[.!|)]|\s*$)"
# "테스트 통과, 빌드 성공" is a list of words; "테스트 40개 통과, ..." is a report
_KO_COUNT = r"(\d+ ?개?[^.\n]{0,8}테스트|테스트[^.\n]{0,8}\d+ ?개?)[^.\n]{0,10}(통과|성공)"
CLAIMS = {
    "tests_pass": re.compile(
        r"\b(all |the )?tests? (now |all )?(pass|passed|passing|are passing|are green|succeed)"
        r"|\b\d+ (tests? )?(passed|passing)\b|test suite (passes|is green)|\ball green\b"
        r"|테스트.{0,15}(통과|성공|패스)" + _KO_ASSERT + "|" + _KO_COUNT,
        re.I,
    ),
    "build_ok": re.compile(
        r"\bbuild (now )?(succeeds|succeeded|passes|passed|is green|works)"
        r"|\b(builds|compiles) (cleanly|successfully|fine|without errors)"
        r"|(빌드|컴파일).{0,4}(성공|통과|완료)" + _KO_ASSERT,
        re.I,
    ),
    "works": re.compile(
        r"\b(is|are|now) (fixed|working)\b|\bfixed the (bug|issue|error|crash)"
        r"|\b(it|everything) (now )?works\b|\bworks (now|as expected|correctly)"
        r"|(해결|고쳐)(했|됐|되었|졌)|(정상|잘) ?(동작|작동)" + _KO_ASSERT,
        re.I,
    ),
}
# ponytail: sentence-level negation heuristic; Laya backend handles real phrasing
NEGATION = re.compile(
    r"\b(not|n't|never|unverified|untested|haven't|couldn't|unable|should|may|might)\b"
    r"|않|못|안 |아직|확인 ?필요|어야|아야|여부|해도|하면|하는데|하던|한다는",
    re.I,
)

# claims nocap can't check against this session: manual QA, other PRs, earlier work
NOT_THIS_SESSION = re.compile(
    r"항목|체크리스트|checklist|manual|수동|\bQA\b|PR ?#\d+|머지 전|이전에|earlier|previously|before (the )?merg",
    re.I,
)

# "23 unit tests pass; the E2E suite failed on the network" reports the failure it ran into
ADMITS_FAILURE = re.compile(r"(test|테스트)[^.\n]{0,40}(fail|실패)|(fail|실패)[^.\n]{0,20}(test|테스트)", re.I)

LAYA_QUESTIONS = {
    "tests_pass": "Does the message state that tests were run and pass?",
    "build_ok": "Does the message state that the build or compilation succeeds?",
    "works": "Does the message state that a bug is fixed or the code now works?",
}

REASONS = {
    "tests_pass": "you said tests pass, but {why}. Run the tests and show the result, or retract the claim.",
    "build_ok": "you said the build succeeds, but {why}. Run the build and show the result, or retract the claim.",
    "works": "you said it's fixed/working, but {why}. Run a test or build that proves it, or say it is unverified.",
}


QUOTED = re.compile(r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|(?<!\w)'[^'\n]*'(?!\w)", re.S)


def detect_claims_regex(text):
    # quoting a claim isn't making it. Nested quotes defeat QUOTED's pairing, so first
    # blank out any claim that starts right after a quote mark.
    for pat in CLAIMS.values():
        text = pat.sub(lambda m: " " * len(m.group(0)) if m.start() and text[m.start() - 1] in "\"'`“‘" else m.group(0), text)
    text = QUOTED.sub(" ", text)
    found = {}  # claim -> the sentence that made it
    for sentence in re.split(r"(?<=[.!?。])\s+|\n+", text):
        if NEGATION.search(sentence) or NOT_THIS_SESSION.search(sentence):
            continue
        for name, pat in CLAIMS.items():
            if pat.search(sentence):
                found.setdefault(name, sentence.strip())
    return found


def detect_claims(text):
    if os.environ.get("NOCAP_LAYA") != "1":
        return detect_claims_regex(text)
    # experimental: zero-shot Laya finds claims but often mislabels their type
    from laya import Router  # needs python 3.10+
    # ponytail: model loads on every Stop (~seconds); switch to laya-serve if latency hurts
    qs = {k: {"type": "noul", "instructions": v} for k, v in LAYA_QUESTIONS.items()}
    answers = Router().predict(text, qs)["answers"]
    return {k for k in qs if answers[k]["noul"] >= 0.7}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


TASK_NOTE = re.compile(
    r"<tool-use-id>(\w+)</tool-use-id>.*?<output-file>(.*?)</output-file>.*?<status>(\w+)</status>"
    r"(?:.*?exit code (\d+))?",
    re.S,
)


def in_project(path, cwd):
    path = os.path.expanduser(path)
    if NON_CODE.search(path):
        return False
    if cwd and path.startswith(cwd):  # a project may itself live under /tmp
        return True
    return not TEMP_DIR.match(path) and (not os.path.isabs(path) or not cwd)


def _writes_code(command, cmd, cwd):
    targets = REDIRECT.findall(cmd)
    for m in SED_INPLACE.finditer(cmd):
        try:
            args = shlex.split(m.group(1))
        except ValueError:
            continue
        targets += [a for a in args if " " not in a and not re.match(r"s[/#|,]", a)]  # skip the s/../../ expr
    # a heredoc body is code only when fed to an interpreter; in `git commit`/`gh pr` it's prose
    if INTERPRETER_HEREDOC.search(command) and SCRIPT_WRITE.search(command):
        targets += PATH_LITERAL.findall(command)
    return any(CODE_FILE.search(t.strip("'\"")) and in_project(t.strip("'\""), cwd) for t in targets)


def last_result(output):
    """"pass" | "fail" | None from the last result line: `mutate; test; restore; test` ends green."""
    last = None
    for line in output.splitlines():
        if FAIL_OUTPUT.search(line):
            last = "fail"
        elif PASS_OUTPUT.search(line):
            last = "pass"
    return last


def build_result(output, ok):
    if BUILD_REACHED_TESTS.search(output):
        return True
    if BUILD_FAIL.search(output):
        return False
    return True if BUILD_PASS.search(output) else ok


def _add_bash(events, command, exit_ok, output, cwd):
    cmd = HEREDOC.sub("<<", command)  # heredoc bodies are data, not commands
    writes = _writes_code(command, cmd, cwd)
    if writes:  # `sed -i x.py && pytest`: the edit comes first
        events.append(("edit",))
    if not exit_ok and ENV_ERROR.search(output):
        return
    last = last_result(output)
    # `npm test && git push` can exit non-zero after a green test run
    ok = last == "pass" if last else exit_ok
    results = {}  # kind -> did that part pass
    if runs(TEST_CMD, cmd) or TEST_OUTPUT.search(output):
        results["test"] = ok
    if runs(BUILD_CMD, cmd) or BUILD_OUTPUT.search(output):
        results["build"] = build_result(output, exit_ok or ok)
    if runs(REMOTE_CHECK, cmd):  # CI / Pages results checked with gh are a receipt for both
        results.setdefault("test", ok)
        results.setdefault("build", ok)
    if not writes or results:
        events.append(("cmd", cmd, ok, results))


def _read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def read_transcript(path, upto=None):
    """-> (commands since the last edit, edited at all?, final_text). commands: [(cmd, ok)]."""
    with open(path, encoding="utf-8") as f:
        codex = '"session_meta"' in f.readline()
        f.seek(0)
        head = f.read() if upto is None else "".join(line for _, line in zip(range(upto), f))
    used_tools = any(k in head for k in TOOL_MARKERS)
    if codex:
        events, final_text = _codex_events(path, upto)
    else:
        events, final_text = _events(path, path[: -len(".jsonl")], 0, upto)
    return split(events) + ("\n".join(final_text), used_tools)


TOOL_MARKERS = ('"tool_use"', '"CommandExecution"', '"FileChange"', '"function_call"', '"custom_tool_call"')


def split(events):
    """-> (commands since the last edit as (cmd, ok, results), edited at all?)"""
    last_edit = max((i for i, e in enumerate(events) if e[0] == "edit"), default=-1)
    return [e[1:] for e in events[last_edit + 1:] if e[0] == "cmd"], last_edit >= 0


CODEX_EXIT = re.compile(r"(?:Process exited with code|Exit code:) (\d+)")
CODEX_RUNNING = re.compile(r"Process running with session ID (\d+)")
# code mode: a JS cell calls tools.exec_command({cmd:"..."}); no exit code is logged
JS_EXEC = re.compile(r"""exec_command\(\{\s*["']?cmd["']?\s*:\s*("(?:[^"\\]|\\.)*")""")
JS_CELL_RUNNING = re.compile(r"Script running with cell ID (\d+)")
JS_POLL = re.compile(r"write_stdin\(\{[^}]*?session_id\s*:\s*(\d+)")
JSON_EXIT = re.compile(r'"exit_code"\s*:\s*(\d+)')
JSON_SESSION = re.compile(r'"session_id"\s*:\s*(\d+)')
PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def _codex_output(out):
    out = _text(out) if not isinstance(out, str) else out
    return out.split("Output:\n", 1)[-1]


def _codex_events(path, upto=None, marks=None):
    """Codex rollout JSONL. Newer builds log item_completed (CommandExecution, FileChange);
    older ones only exec_command / write_stdin / apply_patch calls. Both are read, deduped by call id."""
    events, final_text, cwd = [], [], None
    calls, polls, sessions, done = {}, {}, {}, set()
    cells, cell_waits = {}, {}  # code-mode cell id -> cmd; wait() call id -> cell id
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f):
            if upto is not None and n >= upto:
                break
            if marks is not None and n in marks:  # replay: state as of this turn's end
                marks[n] = (len(events), list(final_text))
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            p = entry.get("payload")
            if not isinstance(p, dict):
                continue
            cwd = p.get("cwd") if entry.get("type") in ("session_meta", "turn_context") and p.get("cwd") else cwd
            kind = p.get("type")
            if kind in ("function_call", "custom_tool_call"):
                name, call_id = p.get("name"), p.get("call_id")
                try:
                    args = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if name in ("exec_command", "shell"):
                    cmd = args.get("cmd") or args.get("command") or ""
                    calls[call_id] = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd)
                elif name == "exec":
                    cmds = []
                    for lit in JS_EXEC.findall(p.get("input") or ""):
                        try:
                            cmds.append(json.loads(lit))
                        except ValueError:  # JS-only escapes like \$ aren't JSON; the raw text still matches
                            cmds.append(lit[1:-1].replace('\\"', '"').replace("\\n", "\n"))
                    poll = JS_POLL.search(p.get("input") or "")
                    if cmds:
                        calls[call_id] = ("js", "\n".join(cmds))
                    elif poll:
                        polls[call_id] = poll.group(1)
                elif name == "wait" and args.get("cell_id") is not None:
                    cell_waits[call_id] = str(args["cell_id"])
                elif name == "write_stdin":
                    polls[call_id] = str(args.get("session_id"))
                elif name == "apply_patch":
                    final_text = []
                    if any(in_project(f.strip(), cwd) for f in PATCH_FILE.findall(p.get("input") or "")):
                        events.append(("edit",))
                continue
            if kind in ("function_call_output", "custom_tool_call_output"):
                call_id, out = p.get("call_id"), p.get("output")
                out = out if isinstance(out, str) else _text(out)
                cmd = calls.pop(call_id, None)
                if cmd is None and call_id in cell_waits:
                    cmd = cells.pop(cell_waits.pop(call_id), None)
                if cmd is None and call_id in polls:
                    cmd = sessions.get(polls.pop(call_id))
                if cmd is None or call_id in done:
                    continue
                if isinstance(cmd, tuple) and JS_CELL_RUNNING.search(out):  # finishes in a later wait()
                    cells[JS_CELL_RUNNING.search(out).group(1)] = cmd
                    continue
                if isinstance(cmd, tuple):  # code-mode cell: exit codes only show up as JSON in its output
                    codes, running = JSON_EXIT.findall(out), JSON_SESSION.findall(out)
                    text = _codex_output(out).replace("\\n", "\n")
                    if codes or not running:
                        final_text = []
                        done.add(call_id)
                        ok = out.startswith("Script completed") and all(c == "0" for c in codes)
                        _add_bash(events, cmd[1], ok, text, cwd)
                        sessions = {k: v for k, v in sessions.items() if v is not cmd}
                    else:
                        sessions[running[-1]] = cmd
                    continue
                exited, running = CODEX_EXIT.search(out), CODEX_RUNNING.search(out)
                if exited:
                    final_text = []
                    done.add(call_id)
                    _add_bash(events, cmd, exited.group(1) == "0", _codex_output(out), cwd)
                    sessions = {k: v for k, v in sessions.items() if v is not cmd}
                elif running:
                    sessions[running.group(1)] = cmd
                continue
            item = p.get("item") if kind == "item_completed" else None
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "AgentMessage":
                final_text.append(_text([{"text": c.get("text", "")} for c in item.get("content") or []]))
            elif kind == "UserMessage":
                final_text = []
            elif kind == "CommandExecution":
                final_text = []
                if item.get("id") in done:
                    continue
                done.add(item.get("id"))
                calls.pop(item.get("id"), None)
                cmd = item.get("command")
                cmd = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd or "")
                ok = item.get("status") == "completed" and item.get("exit_code") == 0
                _add_bash(events, cmd, ok, item.get("aggregated_output") or "", cwd)
            elif kind == "FileChange" and item.get("status") in (None, "completed"):
                final_text = []
                if any(in_project(path, cwd) for path in item.get("changes") or {}):
                    events.append(("edit",))
    return events, final_text


def _events(path, session_dir, depth, upto=None, until=None, marks=None):
    """-> (events, final_text). Subagent transcripts are spliced in where their Agent call returned."""
    pending = {}  # tool_use_id -> ("cmd" | "bg", command) | ("edit", None)
    background = {}  # tool_use_id -> command, settled by a later <task-notification>
    async_agents = {}  # tool_use_id -> subagent transcript, spliced in when it reports done
    events = []  # ("cmd", command, ok, {kind: passed}) | ("edit",)
    final_text = []
    cwd = None
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f):
            if upto is not None and n >= upto:  # replay: the session as it was at that turn
                break
            if marks is not None and n in marks:
                marks[n] = (len(events), list(final_text))
            note = TASK_NOTE.search(line.replace("\\n", "\n"))
            if note and note.group(1) in async_agents:
                sub = async_agents.pop(note.group(1))
                stamp = re.search(r'"timestamp":\s*"([^"]+)"', line)
                if os.path.exists(sub):  # a subagent log can keep growing later (SendMessage); cut it here
                    events += _events(sub, session_dir, depth + 1, until=stamp and stamp.group(1))[0]
                continue
            if note and note.group(1) in background:
                use_id, out_file, status, code = note.groups()
                ok = status == "completed" and code in (None, "0")
                _add_bash(events, background.pop(use_id), ok, _read_file(out_file), cwd)
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if until and entry.get("timestamp", "") > until:
                break
            if entry.get("isSidechain") and depth == 0:
                continue
            cwd = entry.get("cwd") or cwd
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list):
                if entry.get("type") == "user" and not entry.get("isMeta"):
                    final_text = []
                continue
            for b in content:
                kind = b.get("type")
                if kind == "text" and entry.get("type") == "assistant":
                    final_text.append(b.get("text", ""))
                elif kind == "text" and entry.get("type") == "user":
                    final_text = []
                elif kind == "tool_use":
                    final_text = []
                    inp = b.get("input") or {}
                    if b.get("name") == "Bash":
                        bg = "bg" if inp.get("run_in_background") else "cmd"
                        pending[b.get("id")] = (bg, inp.get("command", ""))
                    elif b.get("name") in EDIT_TOOLS and in_project(inp.get("file_path", ""), cwd):
                        pending[b.get("id")] = ("edit", None)
                elif kind == "tool_result":
                    final_text = []
                    agent = (entry.get("toolUseResult") or {}) if depth < 3 else {}
                    if isinstance(agent, dict) and agent.get("agentId"):
                        sub = os.path.join(session_dir, "subagents", "agent-%s.jsonl" % agent["agentId"])
                        if agent.get("isAsync") or agent.get("status") == "async_launched":
                            # its work counts when it finishes; work still in flight isn't the parent's claim
                            async_agents[b.get("tool_use_id")] = sub
                        elif os.path.exists(sub):
                            events += _events(sub, session_dir, depth + 1, until=entry.get("timestamp"))[0]
                    use = pending.pop(b.get("tool_use_id"), None)
                    if not use:
                        continue
                    ok = not b.get("is_error")
                    if use[0] == "edit":
                        if ok:
                            events.append(("edit",))
                    elif use[0] == "bg":
                        background[b.get("tool_use_id")] = use[1]
                    else:
                        _add_bash(events, use[1], ok, _text(b.get("content")), cwd)
    return events, final_text


def missing_evidence(claim, commands, edited):
    if claim == "works":  # anything that exercised the code and succeeded counts
        ran = [ok for cmd, ok, results in commands if results or not READ_ONLY.search(cmd)]
        ran = ran and [any(ran)]
    else:
        kind = "test" if claim == "tests_pass" else "build"
        ran = [results[kind] for cmd, ok, results in commands if kind in results]
    since = "since your last edit" if edited else "in this session"
    if not ran:
        return "no matching command ran " + since
    if not ran[-1]:
        return "the last matching command " + since + " failed"
    return None


def verdict(hook_input, upto=None):
    if hook_input.get("stop_hook_active"):
        return None
    commands, edited, final_text, used_tools = read_transcript(hook_input["transcript_path"], upto)
    return judge(commands, edited, hook_input.get("last_assistant_message") or final_text, used_tools)


def judge(commands, edited, text, used_tools):
    if not used_tools:  # a chat with no tool calls (advice, Q&A) has nothing to check against
        return None
    problems = []
    for claim in sorted(detect_claims(text)):
        why = missing_evidence(claim, commands, edited)
        if why and why.startswith("the last") and ADMITS_FAILURE.search(text):
            continue
        if why:
            problems.append(REASONS[claim].format(why=why))
    if not problems:
        return None
    return {"decision": "block", "reason": "nocap 🧢 — " + " Also, ".join(problems)}


def main():
    try:  # bytes in and out: Windows consoles default to cp1252, hook payloads are UTF-8
        out = verdict(json.loads(sys.stdin.buffer.read().decode("utf-8")))
    except Exception as e:  # never break the user's session
        print("nocap: skipped ({})".format(e), file=sys.stderr)
        return
    if out:
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")


if __name__ == "__main__":
    main()
