"""One bounded native Codex integration exercise, using already-trusted hooks."""
import os
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import json
import re
from journal_core import BASE, now, put_json, redact, digest, read_json


def verify(report, base=BASE):
    events = []
    session_id = report.get("native_session")
    for line in report["bounded_redacted_output"].splitlines():
        try:
            item = json.loads(line)
            if item.get("type") == "thread.started":
                session_id = item["thread_id"]
        except (ValueError, KeyError):
            pass
    for p in (base / "events/demo-project").glob("*.json"):
        event = read_json(p)
        if event.get("session_id") == session_id and event.get("tool_use_id"):
            events.append(event)
    receipts = [read_json(p) for p in (base / "state/receipts/demo-project").glob("*.json")]
    event_ids = {x["event_id"] for x in events}
    linked = False
    for receipt in receipts:
        path = Path(receipt.get("path", ""))
        if (set(receipt.get("event_ids", [])) & event_ids
                and receipt.get("status") in ("published", "unchanged")
                and path.is_file() and digest(path.read_bytes()) == receipt.get("content_hash")):
            linked = True
    tests_passed = any(e.get("classification") == "test_result" and e.get("exit_code") == 0
        and re.search(r"Ran \d+ tests?", e.get("excerpt", ""))
        and re.search(r"\bOK\b", e.get("excerpt", "")) for e in events)
    retries = [read_json(p) for p in (base / "state/retries").glob("*.json")]
    stop_continuation = any(set(r.get("events", [])) & event_ids for r in retries)
    report["verification"] = {"native_session": session_id, "native_hook_events": len(events), "linked_publication_receipt": linked,
                              "native_tests_passed": bool(tests_passed), "stop_continuation_observed": stop_continuation,
                              "module_exists": (base / "demo-project/title_tools.py").exists(),
                              "tests_exist": bool(list((base / "demo-project").rglob("test*title*.py")))}
    complete = all((report["exit_code"] == 0, not report.get("timed_out"), session_id,
        event_ids, linked, tests_passed, report["verification"]["module_exists"], report["verification"]["tests_exist"],
        stop_continuation or not report.get("exercise_stop")))
    sandbox_failure = re.search(r"setup refresh had errors|helper_(?:unknown_error|sandbox_lock_failed)|failed to create unified exec process",
                                report["bounded_redacted_output"], re.I)
    report["status"] = "passed" if complete else "blocked" if sandbox_failure else "failed"
    return report

prompt = '''这是一个本地集成验收开发任务。在当前 demo-project 中实现 title_tools.py：
normalize_title(text) 合并空白并去掉首尾空白，空字符串或全空白输入抛出 ValueError。
只使用 Python 标准库。为普通中文标题、重复空白和空标题编写至少三个 unittest 用例，
直接使用 python -m unittest 运行这些测试，以验证 PostToolUse 自动采集，并简短报告实际结果。保持 expectation_check.py 不变。
遵循当前已安装的项目指令；不要修改 Hook 信任、全局配置或非本项目的任何代码。
这是一次隔离验收，只修改上述模块和测试，并使用已配置的本地工程记录流程。
发布时使用绝对路径 D:/Computer-Science/C-obsidian/publisher.py（默认配置即可）。
本轮自动链路里程碑使用 MS-NATIVE-AUTO-001，fingerprint=native-auto-acceptance；它与标题模块功能里程碑不同。
更新已有条目只提交本轮新证据，使用带事件ID的新evidence id；旧证据交给发布器保留，不复制旧evidence数组。
本机CLI的Bash Hook可能只传stdout，缺少结构化退出码。因此直接测试请用PowerShell执行：
python -m unittest discover -s tests -v; $journalTestExit = $LASTEXITCODE; Write-Output "Exit code: $journalTestExit"; exit $journalTestExit
必须打印实际保存的退出码，不能把OK文本推断为退出码0。
'''

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--exercise-stop", action="store_true", help="Intentionally defer first publication to exercise one Stop continuation")
    parser.add_argument("--windows-sandbox", choices=("mxc", "elevated"), help="One-run sandbox override; global config remains unchanged")
    args = parser.parse_args()
    if args.audit_only:
        from journal_core import read_json
        result = verify(read_json(BASE / "docs/native-codex-runtime.json"))
        put_json(BASE / "docs/native-codex-runtime.json", result)
        print(json.dumps({"status": result["status"], "verification": result["verification"]}, ensure_ascii=True))
        sys.exit(0 if result["status"] == "passed" else 1)
    executable = shutil.which("codex")
    if not executable:
        raise SystemExit("Codex not found")
    command = [executable, "--no-daemon", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
               "--add-dir", str(BASE), "-C", str(BASE / "demo-project"), "--json", "-"]
    if args.windows_sandbox:
        command[1:1] = ["-c", "windows.sandbox=" + args.windows_sandbox]
    previous = BASE / "docs/native-codex-runtime.json"
    if previous.exists():
        snapshot = BASE / "docs" / ("native-codex-before-" + now().replace(":", "").replace("+", "_") + ".json")
        shutil.copy2(previous, snapshot)
    env = {**os.environ, "PYTHONUTF8": "1"}
    started = now()
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    exercise_prompt = prompt
    if args.exercise_stop:
        exercise_prompt += '''\n本轮专门模拟Stop补漏：首次回答前只完成模块核对和上述直接测试，简短报告后结束，暂不运行journal.py run或publisher.py。
如果Stop Hook随后自动要求继续，再遵循engineering-journal流程完成一次证据发布并核对回执。
这是本轮明确授权的验收步骤，不修改持久规则，也不手动调用hook.py来模拟触发。\n'''
    p.stdin.write(exercise_prompt.encode("utf-8"))
    p.stdin.close()
    output = []
    native_session = None
    timed_out = []
    def terminate():
        timed_out.append(True)
        p.kill()
    timer = threading.Timer(240, terminate)
    timer.start()
    try:
        for line in iter(p.stdout.readline, b""):
            message = redact(line.decode("utf-8", errors="replace"))
            output.append(message)
            if len(output) > 160:
                output.pop(0)
            # Small progress trace; full raw conversation is not persisted.
            import json
            try:
                event = json.loads(message)
                if event.get("type") == "thread.started":
                    native_session = event.get("thread_id")
                print(event.get("type", "event"), flush=True)
            except ValueError:
                print(message[:300].rstrip(), flush=True)
        code = p.wait()
    finally:
        timer.cancel()
        p.stdout.close()
    result = verify({"started": started, "finished": now(), "exit_code": code,
             "native_session": native_session,
             "exercise_stop": args.exercise_stop,
             "timed_out": bool(timed_out), "command": command, "bounded_redacted_output": "".join(output)[-24000:]})
    put_json(BASE / "docs/native-codex-runtime.json", result)
    print("Native Codex integration exit:", code, flush=True)
    print(json.dumps({"status": result["status"], "verification": result["verification"]}, ensure_ascii=False), flush=True)
    sys.exit(0 if result["status"] == "passed" else 1)
