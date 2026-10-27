# Foreign-language → English guided reading EPUB

A reusable Codex skill for reading a foreign-language book sentence by sentence in English.

**Plan the whole input → generate each chunk separately → concatenate once into one EPUB.**

Each reading unit preserves the original sentence, explains its vocabulary and grammar, gives a complete English rendering, and interprets its meaning in context. The final book reads continuously: original chapter headings appear once, with no visible unit numbers, teaching-category subtitles, exercises, or glossary.

The model writes the explanations. The Python scripts plan, validate, save, and assemble them; they do **not** call a model API or automatically translate a book on their own.

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
First plan all remaining source text against declared input/output budgets.
Generate each planned chunk as a separate explanation file, then concatenate
the completed chunks in original book order. Reuse approved work if present.
Save the final book to /absolute/path/reading-project/Guided_Reading.epub.
```

You can also ask the agent to resume an existing project. One invocation should keep processing the requested scope; internal chunks are checkpoints, not separately delivered volumes. An interruption leaves accepted drafts available for the next invocation. The skill is not a background scheduler.

## Workflow

1. **Plan all remaining input.** Read EPUB spine order, chapters, paragraphs, and notes. Declare context and output budgets, estimate the expansion into English teaching prose, and reserve prompt/context overhead and a safety margin. Divide at chapter and paragraph boundaries; subdivide oversized paragraphs into sentences, then meaningful clauses when necessary. Save the complete, nonoverlapping plan before generating new explanations.
2. **Generate independent chunks.** The model reads each owned source span and attached notes, with limited read-only neighbouring context. It writes and reviews the original, English language explanation, complete rendering, and interpretation in a separate JSON draft. Saving a draft validates and checkpoints it without rebuilding the book.
3. **Concatenate and assemble.** When every planned chunk is present, join the explanation units in source order, reunite split paragraphs, include any approved prefix, validate coverage, and build one final EPUB. Missing chunks block assembly. The user never has to merge installments.

Token counts are a configurable character-based heuristic. The scripts cannot discover the running model's true limits; use declared working budgets and calibrate the expansion ratio against representative guidance. An indivisible span or attached note that still exceeds budget stops planning rather than being truncated.

## Local helper commands

Run these from the skill directory, using absolute paths for book data. Example capacity values below are illustrative, not claims about a particular model:

```sh
python scripts/epub_workflow.py init --source /books/example.epub --project /reading/example --title "Example — English guided reading" --source-language fr --output /reading/example/Guided_Reading.epub
python scripts/plan_chunks.py plan --project /reading/example --context-window 32768 --max-output-tokens 8192 --expansion-ratio 12
python scripts/chunk_pipeline.py next --project /reading/example
```

The model then authors a draft using the returned chunk identity and plan hash. After review:

```sh
python scripts/chunk_pipeline.py save --project /reading/example --draft /reading/example/drafts/chunk-000001.json
python scripts/chunk_pipeline.py status --project /reading/example
```

Repeat `next → model authors/reviews → save`. Once all chunks are accepted:

```sh
python scripts/chunk_pipeline.py assemble --project /reading/example
python scripts/epub_workflow.py audit --project /reading/example
```

See [workflow details and the draft schema](references/workflow.md) and the [reading-format specification](references/format.md).

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

这是一个“外语原文 → 英文逐句导读”的 Codex skill。先结合原文规模与模型输入/输出预算规划全部分块，再逐块生成独立解释文本，最后按原书顺序统一拼接为一个 EPUB。断点续接不需要用户手动合并文件。实际英文解释由模型撰写，脚本负责规划、校验、存档和装配。

## License

[MIT](LICENSE). The license covers this skill's code and documentation; source books retain their own rights and terms.
