"""Repeatable end-to-end acceptance on the active dedicated test Vault."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import uuid
from journal_core import BASE, Registry, digest, now, put_json
from journal import graph_audit, run_test
from publisher import publish


def evidence(path, label, kind="source", summary="实际本地文件"):
    sha = digest(Path(path).read_bytes())
    if kind == "source":
        label += "-" + sha[:8]
    return {"id": label, "path": str(path), "sha256": sha, "kind": kind,
            "observed_at": now(), "summary": summary}


def draft(pid, eid, typ, title, summary):
    return {"schema_version": 1, "project_id": pid, "entry_id": eid, "type": typ,
            "title": title, "summary": summary, "status": "provisional", "fingerprint": eid.lower(),
            "context": "根据本地交接文档实施，并在独立测试 Vault 验收。", "verification": {"result": "not_run", "evidence_ids": [], "summary": "待测试。"},
            "evidence": [], "event_ids": [], "lessons": [], "related_entries": []}


def script_milestone(suite):
    milestone = draft("engineering-journal", "MS-0001", "milestone", "脚本与独立测试库验收",
                      "本次检查脚本、发布回执、Hook 协议和静态双链。客户端与原生自动链路见各自独立报告。")
    milestone.update(status="verified", completed=[
        "Python 测试套件、Schema、项目白名单、结构化回执通过。",
        "静态双链、失败历史、原子更新、重复执行与 Stop 防循环检查通过。"],
        open_issues=[], next_steps=["客户端与原生自动链路验收结果分别查看对应报告。"])
    milestone["evidence"] = [evidence(suite["path"], "SUITE-" + suite["event_id"][:8], "tool_result", "实际完整测试套件")]
    milestone["verification"] = {"result": "passed", "evidence_ids": [milestone["evidence"][0]["id"]],
                                  "summary": "脚本验收通过；Hook 协议注入与自动触发分别记录。"}
    return milestone


def hook(action, payload, reg):
    proc = subprocess.run([sys.executable, str(BASE / "hook.py"), action, "--config", str(reg.path)],
                          input=json.dumps(payload).encode("utf-8"), capture_output=True, check=True)
    return json.loads(proc.stdout)


def main():
    reg = Registry()
    session = "acceptance-" + uuid.uuid4().hex[:12]
    suite = run_test(reg, "engineering-journal", session, "suite", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    if suite["exit_code"] != 0:
        raise RuntimeError("Unit tests failed: " + suite["path"])
    requirement = BASE / "docs/handoff.md"
    formatting = BASE / "docs/note-format.md"
    decision = draft("engineering-journal", "ADR-0001", "decision", "稳定文件名与完整路径双链", "笔记文件名包含稳定 ID 与创建时中文标题；完整路径双链隔离同名项目记录，图谱节点也可直接阅读。")
    decision.update(status="verified", decision="按原架构直接发布 Markdown；文件名使用 entry_id 加创建时标题，后续标题修改保留文件名；双链使用 Vault 相对完整路径。",
                    justification="用户要求保留已确定架构，并先优化笔记可读性及关系图谱。多个项目共享 Vault，Overview、Progress 等同名笔记需要明确的链接目标。",
                    constraints=["第一版不依赖第三方 Obsidian 插件。", "用户笔记不得被自动覆盖。"],
                    alternatives=["仅以笔记 basename 链接：多个 Overview 同名时容易产生歧义。", "只用 ID 文件名及 aliases：实际 Obsidian 图谱仍显示 ID，阅读不够直观。"],
                    tradeoffs=["文件名保留创建时标题；后续中文标题更新体现在 H1 和 aliases，文件名保持稳定。", "自动写入由发布器管理；人工修改受管笔记后将暂停覆盖，保留用户内容。"])
    decision["evidence"] = [evidence(requirement, "REQ"), evidence(formatting, "FORMAT"), evidence(suite["path"], "TEST-" + suite["event_id"][:8], "tool_result", "实际运行验收测试套件，退出码 0。")]
    decision["event_ids"] = [suite["event_id"]]
    decision["verification"] = {"result": "passed", "evidence_ids": [decision["evidence"][-1]["id"]], "summary": "测试覆盖双链目标存在、幂等重放、路径隔离、并发写入和故障恢复。Obsidian 图谱另外使用客户端验收。"}
    decision["lessons"] = [{"text": "多项目笔记使用稳定身份与完整路径双链，标题变化不会改变记录身份。", "conditions": "适用于同一 Obsidian Vault 中存在同名项目索引的场景。", "reusable": True}]
    receipts = [publish(decision, reg)]
    demo = BASE / "demo-project"
    source = demo / "expectation_check.py"
    source.write_text("assert 1 + 1 == 3, 'acceptance: deliberately incorrect expectation'\n", encoding="utf-8")
    failures = [run_test(reg, "demo-project", session, "debug", [sys.executable, str(source)]) for _ in range(2)]
    if any(x["exit_code"] == 0 for x in failures):
        raise RuntimeError("Expected the deliberate acceptance failure")
    symptom = draft("demo-project", "PIT-0001", "pitfall", "验收示例：断言期望值错误", "两次实际运行均断言失败；先保存未解决状态，再修正期望值并重新验证。此例用于测试记录链路。")
    symptom.update(status="unresolved", symptom="AssertionError: acceptance: deliberately incorrect expectation", environment="Windows / Python 3.13；人为设置的验收用例。", root_cause=None, root_cause_status="unknown", fix=None)
    symptom["attempts"] = [{"action": f"第 {i} 次运行相同断言", "outcome": "退出码非零，错误仍然存在。", "evidence_ids": ["FAIL-" + e["event_id"][:8]]} for i, e in enumerate(failures, 1)]
    symptom["evidence"] = [evidence(e["path"], "FAIL-" + e["event_id"][:8], "tool_result", "真实采集的失败输出与退出码。") for e in failures]
    symptom["event_ids"] = [e["event_id"] for e in failures]
    symptom["verification"] = {"result": "failed", "evidence_ids": [symptom["evidence"][-1]["id"]], "summary": "重复运行未能解决问题。"}
    receipts.append(publish(symptom, reg))
    source.write_text("assert 1 + 1 == 2\nprint('acceptance: expectation corrected; test passed')\n", encoding="utf-8")
    success = run_test(reg, "demo-project", session, "debug", [sys.executable, str(source)])
    symptom.update(status="verified", root_cause="用例把 1 + 1 的期望值错误设为 3；失败输出直接指出该断言。", root_cause_status="established", fix="将期望值修正为 2。")
    symptom["evidence"] = [evidence(success["path"], "PASS-" + success["event_id"][:8], "tool_result", "修复后实际执行成功，退出码 0。")]
    symptom["verification"] = {"result": "passed", "evidence_ids": [symptom["evidence"][-1]["id"]], "summary": "修正期望值后重新运行成功；失败尝试保留在同一问题记录及历史修订。"}
    symptom["event_ids"] = [success["event_id"]]
    symptom["lessons"] = [{"text": "重复执行同一失败测试不能作为修复证据；修复后需产生新的验证回执。", "conditions": "适用于构建、测试或发布步骤的故障排查。", "reusable": True}]
    receipts.append(publish(symptom, reg))
    payload = {"cwd": str(demo), "session_id": session, "turn_id": "stop-wire", "tool_name": "Bash", "tool_use_id": "wire-1",
               "tool_input": {"command": "python -m unittest"}, "tool_response": {"exit_code": 0, "output": "OK (hook protocol acceptance fixture)"}}
    capture = hook("capture", payload, reg)
    event = json.loads(capture["hookSpecificOutput"]["additionalContext"])
    first_stop = hook("stop", payload, reg)
    second_stop = hook("stop", payload, reg)
    active_stop = hook("stop", {**payload, "stop_hook_active": True}, reg)
    assert first_stop.get("decision") == "block"
    assert second_stop.get("decision") != "block" and active_stop.get("decision") != "block"
    # The wire fixture is not evidence that Codex's runtime dispatched a Hook.
    from journal_core import lock
    from event_store import acknowledge
    with lock(reg.base):
        acknowledge(reg, "demo-project", event["journal_event"], {"reason": "Synthetic hook protocol fixture; not a product milestone", "dismissed_at": now()})
    milestone = script_milestone(suite)
    milestone["related_entries"] = [{"project_id": "engineering-journal", "entry_id": "ADR-0001"}, {"project_id": "demo-project", "entry_id": "PIT-0001"}]
    receipts.append(publish(milestone, reg))
    audit = graph_audit(reg.vault)
    assert all(r["status"] in ("published", "unchanged") for r in receipts), receipts
    assert not audit["unresolved"], audit
    report = {"timestamp": now(), "suite": suite, "receipts": receipts, "graph": audit,
              "hook_protocol": {"capture": True, "first_stop": first_stop, "second_stop": second_stop, "active_stop": active_stop},
              "native_codex_hook_e2e": "not_run_by_this_command", "obsidian_visual": "not_run_by_this_command"}
    put_json(BASE / "docs/acceptance-runtime.json", report)
    print(json.dumps({"status": "passed", "notes": audit["notes"], "edges": audit["edges"], "unresolved": audit["unresolved"], "report": str(BASE / "docs/acceptance-runtime.json")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
