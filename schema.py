"""Single source for runtime validation and exported JSON Schema."""
S = {"type": "string", "maxLength": 12000}
SHORT = {"type": "string", "minLength": 1, "maxLength": 200}
IDENT = {"type": "string", "pattern": "[A-Za-z0-9][A-Za-z0-9_-]{0,79}"}


def arr(item=S, limit=50):
    return {"type": "array", "items": item, "maxItems": limit}


def obj(props, required):
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


ENTRY_SCHEMA = obj({
    "schema_version": {"type": "integer", "const": 1},
    "project_id": IDENT, "entry_id": IDENT,
    "type": {"type": "string", "enum": ["decision", "pitfall", "milestone"]},
    "title": SHORT, "summary": S,
    "status": {"type": "string", "enum": ["verified", "provisional", "unresolved"]},
    "fingerprint": SHORT, "created": SHORT, "updated": SHORT,
    "note_name": {"type": "string", "maxLength": 160},
    "context": S, "constraints": arr(), "decision": S, "justification": {"type": ["string", "null"], "maxLength": 12000},
    "alternatives": arr(), "tradeoffs": arr(), "symptom": S, "environment": S,
    "attempts": arr(obj({"action": S, "outcome": S, "evidence_ids": arr(SHORT)}, ["action", "outcome", "evidence_ids"])),
    "root_cause": {"type": ["string", "null"], "maxLength": 12000},
    "root_cause_status": {"type": "string", "enum": ["established", "hypothesis", "unknown"]},
    "fix": {"type": ["string", "null"], "maxLength": 12000},
    "verification": obj({"result": {"type": "string", "enum": ["passed", "failed", "not_run", "not_applicable"]},
                         "evidence_ids": arr(SHORT), "summary": S}, ["result", "evidence_ids", "summary"]),
    "evidence": arr(obj({"id": SHORT, "path": {"type": "string", "maxLength": 500},
                         "sha256": {"type": "string", "pattern": "[a-f0-9]{64}"},
                         "kind": {"type": "string", "enum": ["source", "artifact", "tool_result"]},
                         "observed_at": SHORT, "summary": S,
                         "commit": {"type": "string", "pattern": "[a-f0-9]{7,40}"}},
                        ["id", "path", "sha256", "kind", "observed_at", "summary"])),
    "event_ids": arr({"type": "string", "pattern": "[a-f0-9]{32}"}),
    "lessons": arr(obj({"text": S, "conditions": S, "reusable": {"type": "boolean"}}, ["text", "conditions", "reusable"])),
    "related_entries": arr(obj({"project_id": IDENT, "entry_id": IDENT}, ["project_id", "entry_id"])),
    "completed": arr(), "open_issues": arr(), "next_steps": arr(),
    "explanation": obj({"project_goal": S, "change_role": S, "flow": arr(),
                        "terms": arr(obj({"term": SHORT, "meaning": S}, ["term", "meaning"])),
                        "example": S, "evidence_ids": arr(SHORT)},
                       ["project_goal", "change_role", "flow", "evidence_ids"]),
}, ["schema_version", "project_id", "entry_id", "type", "title", "summary", "status", "fingerprint", "context", "verification", "evidence", "event_ids", "lessons", "related_entries"])
ENTRY_SCHEMA["$schema"] = "https://json-schema.org/draft/2020-12/schema"
