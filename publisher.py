"""Validate drafts; publish only managed Markdown; keep versioned receipts."""
from __future__ import annotations
import argparse
import copy
import html
import json
import re
import sqlite3
from pathlib import Path
import sys
from journal_core import (Registry, JournalError, atomic, digest, dumps, guarded, lock,
                          now, put_json, read_json, redact, safe_id, schema_validate,
                          timestamp, under, wiki, publication_receipt, published_entries)
from schema import ENTRY_SCHEMA
from event_store import acknowledge

FOLDERS = {"decision": "Decisions", "pitfall": "Pitfalls", "milestone": "Milestones"}
STATUS = {"verified": "已验证", "provisional": "待验证", "unresolved": "未解决"}
TEST = {"passed": "通过", "failed": "失败", "not_run": "未运行", "not_applicable": "不适用"}


def clean(text):
    return html.escape(str(text), quote=False).replace("[[", r"\[\[").replace("]]", r"\]\]")


def bullets(items):
    return "\n".join("- " + clean(x) for x in items) or "尚未记录。"


def filename_label(text, limit=42):
    return re.sub(r'[\\/:*?"<>|\[\]#^\r\n]', " ", text).strip().rstrip(". ")[:limit] or "记录"


def project_note(reg, pid, kind):
    p = guarded(reg.base, f"state/project-names/{pid}.json")
    if p.exists():
        names = read_json(p)
    else:
        label = filename_label(reg.project(pid).get("name", pid))
        names = {x: x + " " + label for x in ("Overview", "Progress")}
        put_json(p, names)
    return reg.project(pid)["output"] + "/" + names[kind]


def entry_path(reg, entry):
    project = reg.project(entry["project_id"])
    return f'{project["output"]}/{FOLDERS[entry["type"]]}/{entry.get("note_name", safe_id(entry["entry_id"]))}.md'


def lesson_path(lesson):
    return "Engineering/Reusable-Knowledge/" + filename_label(lesson["text"], 16) + "… -- " + digest([lesson["text"], lesson["conditions"]])[:10] + ".md"


def validate(entry, reg, historical=None, *, schema_checked=False):
    if not schema_checked:
        schema_validate(entry, ENTRY_SCHEMA)
    project = reg.project(entry["project_id"])
    safe_id(entry["entry_id"])
    if "\n" in entry["title"] or any(c in entry["title"] for c in "[]|#"):
        raise JournalError("Title contains link/control characters")
    for field in ("created", "updated"):
        if field in entry:
            timestamp(entry[field])
    if entry["type"] == "decision" and (not entry.get("decision") or not entry.get("justification")):
        raise JournalError("Decision and contemporaneous justification required")
    if entry["type"] == "pitfall" and not entry.get("symptom"):
        raise JournalError("Pitfall symptom required")
    if entry.get("root_cause_status") == "established" and not entry.get("root_cause"):
        raise JournalError("Established cause needs a description and evidence")
    if entry["status"] == "verified" and entry["verification"]["result"] not in ("passed", "not_applicable"):
        raise JournalError("Unrun/failed tests cannot be marked verified")
    if entry["type"] == "pitfall" and entry["status"] == "verified" and entry["verification"]["result"] != "passed":
        raise JournalError("A verified fix requires a passing test")
    evidence = {}
    issues = []
    for item in entry["evidence"]:
        if item["id"] in evidence:
            raise JournalError("Duplicate evidence id")
        timestamp(item["observed_at"])
        if historical and digest(item) in historical:
            # Prior accepted observation remains historical evidence. Current tests must
            # explicitly cite fresh collector events; newly submitted paths are rechecked.
            evidence[item["id"]] = (item, None)
            continue
        p = Path(item["path"])
        if not p.is_absolute():
            p = Path(project["root"]) / p
        resolved = p.resolve()
        allowed = [Path(project["root"]), reg.base / "events" / entry["project_id"], reg.base / "docs"]
        if not any(under(resolved, root) for root in allowed):
            raise JournalError("Evidence outside registered project/events/docs")
        if under(resolved, project["root"]) and not under(resolved, reg.base / "docs") and not under(resolved, reg.base / "events" / entry["project_id"]) and reg.match(resolved) != entry["project_id"]:
            raise JournalError("Evidence belongs to a different nested project")
        # Never read auth files, secrets or private keys as evidence.
        if any(part.lower() in {".env", "auth.json", ".ssh", ".sandbox-secrets"} or re.search(r'(?i)(^\.env\.|credentials|secrets|^id_(rsa|ed25519)$)', part) for part in p.parts) or p.suffix.lower() in {".pem", ".key", ".pfx"}:
            raise JournalError("Sensitive evidence path")
        if not p.is_file() or p.stat().st_size > 10_000_000:
            issues.append("证据不存在或超过大小限制：" + item["id"])
            continue
        if digest(p.read_bytes()) != item["sha256"]:
            issues.append("证据 hash 不匹配：" + item["id"])
            continue
        value = None
        if item["kind"] == "tool_result":
            if not under(p, reg.base / "events" / entry["project_id"]):
                raise JournalError("Tool evidence must come from local collector")
            value = read_json(p)
            if value.get("project_id") != entry["project_id"] or value.get("kind") != "tool_result":
                raise JournalError("Tool evidence identity mismatch")
        evidence[item["id"]] = (item, value)
    references = entry["verification"]["evidence_ids"] + [ref for a in entry.get("attempts", []) for ref in a["evidence_ids"]]
    if entry.get("explanation") is not None:
        explanation = entry["explanation"]
        if not explanation["project_goal"].strip() or not explanation["change_role"].strip() or not explanation["flow"]:
            raise JournalError("Explanation needs a project goal, change role and execution flow")
        refs = explanation["evidence_ids"]
        references += refs
        if not refs or not any(x in evidence and evidence[x][0]["kind"] == "source" for x in refs):
            issues.append("项目讲解缺少代码或文档来源证据")
    if any(ref not in evidence for ref in references):
        issues.append("验证或尝试引用了无效证据")
    if entry["status"] == "verified" and not evidence:
        issues.append("已验证记录缺少证据")
    if entry["verification"]["result"] == "passed":
        tests = [evidence[x][1] for x in entry["verification"]["evidence_ids"] if x in evidence]
        if not any(x and type(x.get("exit_code")) is int and x['exit_code'] == 0
                   and not x.get('timed_out') and x.get("classification") == "test_result" for x in tests):
            issues.append("通过状态缺少采集器记录的成功测试结果")
    if entry.get("root_cause_status") == "established" and not evidence:
        issues.append("已确认原因缺少证据")
    for eid in entry["event_ids"]:
        if not (reg.base / "events" / entry["project_id"] / (eid + ".json")).is_file():
            issues.append("关联事件不存在：" + eid)
    return issues


def render_entry(reg, e, issues=None):
    project = reg.project(e["project_id"])
    status = "provisional" if issues else e["status"]
    front = {"type": e["type"], "project": e["project_id"], "status": status, "entry_id": e["entry_id"],
             "created": e["created"], "updated": e["updated"], "aliases": [e["title"]],
             "tags": ["engineering/" + e["type"], "project/" + e["project_id"]], "managed_by": "engineering-journal"}
    lines = ["---", *[k + ": " + json.dumps(v, ensure_ascii=False) for k, v in front.items()], "---", "",
             "# " + clean(e["title"]), "", wiki(project_note(reg, e["project_id"], "Overview"), "项目主页") + " · " + wiki(project_note(reg, e["project_id"], "Progress"), "项目进度"), "",
             "> [!summary] 结论", *["> " + clean(x) for x in e["summary"].splitlines()], "",
             f'**事实状态：{STATUS[status]}** · **验证：{TEST[e["verification"]["result"]] if not issues else "草稿声明待复核"}**', "",
             "## 背景与目标", "", clean(e["context"]), ""]
    if issues:
        lines += ["> [!warning] 待复核", *["> " + clean(x) for x in issues], ""]
    if e.get("explanation"):
        lines += explanation_lines(e["explanation"])
    if e["type"] == "decision":
        for title, value in [("约束", bullets(e.get("constraints", []))), ("决定", clean(e["decision"])),
                             ("理由与依据", clean(e["justification"])), ("实际考虑的替代方案", bullets(e.get("alternatives", []))),
                             ("取舍与局限", bullets(e.get("tradeoffs", [])))]:
            lines += ["## " + title, "", value, ""]
    elif e["type"] == "pitfall":
        lines += ["## 现象与环境", "", clean(e["symptom"]), "", clean(e.get("environment", "未记录环境。")), "", "## 排查与失败尝试", ""]
        for i, a in enumerate(e.get("attempts", []), 1):
            lines += [f'### {i}. {clean(a["action"])}', "", clean(a["outcome"]), "", "证据：" + ", ".join(clean(x) for x in a["evidence_ids"]), ""]
        cause = {"established": "已确认", "hypothesis": "假设", "unknown": "未知"}[e.get("root_cause_status", "unknown")]
        lines += ["## 原因（" + cause + "）", "", clean(e.get("root_cause") or "尚未确定。"), "", "## 修复", "", clean(e.get("fix") or "尚无已确认修复。"), ""]
    else:
        for title, field in [("已完成", "completed"), ("开放问题", "open_issues"), ("下一步验证", "next_steps")]:
            lines += ["## " + title, "", bullets(e.get(field, [])), ""]
    lines += ["## 验证", "", clean(e["verification"]["summary"]), "", "引用：" + ", ".join(clean(x) for x in e["verification"]["evidence_ids"]), "", "## 证据", ""]
    for item in e["evidence"]:
        # Absolute paths are local evidence references, not new graph nodes.
        lines += [f'- **{clean(item["id"])}**：{clean(item["summary"])}',
                  "  - 路径：`" + clean(item["path"]).replace("`", "") + "`",
                  "  - SHA-256：`" + item["sha256"] + "`；采集时间：" + item["observed_at"]]
        if item.get("commit"):
            lines += ["  - Git commit：`" + item["commit"] + "`"]
    lines += ["", "## 可复用经验", ""]
    for lesson in e["lessons"]:
        link = wiki(lesson_path(lesson), "通用经验") + " — " if lesson["reusable"] and not issues else ""
        lines += ["- " + link + clean(lesson["text"]), "  - 适用条件：" + clean(lesson["conditions"])]
    lines += ["", "## 关联记录", ""]
    for related in e["related_entries"]:
        p = reg.base / "state" / "entries" / related["project_id"] / (related["entry_id"] + ".json")
        if p.exists():
            r = read_json(p)
            lines += ["- " + wiki(entry_path(reg, r), clean(r["title"]))]
    return "\n".join(lines).rstrip() + "\n"


def explanation_lines(explanation):
    lines = ["## 项目讲解", "", "### 项目在解决什么问题", "", clean(explanation["project_goal"]), "",
             "### 这一步在项目中的作用", "", clean(explanation["change_role"]), "", "### 执行流程", ""]
    lines += [str(i) + ". " + clean(step) for i,step in enumerate(explanation["flow"],1)]
    if explanation.get("terms"):
        lines += ["", "### 术语解释", ""]
        lines += ["- **" + clean(x["term"]) + "**：" + clean(x["meaning"]) for x in explanation["terms"]]
    if explanation.get("example"):
        lines += ["", "### 具体例子", "", clean(explanation["example"])]
    return lines + ["", "讲解依据：" + ", ".join(clean(x) for x in explanation["evidence_ids"]), ""]


def managed_write(reg, rel, content, manifest):
    target = guarded(reg.vault, rel)
    expected = digest(content.encode("utf-8"))
    ownership = manifest.get(rel)
    if target.exists():
        current = digest(target.read_bytes())
        if not ownership or current not in {ownership.get("hash"), ownership.get("pending")}:
            raise JournalError("Unmanaged or manually modified note; refusing overwrite: " + rel)
        if current == expected:
            manifest[rel] = {"hash": expected}
            return
        backup = guarded(reg.base, "state/history/" + digest(rel)[:20] + "/" + current + ".md")
        if not backup.exists():
            atomic(backup, target.read_text(encoding="utf-8"))
    manifest[rel] = {"hash": ownership.get("hash") if ownership else None, "pending": expected}
    put_json(guarded(reg.base, "state/owned.json"), manifest)
    atomic(target, content)
    manifest[rel] = {"hash": expected}
    put_json(guarded(reg.base, "state/owned.json"), manifest)


def rebuild(reg, manifest, candidate=None, project_id=None):
    """Update affected views; a no-argument rebuild remains a full repair."""
    entries = list(published_entries(reg))
    affected_shared = {lesson_path(lesson) for e in entries if e['project_id'] == project_id
                       for lesson in e['lessons'] if lesson['reusable']}
    if candidate:
        affected_shared.update(lesson_path(x) for x in candidate['lessons'] if x['reusable'])
        entries = [e for e in entries if (e['project_id'], e['entry_id']) !=
                   (candidate['project_id'], candidate['entry_id'])] + [candidate]
    active_paths = set()
    def view_write(rel, content):
        # Unchanged shared/foreign views need no I/O or ownership check.
        if project_id is None or manifest.get(rel, {}).get('hash') != digest(content.encode('utf-8')):
            managed_write(reg, rel, content, manifest)
    for entry in entries:
        rel = entry_path(reg, entry)
        if project_id is None:
            managed_write(reg, rel, render_entry(reg, entry), manifest)
        active_paths.add(rel)
    index = ["# 工程经验库", "", "从项目进入，沿双链查看决定、问题和经验。", "", "## 项目", ""]
    reusable = {}
    for pid, project in reg.projects.items():
        own = sorted([e for e in entries if e["project_id"] == pid], key=lambda e: e["updated"], reverse=True)
        overview = ["# " + clean(project.get("name", pid)), "", wiki("Engineering/Index", "经验库首页") + " · " + wiki(project_note(reg, pid, "Progress"), "阶段进度"), "", "## 工程记录", ""]
        progress = ["# " + clean(project.get("name", pid)) + " — 进度", "", wiki(project_note(reg, pid, "Overview"), "项目主页"), ""]
        explained = [e for e in own if e.get("explanation")]
        if explained:
            guide = project["output"] + "/Project-Guide 项目讲解.md"
            lines = ["# " + clean(project.get("name",pid)) + " — 项目讲解", "",
                     wiki(project_note(reg,pid,"Overview"),"项目主页"), "",
                     "从当前记录理解项目，再沿阅读路径查看各步骤的原因和验证。", ""]
            lines += explanation_lines(explained[0]["explanation"])
            lines += ["## 当前讲解来源", "", wiki(entry_path(reg,explained[0]),clean(explained[0]["title"])), "",
                      "## 阅读路径", ""]
            lines += ["- " + wiki(entry_path(reg,e),clean(e["title"])) for e in sorted(explained,key=lambda e:(e["created"],e["entry_id"]))]
            if project_id in (None, pid):
                view_write(guide,"\n".join(lines) + "\n")
            active_paths.add(guide)
            overview += [wiki(guide,"项目讲解与阅读路径"), ""]
        for typ, title in [("milestone", "阶段里程碑"), ("decision", "技术决策"), ("pitfall", "问题与修复")]:
            progress += ["## " + title, ""]
            for e in own:
                if e["type"] == typ:
                    link = wiki(entry_path(reg, e), clean(e["title"]))
                    overview += [f'- {link} · {STATUS[e["status"]]}']
                    progress += [f'- {link} · {STATUS[e["status"]]} — {clean(e["summary"])}']
                    if typ == "milestone":
                        progress += ["", "开放问题：", "", bullets(e.get("open_issues", [])), "", "下一步：", "", bullets(e.get("next_steps", [])), ""]
            progress += [""]
        for e in own:
            for lesson in e["lessons"]:
                if lesson["reusable"]:
                    rel = lesson_path(lesson)
                    record = reusable.setdefault(rel, {"lesson": lesson, "sources": []})
                    record["sources"].append(wiki(entry_path(reg, e), clean(e["title"])))
        for filename, lines in [("Overview", overview), ("Progress", progress)]:
            rel = project_note(reg, pid, filename) + ".md"
            if project_id in (None, pid) or rel not in manifest:
                view_write(rel, "\n".join(lines) + "\n")
            active_paths.add(rel)
        index += ["- " + wiki(project_note(reg, pid, "Overview"), clean(project.get("name", pid)))]
    index += ["", "## 通用经验", ""]
    for rel, record in sorted(reusable.items()):
        lesson = record["lesson"]
        content = "---\ntype: lesson\ntags: [engineering/lesson]\nmanaged_by: engineering-journal\n---\n\n# " + clean(lesson["text"][:70]) + "\n\n" + clean(lesson["text"]) + "\n\n## 适用条件\n\n" + clean(lesson["conditions"]) + "\n\n## 来源记录\n\n" + "\n".join("- " + x for x in record["sources"]) + "\n"
        view_write(rel, content)
        active_paths.add(rel)
        index += ["- " + wiki(rel, clean(lesson["text"][:60]))]
    view_write("Engineering/Index.md", "\n".join(index) + "\n")
    # Retire only obsolete owned generated names, after all new links exist.
    for rel in list(manifest):
        in_scope = project_id is None or rel.startswith(reg.project(project_id)['output'] + '/') or rel in affected_shared
        if in_scope and (rel.startswith("Engineering/Projects/") or rel.startswith("Engineering/Reusable-Knowledge/")) and rel not in active_paths:
            target = guarded(reg.vault, rel)
            if target.exists():
                current = digest(target.read_bytes())
                if current not in {manifest[rel].get('hash'), manifest[rel].get('pending')}:
                    raise JournalError("Cannot migrate manually modified note: " + rel)
                archived = guarded(reg.base, "state/history/" + digest(rel)[:20] + "/" + current + ".md")
                atomic(archived, target.read_text(encoding="utf-8"))
                target.unlink()
            del manifest[rel]
    put_json(guarded(reg.base, "state/owned.json"), manifest)


def publish(draft, reg):
    entry = copy.deepcopy(draft)
    # Redaction before persisting even rejected drafts.
    entry = redact(entry)
    schema_validate(entry, ENTRY_SCHEMA)
    pid, eid = entry["project_id"], safe_id(entry["entry_id"])
    reg.project(pid)
    draft_hash = digest(entry)
    with lock(reg.base):
        saved = guarded(reg.base, f"outbox/{pid}/{eid}-{draft_hash[:12]}.json")
        put_json(saved, entry)
        try:
            historical = set()
            manifest_path = guarded(reg.base, "state/owned.json")
            manifest = read_json(manifest_path) if manifest_path.exists() else {}
            records = guarded(reg.base, f"state/entries/{pid}")
            record_path = records / (eid + ".json")
            for p in records.glob("*.json"):
                old = read_json(p)
                if old["fingerprint"] == entry["fingerprint"] and old["entry_id"] != eid:
                    raise JournalError("Existing fingerprint; update entry " + old["entry_id"])
            old = read_json(record_path) if record_path.exists() else None
            accepted = publication_receipt(reg, old) if old else None
            if accepted and 'record_hash' not in accepted:
                accepted['record_hash'] = digest(old)
                put_json(guarded(reg.base, f'state/receipts/{pid}/{eid}.json'), accepted)
            if old:
                if old["entry_id"] != eid or old["type"] != entry["type"] or old["fingerprint"] != entry["fingerprint"]:
                    raise JournalError("Stable identity/type/fingerprint cannot change")
                new_evidence_ids = {x["id"] for x in entry["evidence"]}
                historical = {digest(x) for x in old["evidence"] if x["id"] not in new_evidence_ids} if accepted else set()
                for field in ("attempts", "evidence", "event_ids"):
                    if field not in old and field not in entry:
                        continue
                    combined = old.get(field, []) + entry.get(field, [])
                    if field == "evidence":
                        by_id = {}
                        for item in combined:
                            if item["id"] in by_id:
                                previous = by_id[item["id"]]
                                if any(previous[k] != item[k] for k in ("path", "sha256", "kind")):
                                    raise JournalError("Evidence id changed revision; use a new evidence id: " + item["id"])
                            else:
                                by_id[item["id"]] = item
                        entry[field] = list(by_id.values())
                    else:
                        entry[field] = list({digest(x): x for x in combined}.values())
                # Preserve original evidence; a successful update cannot erase failures.
                if "explanation" not in entry and "explanation" in old:
                    entry["explanation"] = old["explanation"]
            issues = validate(entry, reg, historical, schema_checked=old is None)
            if reg.project(pid).get("note_mode","concise") == "explanatory" and not draft.get("explanation"):
                issues.append("解释模式需要本次草稿提供有来源的项目讲解")
            entry["created"] = old["created"] if old else entry.get("created", now())
            entry["note_name"] = old.get("note_name") if old and old.get("note_name") else eid + " " + filename_label(entry["title"])
            same = old and {k: v for k, v in old.items() if k not in ("created", "updated")} == {k: v for k, v in entry.items() if k not in ("created", "updated")}
            entry["updated"] = old["updated"] if same else now()
            for related in entry["related_entries"]:
                reg.project(related["project_id"])
                safe_id(related["entry_id"])
                related_path = guarded(reg.base, f'state/entries/{related["project_id"]}/{related["entry_id"]}.json')
                if not related_path.exists() or not publication_receipt(reg, read_json(related_path)):
                    issues.append("关联记录尚未发布：" + related["entry_id"])
            if issues:
                rel = f"Engineering/Needs-Review/{pid}-{eid}.md"
                managed_write(reg, rel, render_entry(reg, entry, issues), manifest)
                rebuild(reg, manifest, project_id=pid)
                receipt = {"entry_id": eid, "project_id": pid, "status": "needs_review", "path": str(reg.vault / rel), "issues": issues, "written_at": now(), "draft_hash": draft_hash}
                if accepted:
                    receipt['record_hash'] = digest(old)
            else:
                rel = entry_path(reg, entry)
                managed_write(reg, rel, render_entry(reg, entry), manifest)
                if old and not same:
                    put_json(guarded(reg.base, f"state/record-history/{pid}/{eid}/{digest(old)}.json"), old)
                review_rel = f"Engineering/Needs-Review/{pid}-{eid}.md"
                if review_rel in manifest and (reg.vault / review_rel).exists():
                    managed_write(reg, review_rel, '---\ntype: review\nstatus: resolved\nmanaged_by: engineering-journal\n---\n\n# 复核已完成\n\n当前记录：' + wiki(rel, clean(entry["title"])) + '\n', manifest)
                rebuild(reg, manifest, candidate=entry, project_id=pid)
                put_json(record_path, entry)
                receipt = {"entry_id": eid, "project_id": pid, "status": "unchanged" if same else "published", "path": str(reg.vault / rel), "written_at": now(), "draft_hash": draft_hash,
                           "content_hash": digest((reg.vault / rel).read_bytes()), "event_ids": entry["event_ids"], 'record_hash': digest(entry)}
            if not issues:
                from knowledge import sync_index
                try:
                    indexed = [e for e in published_entries(reg) if
                               (e['project_id'], e['entry_id']) != (pid, eid)] + [entry]
                    receipt['index'] = dict(status='ready', **sync_index(reg, indexed))
                except (sqlite3.DatabaseError, OSError) as exc:
                    receipt['index'] = {'status': 'stale', 'repair': 'journal.py index', 'reason': redact(str(exc))}
            # One completion write. A cache row prepared above is invisible to
            # recall until this exact record hash has its durable receipt.
            put_json(guarded(reg.base, f"state/receipts/{pid}/{eid}.json"), receipt)
            if not issues:
                for event in entry["event_ids"]:
                    acknowledge(reg, pid, event, {"receipt": f"{pid}/{eid}", "content_hash": receipt["content_hash"]})
            return receipt
        except Exception as exc:
            put_json(saved.with_suffix(".rejected.json"), {"status": "rejected", "reason": redact(str(exc)), "timestamp": now()})
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("draft")
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        reg = Registry(args.config)
        source = Path(args.draft).resolve()
        raw = read_json(source)
        sanitized = redact(raw)
        if raw != sanitized and under(source, reg.base / "outbox"):
            # Our own draft cache must not retain the original secret-bearing input.
            safe_source = guarded(reg.base, source.relative_to(reg.base))
            put_json(safe_source, sanitized)
        result = publish(sanitized, reg)
        print(json.dumps(result, ensure_ascii=True))
        return 2 if result["status"] == "needs_review" else 0
    except Exception as exc:
        print(json.dumps({"status": "rejected", "reason": redact(str(exc))}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
