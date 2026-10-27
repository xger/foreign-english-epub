# Plan → generate chunks → concatenate

Python 3.10+ and `lxml` are required. All scripts are local and make no model/API calls. Store book projects in the user's authorized workspace, outside the installed skill folder. Commands below are relative to the skill root; use absolute script paths when working from another directory. `PROJECT`, `SOURCE.epub`, `MASTER.epub`, draft paths and QA paths are user-selected locations outside the skill folder.

## Commands

Initialize a new project once; existing projects retain their source index and approved prefix:

```text
python scripts/epub_workflow.py init --source SOURCE.epub --project PROJECT --title "Book — English guided reading" --source-language fr --output MASTER.epub
```

`--start-member MEMBER` selects the reading start; repeatable `--skip-member MEMBER` records explicit exclusions. Members are normalized ZIP paths. Inspect source scope before accepting exclusions.

Plan all remaining source. The following numbers are **illustrative declared working budgets**, not claims about the current model:

```text
python scripts/plan_chunks.py plan --project PROJECT --context-window 32768 --max-output-tokens 8192 --expansion-ratio 12 --overhead-tokens 4096 --safety-factor 0.75 --chars-per-token 3
python scripts/plan_chunks.py show --project PROJECT
python scripts/chunk_pipeline.py next --project PROJECT
```

The model reads the returned chunk and writes a separate draft JSON. Then:

```text
python scripts/chunk_pipeline.py save --project PROJECT --draft DRAFT.json
python scripts/chunk_pipeline.py status --project PROJECT
```

Repeat `next → model authors/reviews draft → save` sequentially. No book is built during this phase. After all planned chunks are present:

```text
python scripts/chunk_pipeline.py assemble --project PROJECT
python scripts/epub_workflow.py audit --project PROJECT
node scripts/render_epub.cjs --root PROJECT/staging/current --out QA_DIR --expected-units N
```

`assemble` rejects missing chunks. It also blocks unresolved substantive source reviews. `--allow-incomplete-source-review` permits an explicitly incomplete edition after every planned paragraph chunk is generated; it never makes a full-book claim. There is no flag to bypass missing generated chunks.

## Capacity calculation and hierarchy

Let `S` and `N` be estimated source and attached-note tokens, `H` the prompt/context reserve, `r` the source-to-total-generated expansion ratio, and `k` the number of source spans. The current conservative heuristic is:

```text
S = sum(ceil(source_span_characters / chars_per_token))
N = sum(ceil(note_characters / chars_per_token))
estimated_output = ceil((S + N) * r) + 256 + 96*k
estimated_input = S + N + H
estimated_output <= floor(max_output_tokens * safety_factor)
estimated_input + estimated_output <= floor(context_window * safety_factor)
```

`r` includes returned originals, English language teaching, full renderings and interpretations. JSON overhead has a separate allowance. Reserve `H` for the actual instructions/format and read-only context. `next` caps neighbouring context to a quarter of that reserve; the caller must also keep its own instructions within the remaining reserve. Do not send the entire book or all prior English guidance with each chunk.

Counts are a Unicode-character heuristic, not the provider tokenizer. Calibrate expansion and character/token settings from the actual language and approved guidance, and keep a safety margin. Generated draft size is checked against the declared safe output budget; semantic completeness still requires model review.

The planner keeps chapter boundaries, groups complete paragraphs when they fit, and recursively subdivides oversized paragraphs at sentence then clause punctuation followed by whitespace. Boundary candidates need author review for abbreviations, quotations and verse. Words are never cut. An indivisible unit/attached note exceeding capacity stops planning with its source location; adjust settings or deliberately review a finer meaningful segmentation before generating. Planning never writes a truncated source span.

## Plan and draft schemas

`chunk-plan.json` stores immutable source/index hashes, capacity settings, accepted legacy prefix, chapters, ordered chunks and a plan hash. Offsets are half-open Unicode code-point offsets into the **normalized source-index paragraph text**. The source EPUB is never edited.

Each chunk records `chunk_id`, `sequence`, `source_member`, `title`, `spans`, `footnotes` and `estimated`. Each span records `source_p`, `start`, `end`, `text`, `paragraph_length` and `split_level`. Note bodies are extracted locally and assigned to the chunk containing their call. Read original note formatting as necessary.

The model produces a separate draft:

```json
{
  "plan_sha256": "HASH_FROM_NEXT",
  "chunk_id": "chunk-000001",
  "units": [{
    "source_ps": [1],
    "original": "Exact source sentence.",
    "lang": "fr",
    "language": ["Connected English vocabulary and syntax explanation."],
    "translation": "Complete English rendering.",
    "translation_lead": "The sentence can be rendered: ",
    "thought": ["Interpretation grounded in the source and surrounding argument."]
  }],
  "footnotes_integrated": []
}
```

Prose fields contain paragraph contents, without outer `<p>` elements. Inline emphasis, meaningful superscripts/subscripts and HTTPS source links are allowed. `original_html` preserves meaningful original markup and must display the same words as `original`. `rendering_html` may preserve an existing approved complete rendering paragraph instead of `translation`. No visible teaching-category labels or unit headings.

Multiple sentence units can share one paragraph number. A split paragraph appears in consecutive chunks; final assembly reunites its units before the core paragraph-level check. Read-only neighbouring source is never part of a draft's ownership. Exact normalized concatenation of originals must cover every planned span without gaps or duplicates.

## Persistence and assembly

- `workflow.json` and `source-index.json`: source integrity, selected order, validated complete-paragraph annotations and published master checkpoint.
- `chunk-plan.json`: full remaining-input hierarchy and budgets, generated **before** new drafts.
- `chunk-drafts/`: separate immutable authored explanation files, identified by chunk and hash.
- `chunk-progress.json`: accepted draft order and final assembly hash. Resume the first missing chunk.
- `batches/`: approved prefix plus one final combined annotation record. No complete EPUB per chunk.
- `staging/current/`, `audit-report.json`, `journal.json`, `last-good.epub`: package inspection and transactional publication/recovery.

Saving a duplicate identical draft is a no-op. Conflicting drafts, changed sources/plans, out-of-order saves and missing fields/notes are rejected. Final concatenation is validated before staging. The core's `stage` saves complete-paragraph content without rebuilding; `build` publishes all staged content. The high-level pipeline uses these mechanics once all chunks are ready. Historical `commit` still publishes immediately for migration compatibility; do not use it in the new chunk-generation loop.

An interrupted final publication is recovered through `epub_workflow.py recover --project PROJECT`; the checkpoint keeps accepted content and the last good output. `status` distinguishes generated content from published content. `audit` checks the actual published prefix when staged content is newer. A paragraph-only completion count cannot certify unresolved images, tables, formulas or excluded substantive content.

