#!/usr/bin/env python3
"""Plan the whole remaining source before any model-authored chunk generation.

Capacity is declared explicitly, never inferred from the running model. Token
counts are estimates, not provider tokenizer measurements. No model/API calls.
"""
from __future__ import annotations

import argparse
import copy
import math
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import epub_workflow as w


def tokens(text, config):
    return math.ceil(len(text) / config["chars_per_token"])


def configuration(values):
    c = dict(context_window=int(values["context_window"]),
             max_output_tokens=int(values["max_output_tokens"]),
             expansion_ratio=float(values.get("expansion_ratio", 12)),
             overhead_tokens=int(values.get("overhead_tokens", 4096)),
             safety_factor=float(values.get("safety_factor", .75)),
             chars_per_token=float(values.get("chars_per_token", 3)),
             output_fixed_tokens=256, output_per_span_tokens=96)
    w.require(c["context_window"] > 0 and c["max_output_tokens"] > 0, "Capacity limits must be positive")
    w.require(all(math.isfinite(c[k]) for k in ["expansion_ratio", "safety_factor", "chars_per_token"]), "Capacity values must be finite")
    w.require(c["expansion_ratio"] >= 1 and c["chars_per_token"] > 0 and 0 < c["safety_factor"] < 1, "Invalid expansion, token estimate, or safety factor")
    w.require(0 <= c["overhead_tokens"] < c["context_window"] * c["safety_factor"], "Instructions/context reserve exhausts context budget")
    return c


def estimate(spans, notes, c):
    source = sum(tokens(s["text"], c) for s in spans)
    note = sum(tokens(n["text"], c) for n in notes)
    generated = math.ceil((source + note) * c["expansion_ratio"]) + c["output_fixed_tokens"] + len(spans) * c["output_per_span_tokens"]
    return {"source_tokens": source, "note_tokens": note,
            "input_tokens": source + note + c["overhead_tokens"],
            "output_tokens": generated,
            "total_context_tokens": source + note + c["overhead_tokens"] + generated}


def fits(spans, notes, c):
    e = estimate(spans, notes, c)
    return e["output_tokens"] <= math.floor(c["max_output_tokens"] * c["safety_factor"]) and e["total_context_tokens"] <= math.floor(c["context_window"] * c["safety_factor"])


def note_material(archive, member, paragraph, source_node):
    """Resolve note bodies and exact call offsets in normalized source text."""
    result = []
    for ref in paragraph["footnotes"]:
        marked = copy.deepcopy(source_node)
        marker = "GUIDEDREADINGNOTECALLMARKER"
        anchors = marked.xpath('.//*[local-name()="a"][@href]')
        match = next((a for a in anchors if a.get("href") == ref["href"] and
                      (not ref.get("call_id") or a.get("id") == ref["call_id"])), None)
        w.require(match is not None, f"Cannot locate note call {member}#{ref['id']}")
        # The marker follows all text preceding this call; other numeric calls
        # are removed by the source extractor as usual.
        match.clear(keep_tail=True)
        match.text = marker
        cleaned, _ = w.clean_paragraph(marked)
        rendered = w.norm(w.visible(cleaned))
        offset = len(rendered.split(marker, 1)[0].rstrip())
        target = w.member_path(member, ref["href"])
        tree = w.parse_xml(archive.read(target))
        targets = tree.xpath('//*[@id=$ident]', ident=ref["id"])
        w.require(len(targets) == 1, f"Note target missing/ambiguous: {target}#{ref['id']}")
        node = targets[0]
        if w.local(node) == "a" and node.getparent() is not None:
            node = node.getparent()
        # EPUB endnotes may continue over several sibling paragraphs. Read to
        # the next note target, rather than silently keeping just the first p.
        bodies = [w.norm(w.visible(node))]
        if w.local(node) == "p":
            for sibling in node.itersiblings():
                ids = sibling.xpath('.//*[@id]/@id') + ([sibling.get("id")] if sibling.get("id") else [])
                if any(re.match(r"(?:n\d|note|fn|endnote)", ident, re.I) for ident in ids):
                    break
                if w.local(sibling) != "p":
                    break
                bodies.append(w.norm(w.visible(sibling)))
        text = w.norm(" ".join(bodies))
        w.require(text, f"Empty note body: {target}#{ref['id']}")
        result.append({"id": ref["id"], "source_p": paragraph["source_p"],
                       "call_offset": min(offset, len(paragraph["text"])),
                       "target_member": target, "text": text})
    return result


def owned_notes(span, notes):
    return [n for n in notes if span["start"] <= n["call_offset"] < span["end"] or
            n["call_offset"] == span["end"] == span["paragraph_length"]]


def boundaries(text, level):
    # A punctuation boundary is only a candidate; the author reviews literary
    # abbreviations/quotation continuity. No words or Unicode characters split.
    pattern = r'[.!?。！？][”’»\"\')\]]*\s+' if level == "sentence" else r'[,;:，；：—–]\s+'
    return [m.end() for m in re.finditer(pattern, text)]


def split_span(span, notes, c, level=0):
    if fits([span], owned_notes(span, notes), c):
        return [span]
    levels = ["sentence", "clause"]
    w.require(level < len(levels), f"Indivisible source/note exceeds declared capacity at {span.get('source_member', '')} paragraph {span['source_p']}, offsets {span['start']}:{span['end']}, attached notes {[n['id'] for n in owned_notes(span, notes)]}; review the boundary or capacity/expansion settings, never truncate source")
    cuts = boundaries(span["text"], levels[level])
    if not cuts:
        return split_span(span, notes, c, level + 1)
    pieces, start = [], 0
    for end in cuts + [len(span["text"])]:
        if start == end:
            continue
        part = {**span, "start": span["start"] + start, "end": span["start"] + end,
                "text": span["text"][start:end], "split_level": levels[level]}
        pieces.extend(split_span(part, notes, c, level + 1))
        start = end
    return pieces


def fingerprint(plan):
    return w.sha(w.json_bytes({k: v for k, v in plan.items() if k != "plan_sha256"}))


def validate_plan(project, plan):
    root, state, index, batches, covered = w.load_project(project)
    w.require(plan.get("schema_version") == 1 and plan.get("plan_sha256") == fingerprint(plan), "Chunk plan modified or unsupported")
    w.require(plan["source_sha256"] == state["source_sha256"] and plan["source_index_sha256"] == state["source_index_sha256"], "Chunk plan source mismatch")
    prefix = plan["legacy_prefix"]["commits"]
    w.require(state["commits"][:len(prefix)] == prefix, "Plan's accepted source prefix changed")
    actual_prefix = sum(s["end_p"] - s["start_p"] + 1 for b in batches[:len(prefix)] for s in b["sections"])
    w.require(plan["legacy_prefix"]["covered_paragraphs"] == actual_prefix, "Plan prefix coverage mismatch")
    c = configuration(plan["capacity"])
    expected = [(chapter["source_member"], p) for chapter in index["chapters"] for p in chapter["paragraphs"]][plan["legacy_prefix"]["covered_paragraphs"]:]
    cursor, offset = 0, 0
    ids = set()
    seen_notes, expected_notes = [], []
    for member, p in expected:
        expected_notes += [(member, p["source_p"], n["id"]) for n in p["footnotes"]]
    for sequence, chunk in enumerate(plan["chunks"], 1):
        w.require(chunk["sequence"] == sequence and chunk["chunk_id"] not in ids, "Chunk sequence/identity invalid")
        ids.add(chunk["chunk_id"])
        for span in chunk["spans"]:
            w.require(cursor < len(expected), "Chunk plan overlaps beyond selected corpus")
            member, p = expected[cursor]
            w.require(chunk["source_member"] == member and span["source_p"] == p["source_p"] and span["start"] == offset,
                      "Chunk plan has a source gap, overlap, or wrong order")
            w.require(offset <= span["end"] <= len(p["text"]) and span["text"] == p["text"][offset:span["end"]] and
                      span["paragraph_length"] == len(p["text"]), "Chunk span differs from indexed source")
            offset = span["end"]
            if offset == len(p["text"]):
                cursor += 1
                offset = 0
        w.require(fits(chunk["spans"], chunk["footnotes"], c), "Planned chunk exceeds declared capacity")
        w.require(chunk["estimated"] == estimate(chunk["spans"], chunk["footnotes"], c), "Chunk budget estimate changed")
        seen_notes += [(chunk["source_member"], n["source_p"], n["id"]) for n in chunk["footnotes"]]
    w.require(cursor == len(expected) and offset == 0, "Chunk plan omits selected source")
    w.require(sorted(seen_notes) == sorted(expected_notes), "Chunk plan duplicates or omits source notes")
    return plan


def load_plan(project):
    return validate_plan(project, w.read_json(Path(project) / "chunk-plan.json"))


def create_plan(project, values):
    c = configuration(values)
    with w.writer_lock(project) as root:
        w.recover_pending(root)
        root, state, index, batches, covered = w.load_project(root)
        destination = root / "chunk-plan.json"
        if destination.exists():
            old = load_plan(root)
            w.require(old["capacity"] == c, "A fixed plan already exists with different capacity; do not silently replan generated chunks")
            return old
        plan = {"schema_version": 1, "created_at": w.now(),
                "source_sha256": state["source_sha256"], "source_index_sha256": state["source_index_sha256"],
                "initial_master_sha256": state.get("output_sha256"),
                "capacity": c, "estimator": "ceil(Unicode code points / chars_per_token); heuristic, not a provider tokenizer",
                "budget": {"safe_output_tokens": math.floor(c["max_output_tokens"] * c["safety_factor"]),
                           "safe_context_tokens": math.floor(c["context_window"] * c["safety_factor"]),
                           "formula": "input=source+notes+overhead; output=ceil((source+notes)*expansion_ratio)+256+96*spans; input+output<=safe_context, output<=safe_output"},
                "legacy_prefix": {"covered_paragraphs": covered, "commits": copy.deepcopy(state["commits"])},
                "chapters": [], "chunks": []}
        titles = {s["source_member"]: s["title"] for b in batches for s in b["sections"]}
        skip = covered
        with zipfile.ZipFile(state["source_path"]) as archive:
            for chapter in index["chapters"]:
                if skip >= chapter["paragraph_count"]:
                    skip -= chapter["paragraph_count"]
                    continue
                start = skip
                skip = 0
                member = chapter["source_member"]
                title = titles.get(member, chapter["title"])
                source = w.parse_xml(archive.read(member))
                nodes = source.xpath('//*[local-name()="body"]//*[local-name()="p"]')
                groups, active, active_notes = [], [], []
                for p in chapter["paragraphs"][start:]:
                    notes = note_material(archive, member, p, nodes[p["source_p"] - 1])
                    span = {"source_member": member, "source_p": p["source_p"], "start": 0, "end": len(p["text"]),
                            "text": p["text"], "paragraph_length": len(p["text"]), "split_level": "paragraph"}
                    for piece in split_span(span, notes, c):
                        attached = owned_notes(piece, notes)
                        if active and not fits(active + [piece], active_notes + attached, c):
                            groups.append((active, active_notes))
                            active, active_notes = [], []
                        active.append(piece)
                        active_notes += attached
                if active:
                    groups.append((active, active_notes))
                chapter_record = {"source_member": member, "title": title, "chunk_ids": []}
                for spans, notes in groups:
                    seq = len(plan["chunks"]) + 1
                    ident = f"chunk-{seq:06d}"
                    plan["chunks"].append({"chunk_id": ident, "sequence": seq,
                                           "source_member": member, "title": title,
                                           "spans": spans, "footnotes": notes,
                                           "estimated": estimate(spans, notes, c)})
                    chapter_record["chunk_ids"].append(ident)
                plan["chapters"].append(chapter_record)
        plan["coverage"] = {"remaining_paragraphs": sum(ch["paragraph_count"] for ch in index["chapters"]) - covered,
                            "remaining_characters": sum(len(s["text"]) for ch in plan["chunks"] for s in ch["spans"]),
                            "chunk_count": len(plan["chunks"])}
        plan["plan_sha256"] = fingerprint(plan)
        validate_plan(root, plan)
        w.atomic_json(destination, plan)
        return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "show"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--expansion-ratio", type=float, default=12)
    parser.add_argument("--overhead-tokens", type=int, default=4096)
    parser.add_argument("--safety-factor", type=float, default=.75)
    parser.add_argument("--chars-per-token", type=float, default=3)
    args = parser.parse_args(argv)
    if args.command == "plan" and (args.context_window is None or args.max_output_tokens is None):
        parser.error("plan requires declared --context-window and --max-output-tokens")
    try:
        plan = create_plan(args.project, vars(args)) if args.command == "plan" else load_plan(args.project)
        result = plan if args.command == "show" else {k: plan[k] for k in ["plan_sha256", "capacity", "budget", "coverage"]}
        print(w.json_bytes(result).decode("utf8"), end="")
        return 0
    except (w.WorkflowError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(w.json_bytes({"status": "ERROR", "error": str(error)}).decode("utf8"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
