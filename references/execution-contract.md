# Unified execution contract — API 1.0 / specification 2.0

Run `python scripts/agent_api.py --request request.json`, or use `--request -` for JSON stdin. Agents may call `dispatch(request)` in-process. Successful responses contain `api_version, ok, result`; CLI errors contain `api_version, ok:false, error` and exit code 2. The Python API raises validation errors.

```json
{"api_version":"1.0","operation":"config.update","project":"/reading/book","parameters":{"expected_revision":1,"patch":{"parameters":{"source_language":"fr","explanation_language":"zh","source_proficiency":"B1","explanation_proficiency":"native","language_depth":"explain subordinate clauses; omit elementary articles","interpretation_depth":"deep","reading_goal":"follow the argument","focus":["negation","pronouns","logical transitions"],"translation_focus":["ambiguity","technical terminology"]}}}}
```

Profiles are reusable, source-independent specifications. Import a complete profile with `parameters.config` or `parameters.config_file` pointing to a JSON file. Patch updates recursively; optional expected_revision prevents lost updates. Every accepted update receives a new revision and a historical snapshot in profiles/. Missing required sections, disabled coverage requirements or oversized profiles are rejected. Source language is fixed after initialization. Once formally planned, use a new workflow to change the generation profile; do not mix rules mid-book.

## Operations

| Operation | Parameters and result |
| --- | --- |
| capabilities | No project required; list operations and API version |
| legacy.workflow | command: init/next/commit/stage/build/status/audit/recover; original named arguments; no source migration required |
| legacy.plan | command: plan/show; plan receives capacity object |
| legacy.chunks | command: next/save/status/assemble; save receives draft_path |
| config.get | Return current/default reusable profile |
| config.update | patch, config or config_file; optional expected_revision |
| init | source path, optional title, start_member, skip_members; freezes source and index without planning |
| preview.select | Optional selection mapping simple/complex/logical to source_member, source_p, optional start sentence offset; returns candidate positions only |
| preview.context | category; returns one bounded generation packet |
| preview.save | category, draft; validate a sample explanation |
| preview.export | Export actual Preview.epub using production exporter; return exact file hash |
| preview.approve | user_confirmed:true, confirmation human quote, epub_sha256; bind approval and measure sample expansion |
| plan | Require current approval; plan internally and return only task count/budgets |
| task.next | Return one bounded current task; or ready_to_assemble |
| task.save | draft, context_sha256; validate, persist, advance paragraph checkpoints |
| memory.update | accepted chunk_id, expected_revision, optional upsert/remove/summary |
| status | Compact counts and memory revision, no whole source |
| assemble | Final production export; optional allow_incomplete_source_review for an explicitly incomplete edition |
| audit | Inspect the actual published final EPUB |

A draft retains `plan_sha256, chunk_id, units, footnotes_integrated`. Each unit retains `source_ps, original, language[], translation, thought[]` and optional original_html, rendering_html, translation_lead, lang. The unchanged legacy schema is detailed in [workflow.md](workflow.md). For non-English explanation, specify translation_lead explicitly (empty text is valid) to avoid an English default lead.

The profile does not make language quality machine-verifiable. A worker must actually use the configured language and pedagogy; automated checks establish structure/coverage and metadata, not correctness of translation or philosophy.

## Preview, calibration and approval

Candidates are derived from sentence boundaries in the real source. Mechanical length and logical-connective scores only propose examples; the agent/human reviews their suitability. If the source lacks a requested category, state that limitation and agree representative alternatives before approval. Notes stay attached to the sentence containing their call. No formal chunk-plan.json is created until approval.

Each sample is saved independently and exported with the same package generator, CSS, ZIP writer and audit as formal output. The preview is explicitly labelled as samples, uses its own destination, and never advances formal progress.

The approval record binds the current profile revision/hash, selection, all sample draft hashes and Preview.epub hash. Changing any of these makes the old approval unusable. Record trusted human confirmation, not a self-generated claim of consent.

Calibration estimates output tokens from actual saved sample JSON and input tokens from sample source/notes. The maximum measured expansion across the three samples receives a 25% multiplier, with a minimum ratio of 3. Capacity additionally defaults to a 0.65 safety factor. The planner reserves prompt/config/context overhead and JSON overhead and respects both output and total-context limits. These are character/token estimates, not claims about a provider tokenizer or current model limits. Review working budgets with the actual host.

## Context Builder and scheduler boundary

The scheduler process may parse the entire source index and accepted ledger. It never returns them to generation workers. A task packet contains task identity, profile, bounded consistency memory, current source spans and notes, plan identity, a short instruction and a context hash. Its serialized size plus expected output must fit the conservative context budget.

The host should execute each generation as a separate limited worker task whose input is that packet plus fixed protocol instructions. This package does not call a model service, run an unbounded background loop, or enforce isolation inside a host's already-existing conversation. Do not paste the source archive/index into that conversation for formal generation.

The agent proposes explanations and optional consistency changes. The scheduler chooses tasks, rejects stale packets and advances source paragraphs only when their final span has been accepted. Saved chunk files remain authoritative if a secondary progress snapshot needs reconstruction. Repeated planning does not clear progress.

## Three memories

1. source-memory.json binds immutable/source.epub and source-index.json hashes. The snapshot survives external edits to the originally supplied file; edits to the bound snapshot/index are rejected.
2. chunk-progress.json plus task-progress.json track accepted tasks and fully processed source paragraphs. They hold no terminology or narrative interpretation.
3. consistency-memory.json contains revision, entries and summary. Entries use key, kind (term/referent/convention/context), value and evidence_chunk. Updates cite an accepted chunk and expected revision. Configured bounds cap entry count, entry size and summary size. Empty changes are unnecessary; prune rather than copying all generated prose.

Project-local memories are distinct from any host's global personal memory. No personal memory folder is read or modified by this module.

## Compatibility and export

The legacy init/next/commit/stage/build/audit/recover and plan/next/save/assemble command protocols are unchanged. Their behavior stays available for existing projects; the approval gate is enforced by the new scheduler entry point. Legacy callers can bypass the new workflow contract, so hosts must route formal new generation through agent_api.

Language configuration is applied by an additive exporter adapter around the existing package generator. Preview and final export use the same adapter; default English keeps legacy package behavior. Required source-coverage and note checks are retained. The unified audit applies the same language adapter for byte-consistent validation.
