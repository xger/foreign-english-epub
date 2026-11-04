# Continuous generation — scheduler protocol 3.0

After real preview approval and formal planning, start one `generation.run` call.
The deterministic process repeatedly starts a **fresh subprocess** for one worker
request, validates its result, commits a checkpoint and chooses the next task.
The model generates only text. It never selects tasks, edits the plan, retries,
updates checkpoints or supervises its own book-length loop.

## Start once

```json
{
  "api_version": "1.0",
  "operation": "generation.run",
  "project": "/reading/book",
  "parameters": {
    "worker_command": ["/absolute/python", "/absolute/skill/agents/openai_worker.py"],
    "max_calls": 100,
    "max_seconds": 3600,
    "timeout_seconds": 180,
    "max_output_tokens_total": 500000,
    "max_retries": 2,
    "max_split_depth": 6,
    "assemble": true
  }
}
```

Send this to `python scripts/agent_api.py --request request.json`. The dedicated
`scripts/generation_scheduler.py --request request.json` also accepts the same
project/parameters object. All worker executable/script paths must resolve from
a fresh empty working directory. Commands are argument arrays, executed without
a shell. The new run entry point preserves API 1.0 and existing helper commands.

Provider-specific setup stays under agents/. See [the included worker configuration](../agents/openai-worker.md) or implement the neutral stdin/stdout protocol below. Credentials and model choice belong to that adapter's environment, not the core profile or scheduler state.

A fresh subprocess/cwd separates task data and conversation; arbitrary configured executable code is trusted code, not an OS security sandbox. The scheduler invokes workers directly without a shell and does not expose the full source index in the task payload.

## Worker protocol

Input on stdin:

```json
{"worker_protocol":"1.0","task":{"task_id":"...","chunk":{},"config":{},"short_term_memory":{},"consistency_memory":{}},"max_output_tokens":5000}
```

The actual task includes plan and context hashes, owned spans/notes and context
budgets. No complete source index, task ledger or previous full drafts is sent.

Successful stdout:

```json
{"status":"completed","draft":{"plan_sha256":"...","chunk_id":"...","units":[],"footnotes_integrated":[]},"usage":{"output_tokens":1234}}
```

The draft uses the existing complete sentence-guidance schema; empty units are
valid only for empty structural source. Workers may also return overflow,
transient, refused or configuration_error. Raw legacy draft JSON is accepted.
Report provider output usage including invisible/reasoning tokens when known.
Provider status, actual reported usage, serialized draft size and output file
size are checked separately. A complete-looking JSON response with excessive
reported token use is still rejected.

The subprocess has bounded stdout/stderr size and a timeout. Errors record
classification codes, not credential-bearing provider error bodies. For an
adapter with large fixed instructions, include that overhead in the declared
profile overhead reserve. The supplied adapter's fixed prompt fits the default
reserve; the runner additionally checks serialized input plus its full output cap.

## Failure transitions and local replanning

Normal: pending → running → validating → checkpointed.

- Timeout, transient provider failure, worker exit, malformed or invalid output:
  failed → retry_wait, up to max_retries additional attempts for that leaf.
- Explicit truncation, output-size/token overflow or excessive context:
  failed → replanned directly; a larger task is not repeatedly retried unchanged.
- Retry exhaustion: attempt a local split.
- Split at existing span boundaries, then sentence or clause punctuation. Preserve
  exact source offsets, note-call ownership and original order. Do not cut words
  or discard source. An indivisible fragment/note or split-depth limit becomes
  blocked.
- Configuration errors and refusals become blocked immediately.
- A blocked task stays blocked on resume unless the caller deliberately passes
  retry_blocked:true after correcting the underlying condition.

The immutable source and original chunk-plan.json are unchanged. The scheduler
stores a local leaf tree for the current original parent in generation-run.json.
Previously accepted parents and leaf checkpoints are retained. Only the failed
leaf is replaced by smaller children; the event log records its replanned state.
Complete validated children are concatenated into the original parent before
its ordinary source-aware checkpoint and final exporter.

The parent may now contain more text than a single model call can emit.
generation_parts provides independently validated leaf drafts and exact source
ownership. Validation rechecks every leaf's per-call limit, note assignment and
the exact combined units. It never exempts one oversized model response by
marking it as a concatenation. Model-returned generation_parts is rejected.

## Durable recovery and run limits

generation-run.json owns leaf states, attempt counts, checkpoint hashes and plan
identity. generation-events.jsonl records transitions without book text.
Complete worker JSON is hashed and saved before validation. An interrupted
validating step can replay the saved response without another model call,
provided its context still matches. A kill before recording a provider result
can require a fresh call; provider calls are not claimed to be exactly-once.

Accepted leaf files are immutable. A crash between a parent commit and run-state
update is recovered from the authoritative accepted-draft ledger. Short-term
memory is reconstructed from the last accepted leaf checkpoint when resuming.

max_calls, max_seconds and max_output_tokens_total apply to each explicitly
started run. Output allowance is **reserved before every call**, including failed
calls, rather than trusting only successful usage reports. A timeout bounds an
individual call and the remaining run time. The scheduler returns
budget_exhausted with progress retained instead of continuing indefinitely.
A later deliberate run grants a new finite allowance; it preserves leaf retry
counts and split depth. This is a bounded continuous job, not an always-on daemon.

When all tasks are accepted, assemble:true invokes the production exporter once.
Unresolved source review can still return export_blocked; it does not erase
accepted text or claim a complete book. Use generation.status for compact progress.

## Short-term text memory

parameters.short_term_tokens defaults to 768 and can be set to 0 to disable it.
For newly planned workflows this reserve is added separately to the planner's
prompt/config overhead. It is distinct from the durable consistency-memory bound.

Context Builder supplies only:
- a bounded original tail before the owned span;
- a bounded original prefix after it, including chapter boundaries;
- the last accepted explanation/translation tail.

The entire serialized short-term object, including keys, must fit that reserve.
It is also counted in the complete input-plus-output check. The worker must
never explain those read-only neighbour spans a second time. The separate
short-term-memory.json is replaceable and recoverable, not a transcript archive
or a substitute for terminology/referent memory. Existing approved profiles are
not silently rewritten; missing reserve fields use the default and old plans
remain immutable. A tight old context can trigger local leaf splitting.

## Verification scope

Tests use fresh real subprocesses and original runtime fixtures to check multiple
calls per launch, retries, overflow splitting, oversized aggregate validation,
checkpoint replay, resume, isolation of the working directory and short-term
budgeting. Adapter transport tests mock the HTTP response. They do not call a
paid model or establish translation quality, live provider availability or
actual reader-platform acceptance.
