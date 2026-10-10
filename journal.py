"""Operations for the local journal; no network or AI service dependency."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile
import uuid
from journal_core import Registry, JournalError, digest, guarded, lock, now, put_json, read_json, redact, safe_id, under
from process_runner import spawn
from event_store import pending_events, write_event, acknowledge, rebuild_pending


def graph_audit(vault):
    files = {p.relative_to(vault).as_posix().removesuffix(".md"): p for p in vault.rglob("*.md")}
    missing, edges = [], []
    for source, p in files.items():
        # Exclude inline fenced code; publisher never inserts raw wiki links from evidence.
        text = re.sub(r"```[\s\S]*?```", "", p.read_text(encoding="utf-8-sig"))
        for link in re.findall(r"(?<!\\)\[\[([^\]]+)\]\]", text):
            target = link.split("|")[0].split("#")[0].removesuffix(".md")
            if target not in files:
                missing.append({"source": source, "target": target})
            else:
                edges.append([source, target])
    return {"notes": len(files), "edges": len(edges), "unresolved": missing, "connections": edges}


def run_test(reg, pid, session, turn, command):
    safe_id(session)
    safe_id(turn)
    project = reg.project(pid)
    # Stream a bounded excerpt; never retain raw complete logs on disk.
    process, close_tree = spawn(command, cwd=project["root"])
    excerpt = b""
    import threading
    expired = []
    def expire():
        expired.append(True)
        close_tree()
    timer = threading.Timer(90, expire)
    timer.start()
    try:
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            excerpt = (excerpt + chunk)[-6000:]
        code = process.wait()
    finally:
        timer.cancel()
        timer.join()
        close_tree()
        process.stdout.close()
    if expired:
        code = 124
    event = {"schema_version": 1, "project_id": pid, "session_id": session, "turn_id": turn,
             "kind": "tool_result", "classification": "test_result", "meaningful": True, "candidate": code != 0,
             "tool": "journal-test-runner", "exit_code": code, "timestamp": now(),
             "command": redact(subprocess.list2cmdline(command))[:800],
             "excerpt": redact(excerpt.decode("utf-8", errors="replace"))[:6000], "timed_out": bool(expired)}
    eid = digest([event, uuid.uuid4().hex])[:32]
    event["event_id"] = eid
    p = guarded(reg.base, f"events/{pid}/{eid}.json")
    with lock(reg.base):
        write_event(reg, event)
    return {"event_id": eid, "path": str(p), "sha256": digest(p.read_bytes()), "exit_code": code, "observed_at": event["timestamp"]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    sub.add_parser("pending")
    sub.add_parser("events-index")
    sub.add_parser("audit")
    sub.add_parser("index")
    recall_cmd = sub.add_parser("recall")
    recall_cmd.add_argument("--project", required=True)
    recall_cmd.add_argument("--query", required=True)
    recall_cmd.add_argument("--limit", type=int, default=3)
    recall_cmd.add_argument("--max-chars", type=int, default=2400)
    mode = sub.add_parser("mode")
    mode.add_argument("--project", required=True)
    mode.add_argument("--set", choices=["concise", "explanatory"])
    test = sub.add_parser("run")
    test.add_argument("--project", required=True)
    test.add_argument("--session", required=True)
    test.add_argument("--turn", required=True)
    test.add_argument("command", nargs=argparse.REMAINDER)
    mark = sub.add_parser("mark")
    mark.add_argument("event_id")
    mark.add_argument("--reason", required=True)
    dismiss = sub.add_parser("dismiss")
    dismiss.add_argument("event_id")
    dismiss.add_argument("--reason", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    restore = sub.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("destination")
    register = sub.add_parser("register")
    register.add_argument("project_id")
    register.add_argument("root")
    register.add_argument("--name")
    args = p.parse_args()
    try:
        reg = Registry(args.config)
        if args.action == "doctor":
            result = {"python": sys.version, "executable": sys.executable, "config": str(reg.path), "vault": str(reg.vault),
                      "projects": list(reg.projects), "hook_trust": "Requires user review in Codex /hooks; not inferred from file presence"}
        elif args.action == "pending":
            with lock(reg.base):
                result = pending_events(reg)
        elif args.action == 'events-index':
            with lock(reg.base):
                result = rebuild_pending(reg)
        elif args.action == "audit":
            result = graph_audit(reg.vault)
        elif args.action == "index":
            from knowledge import repair_index
            with lock(reg.base):
                result = repair_index(reg)
        elif args.action == "recall":
            from knowledge import recall
            result = recall(reg, args.project, args.query, args.limit, args.max_chars)
        elif args.action == "mode":
            reg.project(args.project)
            if args.set:
                with lock(reg.base):
                    data = read_json(reg.path)
                    data["projects"][args.project]["note_mode"] = args.set
                    put_json(reg.path, data)
                reg = Registry(reg.path)
            result = {"project_id":args.project, "note_mode":reg.project(args.project).get("note_mode","concise"),
                      "scope":"future drafts; existing notes are preserved"}
        elif args.action == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise JournalError("Test command required")
            result = run_test(reg, args.project, args.session, args.turn, command)
        elif args.action in ("mark", "dismiss"):
            safe_id(args.event_id)
            with lock(reg.base):
                matches = list((reg.base / "events").glob("*/" + args.event_id + ".json"))
                if len(matches) != 1:
                    raise JournalError("Event identity not found")
                if args.action == "mark":
                    event = read_json(matches[0])
                    event["meaningful"] = True
                    event["significance_reason"] = redact(args.reason)[:800]
                    write_event(reg, event)
                else:
                    acknowledge(reg, matches[0].parent.name, args.event_id,
                                {"dismissed_at": now(), "reason": redact(args.reason)[:800]})
            result = {"status": args.action, "event_id": args.event_id}
        elif args.action == "register":
            pid = safe_id(args.project_id)
            if pid in reg.projects:
                raise JournalError("Project already registered")
            root = Path(args.root).resolve(strict=True)
            if not root.is_dir():
                raise JournalError("Project root must be a directory")
            with lock(reg.base):
                data = read_json(reg.path)
                data["projects"][pid] = {"root": str(root), "output": "Engineering/Projects/" + pid, "name": args.name or pid}
                pending = reg.path.with_suffix(".pending.json")
                put_json(pending, data)
                Registry(pending)
                put_json(reg.path, data)
                pending.unlink()
            result = {"registered": pid, "root": str(root)}
        elif args.action == "backup":
            destination = Path(args.destination).resolve()
            if destination.is_relative_to(reg.vault) or destination.exists():
                raise JournalError("Backup must be a new path outside Vault")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with lock(reg.base), zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
                for folder, prefix in [(reg.vault, "vault"), (reg.base / "state", "state"), (reg.base / "events", "events"), (reg.base / "outbox", "outbox")]:
                    for f in folder.rglob("*"):
                        if f.is_file() and f.name != "journal.lock":
                            z.write(f, prefix + "/" + f.relative_to(folder).as_posix())
                z.write(reg.path, "projects.json")
            with zipfile.ZipFile(destination) as z:
                result = {"backup": str(destination), "integrity": z.testzip(), "files": len(z.namelist())}
        elif args.action == "restore":
            destination = Path(args.destination).absolute()
            if destination.exists() or under(destination, reg.vault):
                raise JournalError("Restore destination must be new and outside current Vault")
            with zipfile.ZipFile(args.archive) as z:
                if z.testzip() is not None:
                    raise JournalError("Backup integrity check failed")
                if sum(x.file_size for x in z.infolist()) > 500_000_000:
                    raise JournalError("Backup extraction limit exceeded")
                for member in z.infolist():
                    target = guarded(destination, member.filename)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("xb") as f:
                            f.write(z.read(member))
            # Restored registry intentionally keeps original absolute paths; do not
            # activate it until paths are reviewed. No active config is overwritten.
            result = {"restored_to": str(destination), "active_config_changed": False}
        print(json.dumps(result, ensure_ascii=False) if args.action == "recall" else json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"status": "error", "reason": redact(str(e))}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
