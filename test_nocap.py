"""Run: python3 test_nocap.py"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "hooks"))
import nocap  # noqa: E402

HOOK = os.path.join(os.path.dirname(__file__), "hooks", "nocap.py")
_n = [0]


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool(name, inp, ok=True, output=""):
    _n[0] += 1
    tid = "t%d" % _n[0]
    return [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": output, "is_error": not ok}]}},
    ]


def bash(cmd, ok=True, output=""):
    return tool("Bash", {"command": cmd}, ok, output)


def edit():
    return tool("Edit", {"file_path": "a.py"})


def run(*parts, active=False):
    lines = []
    for p in parts:
        lines.extend(p if isinstance(p, list) else [p])
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(x) for x in lines))
    return nocap.verdict({"transcript_path": f.name, "stop_hook_active": active})


def blocked(*parts, **kw):
    return run(*parts, **kw) is not None


# claim with no evidence -> block
assert blocked(user("fix it"), edit(), say("Done. All tests pass."))
# claim with a passing test run after the edit -> allow
assert not blocked(user("fix it"), edit(), bash("python -m pytest -q", output="5 passed"), say("All tests pass."))
# edited after the last test run -> block
assert blocked(user("fix it"), bash("npm test"), edit(), say("Tests pass."))
# last test run failed -> block
assert blocked(user("x"), edit(), bash("pytest", ok=False, output="Exit code 1"), say("All tests pass now."))
# `pytest | tail` exits 0 but output shows failures -> block
assert blocked(user("x"), edit(), bash("pytest | tail -3", output="2 failed, 3 passed"), say("Tests pass."))
# negated / hedged claims are not claims
assert not blocked(user("x"), edit(), say("I haven't run the tests, so I can't confirm tests pass."))
assert not blocked(user("x"), edit(), say("Tests should pass now."))
# no claim -> allow
assert not blocked(user("x"), edit(), say("I updated the README."))
# Korean claims
assert blocked(user("고쳐줘"), edit(), say("수정 완료했습니다. 테스트 모두 통과합니다."))
assert blocked(user("고쳐줘"), edit(), say("버그를 해결했습니다."))
assert not blocked(user("고쳐줘"), edit(), say("테스트는 아직 실행하지 않았습니다."))
# build claim needs a build command, test run is not enough
assert blocked(user("x"), edit(), bash("pytest", output="3 passed"), say("The build succeeds."))
assert not blocked(user("x"), edit(), bash("npm run build"), say("The build succeeds."))
# "fixed" accepts a test or a build
assert not blocked(user("x"), edit(), bash("cargo test"), say("The bug is fixed."))
# background runs count only once their <task-notification> says they finished cleanly
def bg(cmd, status=None, code="0"):
    parts = tool("Bash", {"command": cmd, "run_in_background": True}, output="Command running in background")
    if status:
        note = ("<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>t%d</tool-use-id>\n<output-file>/nope</output-file>\n"
                "<status>%s</status>\n<summary>Background command completed (exit code %s)</summary>\n</task-notification>"
                % (_n[0], status, code))
        parts.append({"type": "attachment", "attachment": {"type": "queued_command", "prompt": note}})
    return parts


assert blocked(user("x"), edit(), bg("pytest"), say("Tests pass."))
assert not blocked(user("x"), edit(), bg("pytest", "completed"), say("Tests pass."))
assert blocked(user("x"), edit(), bg("pytest", "completed", code="1"), say("Tests pass."))
assert blocked(user("x"), edit(), bg("pytest", "failed"), say("Tests pass."))
# only the final message counts, not earlier text in the turn
assert not blocked(user("x"), say("Goal: make tests pass."), edit(), say("Edited a.py."))
# second stop after a block -> allow (no infinite loop)
assert not blocked(user("x"), edit(), say("All tests pass."), active=True)
# no edits at all, claim without running anything -> block
assert blocked(user("do tests pass?"), bash("ls"), say("Yes, all tests pass."))
# a chat with no tool calls at all (advice, Q&A) has nothing to check against
assert not blocked(user("how do I set up CI?"), say("Run it locally first and confirm the build succeeds."))

# editing docs/memory after the test run doesn't make the claim stale
assert not blocked(user("x"), edit(), bash("npx vitest run"), tool("Edit", {"file_path": "README.md"}), say("40 tests pass."))
# code edited through Bash after the test run does
assert blocked(user("x"), bash("pytest"), bash("sed -i '' 's/a/b/' app.py"), say("All tests pass."))
# edit + test in one command: the test ran after the edit
assert not blocked(user("x"), bash("sed -i '' 's/a/b/' app.swift && xcodebuild -scheme A test"), say("All tests pass."))
# heredoc bodies are not commands ("make sure the test" is not `make test`)
assert blocked(user("x"), edit(), bash("python3 - <<'EOF'\n# make sure the test passes\nprint(1)\nEOF"), say("All tests pass."))
# only writes to code files inside the project are edits
assert not blocked(user("x"), edit(), bash("pytest"), bash("cat > /tmp/probe.mjs <<'EOF'\nconsole.log(1)\nEOF"), say("Tests pass."))
assert not blocked(user("x"), edit(), bash("pytest"), bash("cat >> docs/design.md <<'EOF'\nsee app.swift > old.swift\nEOF"), say("Tests pass."))
assert not blocked(user("x"), edit(), bash("pytest"), bash("python3 - <<'PY'\np='CLAUDE.md'; s=open(p).read()  # scripts/x.mjs\nopen(p,'w').write(s)\nPY"), say("Tests pass."))
assert blocked(user("x"), bash("pytest"), bash("python3 - <<'PY'\np='src/app.py'; s=open(p).read()\nopen(p,'w').write(s)\nPY"), say("Tests pass."))
assert not blocked(dict(user("x"), cwd="/repo"), edit(), bash("pytest"), tool("Write", {"file_path": "/elsewhere/scratch.py"}), say("Tests pass."))
# mutate -> red -> restore -> green in one command: the last result line wins
assert not blocked(user("x"), edit(), bash("npx vitest run; cp a.bak a.ts; npx vitest run", output="Tests  1 failed | 9 passed\nTests  10 passed"), say("Tests pass."))
assert blocked(user("x"), edit(), bash("npx vitest run", output="Tests  1 failed | 9 passed"), say("Tests pass."))
# a sed expression that mentions a .swift file is not an edit to it
assert not blocked(user("x"), edit(), bash("swift test"), bash("sed -i '' 's#swiftc -O a.swift#swiftc a.swift#' build.sh"), say("Tests pass."))
assert blocked(user("x"), bash("swift test"), bash("sed -i '' 's/a/b/' Sources/a.swift"), say("Tests pass."))
# a project that lives under /tmp still has edits
r = run(dict(user("x"), cwd="/tmp/proj"), tool("Edit", {"file_path": "/tmp/proj/calc.py"}), say("All tests pass."))
assert r and "since your last edit" in r["reason"], r
# a commit message heredoc is prose, even if it quotes "> src/app.ts"
assert not blocked(user("x"), edit(), bash("pytest"), bash("git commit -F - <<'EOF'\nfix\n> src/app.ts was wrong\nEOF"), say("Tests pass."))
# a python heredoc that only touches docs is not a code edit
assert not blocked(user("x"), edit(), bash("pytest"), bash("python3 - <<'EOF'\nopen('notes.md','w').write('x')\nEOF"), say("Tests pass."))
# quoted args in the test command still count; output with a real error line does not
assert not blocked(user("x"), edit(), bash("xcodebuild -scheme A -destination 'name=iPhone 16' test"), say("Tests pass."))
assert blocked(user("x"), edit(), bash("xcodebuild build | tail", output="a.swift:4:2: error: nope\n** BUILD FAILED **"), say("The build succeeds."))
# mentions, quotes and code are not claims
assert not blocked(user("x"), edit(), say('This hook blocks fake "tests pass" claims.'))
assert not blocked(user("x"), edit(), say("테스트가 통과해도 결함일 수 있습니다."))
assert not blocked(user("x"), edit(), say("테스트 통과 여부로 판정합니다."))
assert not blocked(user("x"), edit(), say("빌드 시에만 자동으로 사용됩니다."))
assert blocked(user("x"), edit(), say("전체 88개 테스트 통과, 빌드 정상입니다."))
assert blocked(user("x"), edit(), say("테스트 307개 통과, 커밋 완료"))
assert not blocked(user("x"), edit(), say("대상은 세 가지입니다: 테스트 통과, 빌드 성공, 완료."))
# "works" is satisfied by actually running something, not by git/ls
assert not blocked(user("x"), edit(), bash("./run.sh --smoke"), say("It works now."))
assert blocked(user("x"), edit(), bash("git commit -m fix"), say("It works now."))

# tests green, then a later step (push) failed: the test run still counts
assert not blocked(user("x"), edit(), bash("npm test && git push", ok=False, output="Tests  9 passed\n! [rejected] main"), say("Tests pass."))

# a subagent that edited and ran the tests is evidence for the parent's claim
def with_subagent(sub_parts, *parent_after):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "s1", "subagents"))
    sub = [dict(x, isSidechain=True) for p in sub_parts for x in (p if isinstance(p, list) else [p])]
    with open(os.path.join(d, "s1", "subagents", "agent-a1.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(x) for x in sub))
    parent = [user("implement it"),
              {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ag1", "name": "Agent", "input": {}}]}},
              {"type": "user", "toolUseResult": {"agentId": "a1", "status": "completed"},
               "message": {"content": [{"type": "tool_result", "tool_use_id": "ag1", "content": "done"}]}}]
    for p in parent_after:
        parent.extend(p if isinstance(p, list) else [p])
    path = os.path.join(d, "s1.jsonl")
    with open(path, "w") as f:
        f.write("\n".join(json.dumps(x) for x in parent))
    return nocap.verdict({"transcript_path": path}) is not None


assert not with_subagent([edit(), bash("pytest", output="3 passed")], say("All tests pass."))
assert with_subagent([bash("pytest", output="3 passed"), edit()], say("All tests pass."))
assert with_subagent([edit(), bash("pytest", output="3 passed")], edit(), say("All tests pass."))

# Codex rollout transcripts
def codex(*items):
    lines = [{"type": "session_meta", "payload": {"cwd": "/repo"}}]
    for kind, extra in items:
        lines.append({"type": "event_msg", "payload": {"type": "item_completed", "item": dict(extra, type=kind)}})
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(x) for x in lines))
    return nocap.verdict({"transcript_path": f.name}) is not None


def cx_run(cmd, code=0, out=""):
    return ("CommandExecution", {"command": ["/bin/zsh", "-lc", cmd], "status": "completed", "exit_code": code, "aggregated_output": out})


CX_EDIT = ("FileChange", {"changes": {"/repo/app.py": {"type": "update"}}, "status": "completed"})
CX_SAY = ("AgentMessage", {"content": [{"type": "Text", "text": "Fixed. All tests pass."}]})
assert codex(("UserMessage", {}), CX_EDIT, CX_SAY)
assert not codex(("UserMessage", {}), CX_EDIT, cx_run("pytest -q", out="4 passed"), CX_SAY)
assert codex(("UserMessage", {}), CX_EDIT, cx_run("pytest -q", code=1, out="1 failed"), CX_SAY)
assert codex(("UserMessage", {}), cx_run("pytest -q", out="4 passed"), CX_EDIT, CX_SAY)
assert not codex(("UserMessage", {}), CX_EDIT, cx_run("pytest -q", out="4 passed"),
                 ("FileChange", {"changes": {"/elsewhere/notes.py": {}}, "status": "completed"}), CX_SAY)

# legacy Codex records: exec_command / write_stdin / apply_patch
def codex_raw(*payloads):
    lines = [{"type": "session_meta", "payload": {"cwd": "/repo"}}] + [{"type": "response_item", "payload": p} for p in payloads]
    lines.append({"type": "event_msg", "payload": {"type": "item_completed", "item": CX_SAY[1] | {"type": "AgentMessage"}}})
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(x) for x in lines))
    return nocap.verdict({"transcript_path": f.name}) is not None


PATCH = {"type": "custom_tool_call", "name": "apply_patch", "call_id": "p1", "input": "*** Begin Patch\n*** Update File: /repo/app.py\n@@"}
EXEC = {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": json.dumps({"cmd": "pytest -q"})}
assert codex_raw(PATCH)
assert not codex_raw(PATCH, EXEC, {"type": "function_call_output", "call_id": "c1", "output": "Process exited with code 0\nOutput:\n4 passed\n"})
assert codex_raw(PATCH, EXEC, {"type": "function_call_output", "call_id": "c1", "output": "Process exited with code 1\nOutput:\n1 failed\n"})
# a long run finishes through write_stdin polling
assert not codex_raw(PATCH, EXEC, {"type": "function_call_output", "call_id": "c1", "output": "Process running with session ID 7\n"},
                     {"type": "function_call", "name": "write_stdin", "call_id": "w1", "arguments": json.dumps({"session_id": 7})},
                     {"type": "function_call_output", "call_id": "w1", "output": "Process exited with code 0\nOutput:\n4 passed\n"})

# Codex code mode: commands inside a JS cell, judged by output
JS = {"type": "custom_tool_call", "name": "exec", "call_id": "j1",
      "input": 'const r = await tools.exec_command({cmd:"npm test",workdir:"/repo"});\ntext(r.output);'}
assert not codex_raw(PATCH, JS, {"type": "custom_tool_call_output", "call_id": "j1", "output": [{"type": "input_text", "text": "Script completed\nOutput:\n"}, {"type": "input_text", "text": "Tests  12 passed\n"}]})
assert codex_raw(PATCH, JS, {"type": "custom_tool_call_output", "call_id": "j1", "output": [{"type": "input_text", "text": "Script completed\nOutput:\n"}, {"type": "input_text", "text": "Tests  2 failed | 10 passed\n"}]})
assert codex_raw(PATCH, JS, {"type": "custom_tool_call_output", "call_id": "j1", "output": "Script failed\nError: boom"})

# a script with any name counts when its output shows the tests / build ran
assert not blocked(user("x"), edit(), bash("npm run check", output="Test Files  3 passed (3)\n     Tests  31 passed (31)"), say("31 tests pass."))
assert blocked(user("x"), edit(), bash("npm run lint", output="lint ok"), say("31 tests pass."))
assert not blocked(user("x"), edit(), bash("./scripts/ci.sh", output="vite v6\n✓ built in 1.2s"), say("The build succeeds."))

# a JS string with escapes JSON can't decode is still read
JS_ESC = {"type": "custom_tool_call", "name": "exec", "call_id": "j1",
          "input": 'await tools.exec_command({cmd:"cd \\$HOME/app && npm test"});'}
assert not codex_raw(PATCH, JS_ESC, {"type": "custom_tool_call_output", "call_id": "j1", "output": "Script completed\nOutput:\nTests  3 passed"})

# code mode: a long run polled through tools.write_stdin
JS_START = {"type": "custom_tool_call_output", "call_id": "j1", "output": 'Script completed\nOutput:\n{"session_id":42,"output":"Compiling"}'}
POLL = {"type": "custom_tool_call", "name": "exec", "call_id": "j2", "input": "const r = await tools.write_stdin({session_id: 42, chars: ''});"}
assert not codex_raw(PATCH, JS, JS_START, POLL, {"type": "custom_tool_call_output", "call_id": "j2", "output": 'Script completed\nOutput:\n{"exit_code":0,"output":"Tests  5 passed"}'})
assert codex_raw(PATCH, JS, JS_START, POLL, {"type": "custom_tool_call_output", "call_id": "j2", "output": 'Script completed\nOutput:\n{"exit_code":1,"output":"boom"}'})
assert codex_raw(PATCH, JS, JS_START)  # never finished

# a tool only counts in command position
assert blocked(user("x"), edit(), bash('rg -n "pytest|vitest" README.md'), say("All tests pass."))
assert not blocked(user("x"), edit(), bash("cd app && .venv/bin/pytest -q", output="3 passed"), say("All tests pass."))
assert not blocked(user("x"), edit(), bash("npx vitest run", output="Tests  3 passed"), say("All tests pass."))
assert not blocked(user("x"), edit(), bash("python3 test_nocap.py", output="ok"), say("All tests pass."))
# node's built-in test runner output
assert not blocked(user("x"), edit(), bash("npm run check", output="# tests 8\n# pass 8\n# fail 0"), say("8 tests pass."))
assert blocked(user("x"), edit(), bash("npm run check", output="ℹ tests 8\nℹ pass 7\nℹ fail 1"), say("8 tests pass."))
# code mode: a cell that keeps running finishes in a later wait(cell_id)
RUNNING = {"type": "custom_tool_call_output", "call_id": "j1", "output": "Script running with cell ID 11\nOutput:\n"}
WAIT = {"type": "function_call", "name": "wait", "call_id": "w9", "arguments": json.dumps({"cell_id": "11"})}
assert not codex_raw(PATCH, JS, RUNNING, WAIT, {"type": "function_call_output", "call_id": "w9", "output": "Script completed\nOutput:\nTests  4 passed"})
assert codex_raw(PATCH, JS, RUNNING, WAIT, {"type": "function_call_output", "call_id": "w9", "output": "Script failed\nError"})

# nested quotes: the quoted claim is still a quote
assert not blocked(user("x"), edit(), say('Description: "Your agent said "done". nocap blocks unverified "tests pass" claims."'))

# manual QA checklists and claims about other PRs aren't this session's test runs
assert not blocked(user("x"), edit(), say("✅ 25/25 테스트 항목 통과"))
assert not blocked(user("x"), edit(), say("PR #7~#9 were merged after tests passed."))
# CI checked with gh is a receipt
assert not blocked(user("x"), edit(), bash("gh pr checks 12 --watch", output="test  pass"), say("All tests pass."))
# a background agent's work counts when it reports done, not while it runs
def async_agent(sub_parts, done, *after):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "s1", "subagents"))
    with open(os.path.join(d, "s1", "subagents", "agent-a1.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(dict(x, isSidechain=True)) for p in sub_parts for x in (p if isinstance(p, list) else [p])))
    lines = [user("go"),
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ag1", "name": "Agent", "input": {}}]}},
             {"type": "user", "toolUseResult": {"agentId": "a1", "isAsync": True, "status": "async_launched"},
              "message": {"content": [{"type": "tool_result", "tool_use_id": "ag1", "content": "launched"}]}}]
    if done:
        lines.append({"type": "attachment", "attachment": {"type": "queued_command", "prompt":
            "<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>ag1</tool-use-id>\n<output-file>/nope</output-file>\n<status>completed</status>\n</task-notification>"}})
    for p in after:
        lines.extend(p if isinstance(p, list) else [p])
    path = os.path.join(d, "s1.jsonl")
    with open(path, "w") as f:
        f.write("\n".join(json.dumps(x) for x in lines))
    return nocap.verdict({"transcript_path": path}) is not None


assert not async_agent([edit(), bash("pytest", output="3 passed")], True, say("All tests pass."))
assert not async_agent([edit()], False, bash("pytest", output="3 passed"), say("All tests pass."))  # still running
assert async_agent([edit()], True, say("All tests pass."))

# a build passes inside `xcodebuild test` even when a test fails
assert not blocked(user("x"), edit(), bash("xcodebuild -scheme A test", ok=False, output="a.swift:3: error: XCTAssertEqual failed\n** TEST FAILED **"), say("The build succeeds."))
assert blocked(user("x"), edit(), bash("xcodebuild -scheme A test", ok=False, output="** TEST FAILED **"), say("All tests pass."))
# the tool never ran: no evidence either way, the earlier green run stands
assert not blocked(user("x"), edit(), bash("xcodebuild build", output="** BUILD SUCCEEDED **"),
                   bash("xcodebuild -destination x build", ok=False, output="xcodebuild: error: Unable to find a device matching"), say("The build succeeds."))
# dotfile edits don't make a run stale; project check scripts count
assert not blocked(user("x"), edit(), bash("npm test", output="8 passed"), tool("Write", {"file_path": ".gitignore"}), say("8 tests pass."))
assert not blocked(user("x"), edit(), bash("npm run check"), say("All tests pass."))
assert not blocked(user("x"), edit(), bash("node --experimental-strip-types --test tests/a.test.ts"), say("All tests pass."))

# info-only invocations aren't runs
assert blocked(user("x"), edit(), bash("xcodebuild -list && tsc --version"), say("The build succeeds."))
# a red run the message owns up to is not a hidden claim
assert not blocked(user("x"), edit(), bash("npm test", ok=False, output="Tests  1 failed | 23 passed"),
                   say("23 unit tests pass. The E2E test failed on a network error, unrelated to this change."))
assert blocked(user("x"), edit(), bash("npm test", ok=False, output="Tests  1 failed | 23 passed"), say("All tests pass."))

# end-to-end through stdin: blocks with JSON, and never crashes on bad input
with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
    f.write("\n".join(json.dumps(x) for x in [user("x")] + edit() + [say("All tests pass.")]))
out = subprocess.run([sys.executable, HOOK], input=json.dumps({"transcript_path": f.name}),
                     capture_output=True, text=True, encoding="utf-8")
assert out.returncode == 0 and json.loads(out.stdout)["decision"] == "block", out
out = subprocess.run([sys.executable, HOOK], input="garbage", capture_output=True, text=True, encoding="utf-8")
assert out.returncode == 0 and out.stdout == "", out

print("ok")
