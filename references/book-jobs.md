# Whole-book jobs (v0.4)

A user request, a conversation turn, a scheduler batch and a model call have
different lifetimes. Use the following routing for a whole-book request:

```text
user request
  → foreground agent validates existing preview approval and the total grant
  → job.start submits a detached local supervisor, returns job_id
  → supervisor renews generation.run batches within that one total grant
  → each batch starts fresh model workers for chunks
  → checkpoints and final EPUB stay on disk
  → foreground agent uses job.status; it never receives the whole generated book
```

The existing generation.run is intentionally a **single bounded batch**.
A result of budget_exhausted is not an instruction for another chat turn.
The book supervisor continues with the next batch if the whole-book grant still
has room. It stops on completion, a real blocking failure, a stop request or total
budget exhaustion. It does not renew the total grant for itself.

## Unified interface

After source/profile setup, real preview approval and planning:

```json
{
  "api_version":"1.0",
  "operation":"job.start",
  "project":"/reading/book",
  "parameters":{
    "worker_command":["/absolute/python","/absolute/skill/agents/codex_worker.py"],
    "total_budget":{"max_calls":1000,"max_seconds":86400,"max_output_tokens":5000000},
    "batch":{"max_calls":20,"max_seconds":600,"timeout_seconds":180}
  }
}
```

These are illustrative explicit grants, not required defaults or a book-price
estimate. Agree the whole-book grant with the user during setup/preview, or use
an existing approved reusable job configuration. Do not silently turn a small
batch authorization into an unlimited job.

`job.start` returns immediately with job_id, pid and a log path.
`job.status` returns durable status and process liveness.
`job.stop` requests cooperative stopping and interrupts the current worker
process tree. Do not stop by deleting source, plans or checkpoints.

The supervisor always assembles at the end. A small batch setting such as
max_calls:3 is safe inside a larger book grant: it will be renewed automatically.
An existing request with assemble:false deliberately stops before export; a book
job does not use that setting.

## Execution and recovery

The supervisor uses a detached process on Windows and a new session on POSIX.
stdin/stdout are not attached to the chat tool; status, logs, requests and
budgets are durable project files. Tests verify that an actual launcher process
can exit before the supervisor runs later batches and produces the final EPUB.

This removes dependence on a foreground conversation turn. It is not a claim
that the machine can sleep, lose network/authentication, reboot, or that a host
cannot kill its entire process group. For reboot/failover persistence, deploy
the same supervisor under an operating-system service/job manager. Source and
generation checkpoints survive an interrupted process.

Every batch receives a reservation deducted from the total grant **before**
dispatch. Unused capacity is refunded after a valid batch result. A crash before
that result leaves the reservation charged conservatively. The supervisor does
not reset the total grant on a batch boundary. A new job.start is a new explicit
grant; it preserves the underlying accepted drafts and retry history.

The scheduler's call/time controls apply to actual worker execution. Output-token
reservations are strict provider caps with the Responses adapter. Codex CLI has
no max-output-token option in the inspected version: its output limit is a prompt
plus post-generation validation, so it is not a hard billing/token-usage cap.
Call/time limits and actual Codex account limits still apply. Prefer the API
adapter if an exact provider output cap is required.

## Codex backend

[agents/codex_worker.py](../agents/codex_worker.py) creates a new
`codex exec --ephemeral --skip-git-repo-check --ignore-user-config --sandbox read-only --json -`
task for each chunk. It never resumes the parent conversation. It uses the local
CLI's normal authentication; it does not copy, publish or modify auth files.
READING_CODEX_EXE can select the executable and READING_CODEX_MODEL can select
an available model. With --ignore-user-config, custom provider/profile settings
are not loaded; configure a suitable adapter when those settings are necessary.

Workers are instructed to return draft JSON only. Tool-execution events are
rejected. An empty cwd and a read-only CLI sandbox do not constitute an
operating-system file-access jail; source is withheld from the prompt by the
Context Builder.

See [official non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
for CLI flags and authentication. No actual model invocation is required to test
the supervisor; tests use original fixture workers and mocked event streams.

## Foreground agent behavior

For an approved whole-book job, launch once and report its identity, progress and
eventual artifact path. Do not generate one chunk in the foreground, repeatedly
ask the user to say continue, or paste chunk prose back into the conversation.
A user status request should query job.status without changing the job.

The preview-approval gate remains. For a new source/style requiring human preview,
the interaction necessarily includes that approval milestone. Once it is met,
one whole-book start request is sufficient; no per-chunk confirmations are needed.
Do not fabricate preview approval to advertise a one-message experience.
