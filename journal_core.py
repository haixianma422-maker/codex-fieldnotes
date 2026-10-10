"""Local Engineering Journal: bounded data, atomic files, process locks. Stdlib only."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SECRET = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{15,}|"
    r"(?:bearer\s+)[a-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|token|password|passwd|secret)\s*[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]{4,}|"
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z))")


class JournalError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    data = value if isinstance(value, bytes) else dumps(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def read_json(path):
    p = Path(path)
    if p.stat().st_size > 2_000_000:
        raise JournalError("JSON exceeds 2 MB limit")
    return json.loads(p.read_text(encoding="utf-8-sig"))


def redact(value):
    if isinstance(value, str):
        return SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(x) for x in value]
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)^(password|token|secret|api_key|authorization)$", k)
                    else redact(v)) for k, v in value.items()}
    return value


def safe_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise JournalError("Invalid identity")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise JournalError("Reserved Windows identity")
    return value


def under(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def guarded(root, relative):
    root = Path(root).absolute()
    rel = Path(relative)
    if rel.is_absolute() or re.match(r"^[A-Za-z]:", str(relative)) or ".." in rel.parts:
        raise JournalError("Absolute/traversal output is forbidden")
    if any(any(c in part for c in ':*?"<>|') or part.endswith((' ', '.')) for part in rel.parts):
        raise JournalError("Invalid Windows output component")
    target = root / rel
    # Reject every link/reparse component, including root ancestors (Windows junctions).
    for p in [target, *target.parents]:
        if p.exists() or p.is_symlink():
            st = p.lstat()
            if p.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                raise JournalError("Symlink/junction output is forbidden")
    if not under(target, root):
        raise JournalError("Path escapes allowlist")
    return target


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".ej-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def put_json(path, value):
    atomic(path, dumps(value) + "\n")


@contextlib.contextmanager
def lock(base, timeout=8):
    p = guarded(base, "state/journal.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() > deadline:
                    raise JournalError("Journal lock timeout; queue retained")
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class Registry:
    def __init__(self, config=None):
        self.path = Path(config or BASE / "projects.json").resolve()
        self.base = self.path.parent
        self.data = read_json(self.path)
        if not Path(self.data["vault"]).is_absolute():
            raise JournalError("Vault must be an absolute path")
        self.vault = Path(self.data["vault"]).resolve()
        self.projects = self.data["projects"]
        for pid, project in self.projects.items():
            safe_id(pid)
            if project.get("note_mode", "concise") not in ("concise", "explanatory"):
                raise JournalError("Invalid project note_mode")
            root = Path(project["root"])
            if not root.is_absolute() or not root.is_dir():
                raise JournalError("Project root must be an existing absolute directory")
            output = project["output"]
            if not output.startswith("Engineering/Projects/") or len(Path(output).parts) != 3:
                raise JournalError("Output must be Engineering/Projects/<project>")
            guarded(self.vault, output)
        roots = [Path(x["root"]).resolve() for x in self.projects.values()]
        outputs = [os.path.normcase(str(guarded(self.vault, x["output"]))) for x in self.projects.values()]
        if len(outputs) != len(set(outputs)) or len(roots) != len(set(roots)) or len(self.projects) != len({x.casefold() for x in self.projects}):
            raise JournalError("Duplicate project roots/outputs")

    def match(self, cwd):
        matches = [(len(Path(p["root"]).parts), pid) for pid, p in self.projects.items() if under(cwd, p["root"])]
        return max(matches)[1] if matches else None

    def project(self, pid):
        if pid not in self.projects:
            raise JournalError("Project not registered")
        return self.projects[pid]


def publication_receipt(reg, entry):
    """Return proof of this accepted revision, including legacy receipts."""
    pid, eid = safe_id(entry['project_id']), safe_id(entry['entry_id'])
    p = guarded(reg.base, f'state/receipts/{pid}/{eid}.json')
    if not p.exists():
        return None
    receipt = read_json(p)
    if receipt.get('project_id') != pid or receipt.get('entry_id') != eid:
        return None
    if receipt.get('status') not in ('published', 'unchanged', 'needs_review'):
        return None
    if 'record_hash' in receipt:
        return receipt if receipt['record_hash'] == digest(entry) else None
    # The old publisher could leave a new record with an older successful
    # receipt. Re-render the record to prove which legacy revision it covers.
    # Never use the editable live note as the canonical accepted revision.
    if receipt['status'] in ('published', 'unchanged'):
        names = guarded(reg.base, f'state/project-names/{pid}.json')
        if names.exists():
            from publisher import render_entry
            if digest(render_entry(reg, entry).encode('utf-8')) == receipt.get('content_hash'):
                return receipt
    return None


def published_entries(reg):
    for p in (reg.base / 'state/entries').glob('*/*.json'):
        entry = read_json(p)
        if entry['project_id'] in reg.projects and publication_receipt(reg, entry):
            yield entry


def schema_validate(value, schema, where="entry"):
    """Validate the documented schema subset; fail closed on unknown object fields."""
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    expected = schema.get("type")
    if expected:
        choices = expected if isinstance(expected, list) else [expected]
        if not any(isinstance(value, types[x]) and not (x == "integer" and isinstance(value, bool)) for x in choices):
            raise JournalError(f"{where}: wrong type")
    if "enum" in schema and value not in schema["enum"]:
        raise JournalError(f"{where}: invalid enum")
    if "const" in schema and value != schema["const"]:
        raise JournalError(f"{where}: invalid version")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 20000):
            raise JournalError(f"{where}: invalid length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise JournalError(f"{where}: invalid pattern")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            raise JournalError(f"{where}: missing required field")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise JournalError(f"{where}: unknown fields")
        for k, v in value.items():
            if k in props:
                schema_validate(v, props[k], where + "." + k)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 100):
            raise JournalError(f"{where}: too many items")
        for i, item in enumerate(value):
            schema_validate(item, schema.get("items", {}), f"{where}[{i}]")


def wiki(path, label=None):
    target = str(path).replace("\\", "/").removesuffix(".md")
    return "[[" + target + ("|" + label if label else "") + "]]"


def timestamp(value):
    try:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError()
    except (ValueError, AttributeError):
        raise JournalError("Timestamp must be ISO 8601 with timezone")
