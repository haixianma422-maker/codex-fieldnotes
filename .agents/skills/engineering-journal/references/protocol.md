# Draft protocol

Required fields: `schema_version: 1`, registered `project_id`, stable `entry_id`,
`type: decision|pitfall|milestone`, short `title`, outcome-first `summary`,
`status: verified|provisional|unresolved`, stable `fingerprint`, `context`,
`verification`, `evidence`, `event_ids`, `lessons`, `related_entries`.

`verification` contains `result: passed|failed|not_run|not_applicable`,
`evidence_ids: []`, and `summary`. Passing requires a referenced local collector
event with `exit_code: 0` and `classification: test_result`. This establishes
execution success, not the semantic correctness or coverage of a test.

Each evidence object contains `id`, `path`, current `sha256`, `kind:
source|artifact|tool_result`, timezone-aware `observed_at`, and `summary`.
Optional `commit` is a Git hash. Paths must belong to the registered project,
its collector event directory, or the system docs directory. Secret files are forbidden.
Missing/changed evidence goes to Needs-Review. Unknown fields fail schema validation.
Use a new evidence id for a changed file revision. Replaying the same id/path/hash
preserves the original observation time. Prior accepted observations stay in history;
new evidence and tests are checked again. `note_name` is assigned by the publisher
and frozen after creation so later title changes do not rename graph nodes.

Decisions additionally require `decision` and `justification`; optional lists:
`constraints`, `alternatives`, `tradeoffs`. Do not add imagined alternatives.
Pitfalls require `symptom`; optional `environment`, `attempts` (each has `action`,
`outcome`, `evidence_ids`), `root_cause`, `root_cause_status`, `fix`.
Milestones use lists `completed`, `open_issues`, `next_steps`.

Lessons: `{ "text": "...", "conditions": "...", "reusable": true }`.
Related entries: `{ "project_id": "...", "entry_id": "..." }`.
Event IDs refer to local captured events and are acknowledged only after publishing.
Do not claim full automation until Hooks are trusted and an actual Codex turn has
demonstrated capture, continuation and a checked publication receipt.

When updating an entry, submit only new/current evidence in the draft. The publisher merges
prior accepted evidence automatically. Do not copy a previous entry's evidence array into
a fresh draft: explicitly resubmitted evidence is checked against current file hashes.
Use a new evidence id for each changed revision and fresh test; keep the original
observation in publisher history. Use the publisher's default absolute registry path.

Optional `explanation` contains `project_goal`, `change_role`, `flow: [string]`,
`evidence_ids: [id]`, and optional `terms: [{term, meaning}]` and `example`.
The goal, role and flow must be nonempty. Reference code or documentation evidence;
test success alone does not establish a project explanation. In project mode
`explanatory`, each new/update draft must provide a current explanation or goes
to Needs-Review. In `concise` mode it is optional; prior accepted explanations
remain in history/notes when a later draft omits them. The generated project guide
uses the most recently updated explanation and links the other explanatory records.
Explain uncertain or limited coverage in the text. The publisher validates structure
and references, not the semantic accuracy of a tutorial.
