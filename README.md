# Foreign-language → English guided reading EPUB

A reusable guided-reading skill with configurable source and explanation languages, learner strategy, real-reader preview and deterministic single-task scheduling.

**Configure → preview real samples → human approval → calibrate and plan → bounded tasks → final EPUB.**

Each reading unit preserves the original sentence, explains its vocabulary and grammar, gives a complete English rendering, and interprets its meaning in context. The final book reads continuously: original chapter headings appear once, with no visible unit numbers, teaching-category subtitles, exercises, or glossary.

The model writes the explanations. The scheduler plans, launches independent model workers, validates, checkpoints and assembles. Provider-specific worker code handles model calls; the model only generates text.

## Install

Clone the skill into your local Codex skills directory:

**macOS / Linux**

```sh
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/xger/foreign-english-epub.git "$HOME/.agents/skills/foreign-english-epub"
```

**Windows PowerShell**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE/.agents/skills" | Out-Null
git clone https://github.com/xger/foreign-english-epub.git "$env:USERPROFILE/.agents/skills/foreign-english-epub"
```

Install the Python dependency with the interpreter your agent will use:

```sh
python -m pip install -r /path/to/foreign-english-epub/requirements.txt
```

Requires Python 3.10+ and `lxml`. Browser rendering is optional and uses Node.js, Playwright, and an installed browser. See [OpenAI's local skill documentation](https://learn.chatgpt.com/docs/build-skills) for discovery and invocation details. Restart Codex if the new skill does not appear.

Keep source books, generated projects, and outputs **outside the installed skill folder**.

## Use in Codex

```text
$foreign-english-epub

Turn /absolute/path/book.epub into a complete English guided-reading EPUB.
First prepare real-source preview samples and obtain my explicit approval.
Then calibrate output length and plan tasks within conservative budgets.
Generate each planned chunk as a separate explanation file, then concatenate
the completed chunks in original book order. Reuse approved work if present.
Save the final book to /absolute/path/reading-project/Guided_Reading.epub.
```

You can also ask the agent to resume an existing project. One invocation should keep processing the requested scope; internal chunks are checkpoints, not separately delivered volumes. An interruption leaves accepted drafts available for the next invocation. The skill is not a background scheduler.

## Unified agent interface (v0.2)

The platform-neutral entry point is `python scripts/agent_api.py --request request.json`
(or `--request -` for JSON stdin). The functional core contract is in
[SKILL.md](SKILL.md); host-specific invocation is in `agents/openai.yaml`.

```json
{"api_version":"1.0","operation":"config.get","project":"/reading/example"}
```

New workflow order:

1. `config.get/update`: import an existing [profile](profiles/default.json) or turn conversational preferences into explicit patches, including both languages and learner abilities.
2. `init`: freeze the source snapshot and index; do not plan chunks yet.
3. `preview.select/context/save`: explain three reviewed real-source examples (simple syntax, complex syntax, deep logical interpretation).
4. `preview.export`: build **Preview.epub** using the production exporter, for inspection in the actual reading platform.
5. `preview.approve`: record the human's explicit confirmation of the exact exported preview. Changed profile/samples invalidate approval.
6. `plan`: use measured sample output expansion and conservative margins to plan formal tasks internally.
7. `task.next/save`: the deterministic scheduler selects one task; a worker generates only from its bounded Context Builder packet.
8. `memory.update`: selectively retain necessary terminology, referents, context and conventions; no update is required for most chunks.
9. `assemble/audit`: concatenate and export one final book after all tasks are complete.

See [the execution contract](references/execution-contract.md) for every request,
calibration formulas, memory schemas and isolation boundaries. The deterministic helpers own data and progress. The continuous scheduler
can launch provider-specific model workers; those workers generate only text. Do not load the complete source index into a generation worker.

The existing helper commands remain supported as compatibility interfaces;
[legacy command details](references/workflow.md) are not the new default workflow.

## One request for a complete book (v0.4)

Use `job.start` after preview approval, with an explicit whole-book budget. It
returns a job identity and starts a detached supervisor that automatically
renews bounded scheduler batches, checkpoints independent chunk workers and
exports the final EPUB. The foreground chat only submits and monitors the job.
See [whole-book execution and the Codex backend](references/book-jobs.md).

## Continuous generation (v0.3)

After preview approval and planning, one `generation.run` request starts a bounded
continuous job: fresh worker process/model call → validation → checkpoint → next
task → final EPUB. No repeated chat messages are needed within the run allowance.

The supplied `agents/openai_worker.py` uses `OPENAI_API_KEY` and `READING_MODEL`
from the environment. It makes a separate stateless API request for each task;
it does not reuse desktop-chat credentials. Other backends can implement the
same stdin/stdout worker protocol. Configure absolute executable and script paths.

See [continuous generation](references/continuous-generation.md) for a complete
request, limits, failure transitions, local splitting, interruption recovery and
short-term text memory. Tests exercise subprocess scheduling and mock provider
transport; they do not spend API credits or certify model-generated book quality.

## Optional browser checks

From this repository, install the pinned renderer dependency and a Playwright browser:

```sh
npm install
npx playwright install chromium
node scripts/render_epub.cjs --root /reading/example/staging/current --out /reading/example/qa --expected-units 100
```

Replace `100` with the actual unit count. You may supply `--browser /path/to/browser` and `--module-dir /path/to/node_modules` when needed. The renderer checks narrow, regular, and enlarged-text layouts plus navigation. Inspect its screenshots as well as the JSON report.

## Project files

| File | Purpose |
| --- | --- |
| `source-index.json` | Fixed source hash, reading order, normalized paragraphs, notes, and unresolved material |
| `chunk-plan.json` | Full remaining-input hierarchy, exact source offsets, and capacity estimates |
| `chunk-drafts/` | Separate immutable English explanation drafts |
| `chunk-progress.json` | Accepted draft order and final assembly checkpoint |
| `workflow.json` / `batches/` | Validated annotations, reused approved content, and published-master state |
| `staging/current/` | Final XHTML used for package and browser inspection |

The core helper retains low-level `commit` for migrating existing content. New generation uses the planned chunk pipeline; it does not rebuild an EPUB after each chunk.

## Tests and limits

```sh
python -m unittest discover -s tests -v
```

Tests create small original EPUB fixtures at runtime. They check source coverage, separate chunk persistence, missing/out-of-order drafts, sentence/clause splitting, final order, note integration, idempotence, prefix reuse, and publication rollback. They do not score translation or interpretation quality.

The extractor tracks body paragraphs. Images, tables, formulas, and substantive excluded material require explicit review before a whole-book completion claim. The custom package audit is not EPUBCheck certification or a physical-reader test.

This repository contains the reusable skill and original test examples, not source books, personal reading projects, or generated book-length guides.

## 中文简介

这是一个“外语原文 → 英文逐句导读”的 Codex skill。先结合原文规模与模型输入/输出预算规划全部分块，再在真实样例预览并获用户确认后，校准预算并规划任务；由调度器分发有界任务、选择性维护一致性记忆，最终拼接为一个 EPUB。断点续接不需要用户手动合并文件。实际英文解释由模型撰写，脚本负责规划、校验、存档和装配。

## License

[MIT](LICENSE). The license covers this skill's code and documentation; source books retain their own rights and terms.
