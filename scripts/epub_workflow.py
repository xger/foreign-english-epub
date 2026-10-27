#!/usr/bin/env python3
"""Local, resumable assembly of human/agent-authored English EPUB guidance.

The CLI never calls a language model. It indexes source paragraphs, validates
authored batches, and publishes one cumulative EPUB without a manual merge.
Requires Python 3.10+ and lxml.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import hashlib
import html
import json
import os
import posixpath
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

VERSION = 1
XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
NS = {"h": XHTML, "o": OPF, "dc": DC}
PARSER = etree.XMLParser(resolve_entities=False, no_network=True)
INLINE = {"i", "em", "b", "strong", "span", "sup", "sub", "br", "a"}
CSS = """@charset "UTF-8";
html { background: white; }
body { color: #111; font-family: Georgia, 'Times New Roman', serif; line-height: 1.55; margin: 5%; }
h1 { font-family: Georgia, 'Times New Roman', serif; font-size: 1.55em; font-weight: normal; line-height: 1.3; margin: 1em 0 1.5em; }
p { margin: .65em 0; orphans: 2; widows: 2; }
.french { font-size: 1.05em; font-style: italic; margin: 1.3em 0 .8em; }
.unit { margin: 0; padding: 0; }
.rendering { margin: .8em 0; }
@media screen { body { max-width: 42em; margin: 2em auto; padding: 0 1em; } }
"""


class WorkflowError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def norm(text):
    return " ".join(html.unescape(text).split())


def local(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def visible(node):
    pieces = [node.text or ""]
    for child in node:
        pieces.append("\n" if local(child) == "br" else visible(child))
        pieces.append(child.tail or "")
    return html.unescape("".join(pieces))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def atomic_json(path, value):
    atomic_bytes(path, json_bytes(value))


def project_file(project, relative):
    root = Path(project).resolve()
    result = (root / relative).resolve()
    require(result.is_relative_to(root), "Project reference escapes project directory")
    return result


def member_path(base, href):
    parsed = urlsplit(href)
    require(not parsed.scheme and not parsed.netloc, "Source EPUB contains a nonlocal spine member")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))
    require(not path.startswith("../") and not path.startswith("/"), "Invalid EPUB member path")
    return path


def parse_xml(data):
    return etree.fromstring(data, PARSER)


def inline_fragment(value, name):
    require(isinstance(value, str) and value.strip(), f"{name} must be a nonempty string")
    # Source EPUBs sometimes use named HTML entities that are not XML built-ins.
    value = re.sub(r"&([A-Za-z][A-Za-z0-9]+);", lambda m: m.group(0) if m[1] in {"amp", "lt", "gt", "quot", "apos"} else html.unescape(m.group(0)), value)
    try:
        tree = parse_xml(f'<div xmlns="{XHTML}">{value}</div>'.encode("utf8"))
    except etree.XMLSyntaxError as error:
        raise WorkflowError(f"Invalid inline XHTML in {name}: {error}") from error
    require(norm(visible(tree)), f"{name} has no visible text")
    for node in tree.iterdescendants():
        tag = local(node)
        require(tag in INLINE, f"Unsupported or executable element <{tag}> in {name}")
        for attr, data in node.attrib.items():
            require(attr in {"lang", XML_LANG, "class", "href"}, f"Unsupported attribute {attr} in {name}")
            if attr == "href":
                u = urlsplit(data)
                require(tag == "a" and u.scheme == "https" and bool(u.netloc), f"Only HTTPS links are allowed in {name}")
            if attr == "class":
                require(re.fullmatch(r"[A-Za-z0-9 _-]{0,80}", data) is not None, f"Invalid class in {name}")
    require(not re.search(r"\b(?:TODO|TBD|PLACEHOLDER)\b", visible(tree)), f"Unfinished content in {name}")
    if "original_html" not in name:
        label_text = norm(visible(tree)).rstrip(":.：")
        require(label_text not in {"Language explanation", "Translation", "Interpretation", "The whole sentence", "Read the thought", "Build the language", "Working glossary"}, f"Visible category label in {name}")
        require(not re.match(r"^(?:Pause and try|Exercise\s*\d|Quiz\s*\d)\b", label_text, re.I), f"Auxiliary exercise in {name}")
    return value, tree


def footnote_id(value):
    return unquote(value.rsplit("#", 1)[-1])


def clean_paragraph(node):
    node = copy.deepcopy(node)
    notes = []
    for anchor in node.xpath('.//*[local-name()="a"][@href]'):
        href = anchor.get("href")
        label = norm(visible(anchor)).strip("[]()")
        marked = ("noteref" in anchor.get(EPUB_TYPE, "") or "noteref" in anchor.get("class", "")
                  or bool(anchor.xpath('.//*[local-name()="sup"]'))
                  or bool(re.search(r"(?:note|foot|endnote)", href, re.I)))
        if "#" in href and label.isdigit() and marked:
            notes.append({"id": footnote_id(href), "href": href, "call_id": anchor.get("id"), "label": label})
            anchor.clear(keep_tail=True)
    return node, notes


def outside_blocks(body):
    """Conservative inventory; these blocks are not claimed as translated."""
    blocks = []
    for node in body.iterdescendants():
        tag = local(node)
        if tag in {"img", "svg", "math", "table", "pre", "audio", "video"}:
            blocks.append({"kind": tag, "xpath": body.getroottree().getpath(node), "excerpt": norm(visible(node))[:180]})
        elif tag in {"li", "figcaption", "caption", "dt", "dd"} and not node.xpath('.//*[local-name()="p"]') and norm(visible(node)):
            blocks.append({"kind": tag, "xpath": body.getroottree().getpath(node), "excerpt": norm(visible(node))[:180]})
    for node in body.iter():
        tag = local(node)
        if tag in {"body", "div", "section", "article", "blockquote"}:
            if norm(node.text or ""):
                blocks.append({"kind": "text-outside-p", "xpath": body.getroottree().getpath(node), "excerpt": norm(node.text)[:180]})
            for child in node:
                if norm(child.tail or ""):
                    blocks.append({"kind": "text-outside-p", "xpath": body.getroottree().getpath(child), "excerpt": norm(child.tail)[:180]})
    return blocks


def index_source(source, start_member, skip_members):
    source = Path(source).resolve()
    require(source.is_file(), "Source EPUB does not exist")
    data = source.read_bytes()
    with zipfile.ZipFile(source) as z:
        require(z.testzip() is None, "Source ZIP is corrupt")
        container = parse_xml(z.read("META-INF/container.xml"))
        roots = container.xpath('//*[local-name()="rootfile"]/@full-path')
        require(len(roots) == 1, "Expected exactly one EPUB package root")
        opf_member = roots[0]
        opf = parse_xml(z.read(opf_member))
        manifest = {n.get("id"): n for n in opf.xpath('//*[local-name()="manifest"]/*[local-name()="item"]')}
        spine = opf.xpath('//*[local-name()="spine"]/*[local-name()="itemref"]')
        chapters, excluded = [], []
        started = start_member is None
        source_titles = opf.xpath('//*[local-name()="metadata"]/*[local-name()="title"]/text()')
        for seq, itemref in enumerate(spine):
            item = manifest.get(itemref.get("idref"))
            require(item is not None, "Spine points to a missing manifest item")
            member = member_path(opf_member, item.get("href"))
            require(member in z.namelist(), f"Missing source spine document: {member}")
            if member == start_member:
                started = True
            is_nav = "nav" in item.get("properties", "").split()
            reason = "before-start-member" if not started else "explicit-skip" if member in skip_members else "navigation" if is_nav else None
            if reason:
                record = {"source_member": member, "reason": reason, "classification": "navigation" if is_nav else "unreviewed-source-exclusion", "review_required": not is_nav}
                if not is_nav and item.get("media-type") in {"application/xhtml+xml", "text/html"}:
                    omitted = parse_xml(z.read(member))
                    omitted_bodies = omitted.xpath('//*[local-name()="body"]')
                    if len(omitted_bodies) == 1:
                        omitted_body = omitted_bodies[0]
                        omitted_ps = omitted_body.xpath('.//*[local-name()="p"]')
                        record["source_paragraph_count"] = len(omitted_ps)
                        record["excerpt"] = norm(visible(omitted_body))[:180]
                        if not any(norm(visible(p)) for p in omitted_ps) and not outside_blocks(omitted_body):
                            record.update(classification="heading-or-empty-structure", review_required=False)
                        elif (re.fullmatch(r"(?:foot|end)?notes?\.(?:x?html?)", Path(member).name, re.I)
                              and omitted_ps and all(re.match(r"note", p.get("class", ""), re.I) for p in omitted_ps)):
                            record.update(classification="separately-integrated-notes", review_required=False,
                                          note_target_ids=omitted_body.xpath('.//*[local-name()="a"][@id]/@id'))
                excluded.append(record)
                continue
            require(item.get("media-type") in {"application/xhtml+xml", "text/html"}, f"Non-XHTML reading document requires manual handling: {member}")
            tree = parse_xml(z.read(member))
            bodies = tree.xpath('//*[local-name()="body"]')
            require(len(bodies) == 1, f"Expected one source body in {member}")
            body = bodies[0]
            headings = body.xpath('.//*[local-name()="h1" or local-name()="h2"]')
            title_nodes = tree.xpath('//*[local-name()="head"]/*[local-name()="title"]')
            title = norm(visible(headings[0])) if headings else norm(visible(title_nodes[0])) if title_nodes else Path(member).stem
            paragraphs = []
            for number, paragraph in enumerate(body.xpath('.//*[local-name()="p"]'), 1):
                cleaned, refs = clean_paragraph(paragraph)
                paragraphs.append({"source_p": number, "text": norm(visible(cleaned)), "footnotes": refs,
                                   "required_superscripts": [norm(visible(n)) for n in cleaned.xpath('.//*[local-name()="sup"]')],
                                   "required_subscripts": [norm(visible(n)) for n in cleaned.xpath('.//*[local-name()="sub"]')]})
            chapters.append({"source_member": member, "spine_index": seq, "title": title,
                             "paragraphs": paragraphs, "paragraph_count": len(paragraphs),
                             "unresolved_nonparagraph_blocks": outside_blocks(body)})
        require(started, "The requested start member is not in the source spine")
        unknown = set(skip_members) - {c["source_member"] for c in chapters} - {c["source_member"] for c in excluded}
        require(not unknown, f"Skip member is not in source spine: {sorted(unknown)}")
        require(chapters and any(c["paragraphs"] for c in chapters), "No source paragraphs selected")
        # A notes document is not automatically dispensable merely because of
        # its filename. Its note targets must be represented by selected calls.
        referred = {r["id"] for c in chapters for p in c["paragraphs"] for r in p["footnotes"]}
        for record in excluded:
            if record["classification"] == "separately-integrated-notes":
                missing = set(record["note_target_ids"]) - referred
                if missing or not record["note_target_ids"]:
                    record.update(review_required=True, unreferenced_note_target_ids=sorted(missing))
    return {"schema_version": VERSION, "source_sha256": sha(data), "source_filename": source.name,
            "source_title": source_titles[0] if source_titles else source.stem,
            "opf_member": opf_member, "chapters": chapters, "excluded_members": excluded,
            "extraction_scope": "Body p elements in selected spine documents; numeric footnote calls removed and references recorded. Images, tables, math, lists and substantive nonparagraph text require explicit handling before whole-book completion."}


def chapter_map(index):
    return {c["source_member"]: c for c in index["chapters"]}


def pointer(index, covered):
    remain = covered
    for chapter in index["chapters"]:
        count = chapter["paragraph_count"]
        if remain < count:
            return {"source_member": chapter["source_member"], "source_p": remain + 1, "title": chapter["title"]}
        remain -= count
    require(remain == 0, "Committed paragraph count exceeds source corpus")
    return None


def display_pointer(index, covered, batches):
    """Retain an accepted chapter title when resuming an existing chapter."""
    point = pointer(index, covered)
    if point is None:
        return None
    point["source_title"] = point["title"]
    for batch in batches:
        for section in batch["sections"]:
            if section["source_member"] == point["source_member"]:
                point["title"] = section["title"]
                return point
    return point


def validate_batch(batch, state, index, covered):
    require(isinstance(batch, dict), "Batch must be an object")
    ident = batch.get("batch_id", "")
    require(isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", ident), "Invalid batch_id")
    require(batch.get("source_sha256") == state["source_sha256"], "Batch source hash does not match the project")
    sections = batch.get("sections")
    require(isinstance(sections, list) and bool(sections), "Batch needs nonempty sections")
    mapping = chapter_map(index)
    result = copy.deepcopy(batch)
    for section in result["sections"]:
        expected = pointer(index, covered)
        require(expected is not None, "Selected corpus is already covered")
        member = section.get("source_member")
        require(member == expected["source_member"], f"Out-of-order source member; expected {expected['source_member']}")
        chapter = mapping[member]
        start, end = section.get("start_p"), section.get("end_p")
        require(type(start) is int and type(end) is int and start == expected["source_p"] and start <= end <= chapter["paragraph_count"],
                f"Gap, overlap, or invalid paragraph range; expected start {expected['source_p']}")
        require(isinstance(section.get("title"), str) and norm(section["title"]), "Each section needs its original chapter title")
        paragraphs = chapter["paragraphs"][start - 1:end]
        units = section.get("units")
        require(isinstance(units, list) and (bool(units) or not any(norm(p["text"]) for p in paragraphs)), "Each substantive section needs reading units")
        original_units = []
        unit_spans = []
        superscripts, subscripts = [], []
        cursor = 0
        for n, unit in enumerate(units, 1):
            label = f"{ident}/{member}/unit{n}"
            original = unit.get("original")
            require(isinstance(original, str) and norm(original), f"Missing original in {label}")
            original_units.append(norm(original))
            unit_spans.append((cursor, cursor + len(norm(original)), unit.get("source_ps")))
            cursor += len(norm(original)) + 1
            require(re.fullmatch(r"[A-Za-z0-9-]{2,35}", unit.get("lang", state["source_language"])) is not None, f"Invalid source language in {label}")
            if unit.get("original_html") is not None:
                fragment, tree = inline_fragment(unit["original_html"], label + " original_html")
                require(norm(visible(tree)) == norm(original), f"Original HTML changes text in {label}")
                unit["original_html"] = fragment
                superscripts += [norm(visible(e)) for e in tree.xpath('.//*[local-name()="sup"]')]
                subscripts += [norm(visible(e)) for e in tree.xpath('.//*[local-name()="sub"]')]
            for field in ("language", "thought"):
                values = unit.get(field)
                require(isinstance(values, list) and bool(values), f"Missing {field} paragraphs in {label}")
                unit[field] = [inline_fragment(v, label + " " + field)[0] for v in values]
            has_rendering = isinstance(unit.get("rendering_html"), str) and bool(unit["rendering_html"].strip())
            has_translation = isinstance(unit.get("translation"), str) and bool(unit["translation"].strip())
            require(has_rendering or has_translation, f"Missing full sentence rendering in {label}")
            if has_rendering:
                unit["rendering_html"] = inline_fragment(unit["rendering_html"], label + " rendering")[0]
            if has_translation:
                require(not re.search(r"\b(?:TODO|TBD|PLACEHOLDER)\b", unit["translation"]), f"Unfinished translation in {label}")
            if "translation_lead" in unit:
                require(isinstance(unit["translation_lead"], str), f"translation_lead must be text in {label}")
        require(norm(" ".join(original_units)) == norm(" ".join(p["text"] for p in paragraphs)), f"Original source coverage differs in {ident}/{member}")
        spans, cursor = [], 0
        for paragraph in paragraphs:
            length = len(norm(paragraph["text"]))
            # Blank structural paragraphs own no source characters. Counting a
            # separator for them would shift every later unit's source_ps.
            if length:
                spans.append((cursor, cursor + length, paragraph["source_p"]))
                cursor += length + 1
        for left, right, assigned in unit_spans:
            expected_ps = [p for first, last, p in spans if left < last and right > first]
            require(isinstance(assigned, list) and assigned == expected_ps, f"Incorrect source_ps mapping in {ident}: expected {expected_ps}, got {assigned}")
        expected_refs = {r["id"] for p in paragraphs for r in p["footnotes"]}
        refs = section.get("footnotes_integrated", [])
        require(isinstance(refs, list) and all(isinstance(r, str) for r in refs), "footnotes_integrated must list reference IDs")
        require({footnote_id(r) for r in refs} == expected_refs, f"Missing or unexpected integrated notes in {ident}: expected {sorted(expected_refs)}")
        needed_sup = [s for p in paragraphs for s in p["required_superscripts"]]
        needed_sub = [s for p in paragraphs for s in p["required_subscripts"]]
        require(superscripts == needed_sup and subscripts == needed_sub, f"Meaningful superscripts/subscripts must be retained in original_html for {ident}/{member}")
        covered += end - start + 1
    return result, covered


def verify_source(state):
    source = Path(state["source_path"])
    require(source.is_file() and sha(source.read_bytes()) == state["source_sha256"], "Source EPUB changed or is unavailable; refuse to mix editions")


def load_project(project):
    root = Path(project).resolve()
    state = read_json(root / "workflow.json")
    require(state.get("schema_version") == VERSION, "Unsupported workflow state version")
    verify_source(state)
    index_path = project_file(root, state["source_index_file"])
    require(sha(index_path.read_bytes()) == state["source_index_sha256"], "Source index was modified")
    index = read_json(index_path)
    require(index["source_sha256"] == state["source_sha256"], "Index source hash mismatch")
    batches, covered = [], 0
    known_ids = set()
    for record in state["commits"]:
        path = project_file(root, record["file"])
        require(sha(path.read_bytes()) == record["sha256"], "Committed batch was modified")
        batch = read_json(path)
        require(batch["batch_id"] == record["batch_id"] and batch["batch_id"] not in known_ids, "Invalid committed batch identity")
        known_ids.add(batch["batch_id"])
        checked, covered = validate_batch(batch, state, index, covered)
        batches.append(checked)
    return root, state, index, batches, covered


def publication_snapshot(state, batches):
    """Reconstruct the state that produced the current master, including legacy projects."""
    count = state.get("published_commit_count", len(batches) if state.get("output_sha256") else 0)
    require(type(count) is int and 0 <= count <= len(batches), "Invalid published batch checkpoint")
    snapshot = copy.deepcopy(state)
    snapshot["revision"] = state.get("published_revision", state["revision"] if count else 0)
    snapshot["updated_at"] = state.get("published_updated_at", state["updated_at"])
    snapshot["commits"] = state["commits"][:count]
    snapshot["published_commit_count"] = count
    snapshot["published_revision"] = snapshot["revision"]
    snapshot["published_updated_at"] = snapshot["updated_at"]
    published = batches[:count]
    covered = sum(s["end_p"] - s["start_p"] + 1 for b in published for s in b["sections"])
    return snapshot, published, covered


def preserve_publication_checkpoint(state, batches):
    """Freeze legacy publication metadata before accepting unpublished guidance."""
    snapshot, published, _ = publication_snapshot(state, batches)
    state["published_commit_count"] = len(published)
    state["published_revision"] = snapshot["revision"]
    state["published_updated_at"] = snapshot["updated_at"]


def verify_tracked_output(state):
    output = Path(state["output_path"])
    actual = sha(output.read_bytes()) if output.exists() else None
    if state.get("output_sha256"):
        require(actual == state["output_sha256"], "Master EPUB was externally changed or is unavailable; rebuild or restore it before accepting guidance")
    else:
        require(actual is None, "Untracked master file already exists; choose another output")


def summary(state, index, batches, covered):
    total = sum(c["paragraph_count"] for c in index["chapters"])
    unresolved = sum(len(c["unresolved_nonparagraph_blocks"]) for c in index["chapters"])
    notes = sum(len(s.get("footnotes_integrated", [])) for b in batches for s in b["sections"])
    units = sum(len(s["units"]) for b in batches for s in b["sections"])
    selected_complete = covered == total
    exclusion_reviews = [e for e in index.get("excluded_members", []) if e.get("review_required", e.get("reason") != "navigation")]
    published_count = state.get("published_commit_count", len(batches) if state.get("output_sha256") else 0)
    require(type(published_count) is int and 0 <= published_count <= len(batches), "Invalid published batch checkpoint")
    published = batches[:published_count]
    published_covered = sum(s["end_p"] - s["start_p"] + 1 for b in published for s in b["sections"])
    published_units = sum(len(s["units"]) for b in published for s in b["sections"])
    return {"revision": state["revision"], "source_sha256": state["source_sha256"],
            "source_language": state["source_language"], "target_language": "en", "output": state["output_path"],
            "batches": len(batches), "annotated_reading_units": units, "source_paragraphs_covered": covered,
            "selected_source_paragraphs": total, "integrated_note_references": notes,
            "selected_paragraph_scope_complete": selected_complete,
            "generation_coverage_basis": "Accepted immutable explanation batches, including guidance not yet assembled into the master.",
            "master_exists": bool(state.get("output_sha256")),
            "master_up_to_date": bool(state.get("output_sha256")) and published_count == len(batches),
            "assembly_pending": published_count < len(batches),
            "published_revision": state.get("published_revision", state["revision"] if published_count else 0),
            "published_batches": published_count,
            "published_source_paragraphs_covered": published_covered,
            "published_annotated_reading_units": published_units,
            "unassembled_batches": len(batches) - published_count,
            "unresolved_nonparagraph_blocks": unresolved,
            "unreviewed_source_exclusions": len(exclusion_reviews),
            "source_exclusions_requiring_review": exclusion_reviews,
            "whole_book_complete": selected_complete and unresolved == 0 and not exclusion_reviews,
            "completion_basis": "All selected source paragraphs and their note references; whole_book_complete additionally requires no unresolved nonparagraph blocks or unreviewed source exclusions. Exclusions are explicitly classified in source-index.json; legacy unclassified exclusions remain review-required.",
            "next": display_pointer(index, covered, batches), "staging_dir": str(Path(state["project_path"]) / "staging" / "current")}


def document(title, body):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html xmlns="{XHTML}" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>{html.escape(title)}</title><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><link rel="stylesheet" type="text/css" href="../Styles/reading.css"/></head><body>{body}</body></html>'''


def package_members(state, index, batches, covered):
    status = summary(state, index, batches, covered)
    require(status["annotated_reading_units"] > 0, "No authored guidance is committed; no placeholder EPUB will be published")
    groups = {}
    for batch in batches:
        for section in batch["sections"]:
            member = section["source_member"]
            if member not in groups:
                groups[member] = {"title": section["title"], "units": []}
            require(groups[member]["title"] == section["title"], "A continued chapter changed its title; use one original chapter heading")
            groups[member]["units"].extend(section["units"])
    members = {"mimetype": b"application/epub+zip", "META-INF/container.xml": b'<?xml version="1.0" encoding="UTF-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', "OEBPS/Styles/reading.css": CSS.encode("utf8")}
    nav_items, ncx_items, manifest_items, spine_items = [], [], [], []
    unit_no, english_words = 0, 0
    languages = {"en", state["source_language"]}
    for chapter_no, chapter in enumerate(index["chapters"], 1):
        member = chapter["source_member"]
        if member not in groups:
            continue
        group = groups[member]
        title = group["title"]
        body = f'<main epub:type="bodymatter" id="chapter-{chapter_no:04d}"><h1 lang="{state["source_language"]}" xml:lang="{state["source_language"]}">{html.escape(title)}</h1>'
        for unit in group["units"]:
            unit_no += 1
            lang = unit.get("lang", state["source_language"])
            languages.add(lang)
            original = unit.get("original_html") or html.escape(unit["original"]).replace("\n", "<br/>")
            rendering = unit.get("rendering_html") or html.escape(unit.get("translation_lead", "The sentence can be rendered: ")) + "“" + html.escape(unit["translation"]) + "”"
            ps = ",".join(str(p) for p in unit["source_ps"])
            body += f'<div class="unit" id="u{unit_no:06d}" data-source-paragraphs="{ps}"><p class="french" lang="{lang}" xml:lang="{lang}">{original}</p>'
            body += "".join(f"<p>{p}</p>" for p in unit["language"])
            body += f'<p class="rendering">{rendering}</p>'
            body += "".join(f"<p>{p}</p>" for p in unit["thought"]) + "</div>"
            english = " ".join(unit["language"] + [rendering] + unit["thought"])
            english_words += len(re.findall(r"\b\w+(?:['’-]\w+)*\b", re.sub(r"<[^>]*>", " ", english)))
        body += "</main>"
        href = f"Text/c{chapter_no:04d}.xhtml"
        anchor = f"chapter-{chapter_no:04d}"
        members["OEBPS/" + href] = document(title, body).encode("utf8")
        nav_items.append(f'<li><a href="c{chapter_no:04d}.xhtml#{anchor}">{html.escape(title)}</a></li>')
        ncx_items.append(f'<navPoint id="nav{chapter_no}" playOrder="{len(ncx_items)+1}"><navLabel><text>{html.escape(title)}</text></navLabel><content src="{href}#{anchor}"/></navPoint>')
        manifest_items.append(f'<item id="c{chapter_no}" href="{href}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="c{chapter_no}"/>')
    members["OEBPS/Text/nav.xhtml"] = document("Navigation", '<nav epub:type="toc" id="toc"><ol>' + "".join(nav_items) + "</ol></nav>").encode("utf8")
    uid = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "guided-epub:" + state["source_sha256"] + ":" + state["title"]))
    title = state["title"] + (" — Complete selected reading" if status["whole_book_complete"] else " — Cumulative guided reading (in progress)")
    description = f'Cumulative English guided reading in source order. {covered} of {status["selected_source_paragraphs"]} selected source paragraphs annotated in {unit_no} reading units. '
    description += "Selected paragraph corpus and source review fully covered." if status["whole_book_complete"] else f'Guide incomplete; {status["unresolved_nonparagraph_blocks"]} nonparagraph source blocks require separate handling and {status["unreviewed_source_exclusions"]} source exclusions require review. Unannotated source text is not included in this cumulative guide.'
    metadata = f'<dc:identifier id="book-id">{uid}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>en</dc:language>'
    metadata += "".join(f"<dc:language>{lang}</dc:language>" for lang in sorted(languages - {"en"}))
    metadata += f'<dc:contributor>English reading guide prepared with AI assistance</dc:contributor><dc:publisher>Personal study edition</dc:publisher><dc:description>{html.escape(description)}</dc:description><dc:source>{html.escape(index["source_filename"])}</dc:source><meta property="dcterms:modified">{state["updated_at"]}</meta><meta property="rendition:layout">reflowable</meta>'
    members["OEBPS/content.opf"] = (f'<?xml version="1.0" encoding="UTF-8"?><package xmlns="{OPF}" version="3.0" unique-identifier="book-id" xml:lang="en"><metadata xmlns:dc="{DC}">{metadata}</metadata><manifest>' + "".join(manifest_items) + '<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="style" href="Styles/reading.css" media-type="text/css"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx">' + "".join(spine_items) + '</spine></package>').encode("utf8")
    members["OEBPS/toc.ncx"] = (f'<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="en"><head><meta name="dtb:uid" content="{uid}"/><meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head><docTitle><text>{html.escape(title)}</text></docTitle><navMap>' + "".join(ncx_items) + '</navMap></ncx>').encode("utf8")
    return members, {**status, "english_words_approximate": english_words, "reading_spine_documents": len(spine_items)}


def write_epub(path, members):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in ["mimetype"] + sorted(set(members) - {"mimetype"}):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            z.writestr(info, members[name])


def audit_epub(path, expected_members=None):
    trees, checked, count = {}, 0, 0
    with zipfile.ZipFile(path) as z:
        require(z.testzip() is None, "Candidate EPUB ZIP is corrupt")
        first = z.infolist()[0]
        require(first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED and z.read("mimetype") == b"application/epub+zip", "Invalid EPUB mimetype placement")
        names = set(z.namelist())
        require(len(names) == len(z.namelist()), "Duplicate EPUB members")
        if expected_members is not None:
            require(names == set(expected_members) and all(z.read(n) == expected_members[n] for n in names), "Candidate member mismatch")
        for name in names:
            if name.endswith((".xml", ".opf", ".ncx", ".xhtml")):
                trees[name] = parse_xml(z.read(name))
                ids = trees[name].xpath("//@id")
                require(len(ids) == len(set(ids)), f"Duplicate IDs in {name}")
        for name, tree in trees.items():
            for node in tree.iter():
                for attr in ("href", "src", "full-path"):
                    value = node.get(attr)
                    if value is None:
                        continue
                    u = urlsplit(value)
                    if u.scheme:
                        require(u.scheme == "https", "Non-HTTPS external reference")
                        continue
                    target = u.path if attr == "full-path" else posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(u.path))) if u.path else name
                    require(target in names, f"Broken resource {name}: {value}")
                    if u.fragment:
                        require(target in trees and u.fragment in trees[target].xpath("//@id"), f"Broken anchor {name}: {value}")
                    checked += 1
            if name.startswith("OEBPS/Text/c"):
                require(len(tree.xpath("//h:h1", namespaces=NS)) == 1, "Each source chapter needs one heading")
                require(not tree.xpath("//h:h2|//h:h3|//h:h4|//h:h5|//h:h6|//h:header|//h:footer|//h:nav|//h:script", namespaces=NS), "Unexpected visible subtitle/auxiliary section")
                count += len(tree.xpath('//h:div[@class="unit"]', namespaces=NS))
        opf = trees["OEBPS/content.opf"]
        ids = [n.get("idref") for n in opf.xpath('o:spine/o:itemref', namespaces=NS)]
        require(ids and "nav" not in ids, "Navigation must be outside reading spine")
        require(not re.search(r"page-break|break-before|break-after", z.read("OEBPS/Styles/reading.css").decode()), "Forced breaks in stylesheet")
    return {"status": "PASS", "zip_xml_links": "PASS", "internal_resource_references_checked": checked, "annotated_reading_units": count,
            "sha256": sha(Path(path).read_bytes()), "bytes": Path(path).stat().st_size,
            "validation_note": "Custom source/ZIP/XML/link/content checks; not an EPUBCheck certification or visual reader test."}


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            result = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(result)):
                return True
            return result.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextlib.contextmanager
def writer_lock(project):
    root = Path(project).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".workflow.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "wb") as f:
                f.write(json_bytes({"pid": os.getpid(), "created_at": now()}))
            break
        except FileExistsError:
            try:
                record = read_json(lock)
            except Exception as error:
                raise WorkflowError("Unreadable workflow lock; inspect it before proceeding") from error
            require(not process_alive(record.get("pid")), "Another workflow writer is active")
            lock.unlink()
    else:
        raise WorkflowError("Unable to acquire workflow writer lock")
    try:
        yield root
    finally:
        if lock.exists() and read_json(lock).get("pid") == os.getpid():
            lock.unlink()


def recover_pending(root):
    journal_path = root / "journal.json"
    if not journal_path.exists():
        return False
    journal = read_json(journal_path)
    if journal.get("phase") == "complete":
        return False
    state = read_json(root / "workflow.json")
    output = Path(state["output_path"])
    state_is_new = (state["revision"] == journal["new_revision"] and state.get("output_sha256") == journal["new_output_sha256"])
    if state_is_new:
        require(output.exists() and sha(output.read_bytes()) == journal["new_output_sha256"], "Published state/output mismatch; restore or rebuild from committed batches")
    else:
        require(state["revision"] == journal["old_revision"], "Unexpected state revision in recovery journal")
        if output.exists() and sha(output.read_bytes()) == journal["new_output_sha256"]:
            if journal.get("backup_file"):
                backup = project_file(root, journal["backup_file"])
                require(sha(backup.read_bytes()) == journal["old_output_sha256"], "Recovery backup changed")
                atomic_bytes(output, backup.read_bytes())
            else:
                output.unlink()
        elif journal.get("old_output_sha256"):
            require(output.exists() and sha(output.read_bytes()) == journal["old_output_sha256"], "Output was externally changed during interrupted transaction")
    journal["phase"] = "complete"
    journal["recovered_at"] = now()
    if state["commits"] and state.get("output_sha256"):
        _, state, index, batches, covered = load_project(root)
        snapshot, published, published_covered = publication_snapshot(state, batches)
        members, report = package_members(snapshot, index, published, published_covered)
        report.update(audit_epub(output, members))
        report["generation_status"] = summary(state, index, batches, covered)
        for name, data in members.items():
            atomic_bytes(project_file(root / "staging" / "current", name), data)
        atomic_json(root / "audit-report.json", report)
    atomic_json(journal_path, journal)
    return True


def publish(root, state, index, batches, covered, batch=None):
    output = Path(state["output_path"])
    old_state = copy.deepcopy(state)
    old_report = (root / "audit-report.json").read_bytes() if (root / "audit-report.json").exists() else None
    old_sha = sha(output.read_bytes()) if output.exists() else None
    if state.get("output_sha256"):
        require(old_sha == state["output_sha256"], "Master EPUB was externally changed; refuse to overwrite it")
    else:
        require(old_sha is None, "Untracked master file already exists; choose another output")
    state = copy.deepcopy(state)
    if batch is not None:
        batch_data = json_bytes(batch)
        relative = f'batches/{batch["batch_id"]}--{sha(batch_data)[:16]}.json'
        target = project_file(root, relative)
        if target.exists():
            require(target.read_bytes() == batch_data, "Immutable batch filename conflict")
        else:
            atomic_bytes(target, batch_data)
        state["commits"].append({"batch_id": batch["batch_id"], "file": relative, "sha256": sha(batch_data)})
        state["revision"] += 1
        state["updated_at"] = now()
    state["published_commit_count"] = len(batches)
    state["published_revision"] = state["revision"]
    state["published_updated_at"] = state["updated_at"]
    members, report = package_members(state, index, batches, covered)
    tx = f'transactions/{uuid.uuid4().hex}'
    txdir = project_file(root, tx)
    txdir.mkdir(parents=True)
    candidate = txdir / "candidate.epub"
    write_epub(candidate, members)
    check = audit_epub(candidate, members)
    require(check["annotated_reading_units"] == report["annotated_reading_units"], "Candidate reading-unit count mismatch")
    report.update(check)
    state["output_sha256"] = check["sha256"]
    state["coverage"] = summary(state, index, batches, covered)
    backup_file = None
    if output.exists():
        backup_file = "last-good.epub"
        atomic_bytes(project_file(root, backup_file), output.read_bytes())
    journal = {"phase": "prepared", "old_revision": old_state["revision"], "new_revision": state["revision"],
               "old_output_sha256": old_sha, "new_output_sha256": check["sha256"], "backup_file": backup_file,
               "candidate_file": tx + "/candidate.epub", "created_at": now()}
    atomic_json(root / "journal.json", journal)
    try:
        atomic_bytes(output, candidate.read_bytes())
        journal["phase"] = "output-published"
        atomic_json(root / "journal.json", journal)
        atomic_json(root / "workflow.json", state)
        for name, data in members.items():
            path = project_file(root / "staging" / "current", name)
            atomic_bytes(path, data)
        atomic_json(root / "audit-report.json", report)
        journal["phase"] = "complete"
        journal["completed_at"] = now()
        atomic_json(root / "journal.json", journal)
        # Immutable authored batches can reproduce historical editions. Keep
        # only one last-good master, not a quadratic series of large archives.
        try:
            candidate.unlink()
            txdir.rmdir()
        except OSError:
            pass  # A committed result remains valid if optional cleanup fails.
    except Exception:
        # A normal exception rolls back both artifacts. A killed process leaves a
        # journal; the next writer recovers against the last atomic state.
        atomic_json(root / "workflow.json", old_state)
        if old_report is not None:
            atomic_bytes(root / "audit-report.json", old_report)
        elif (root / "audit-report.json").exists():
            (root / "audit-report.json").unlink()
        if backup_file:
            atomic_bytes(output, project_file(root, backup_file).read_bytes())
        elif output.exists() and sha(output.read_bytes()) == check["sha256"]:
            output.unlink()
        journal["phase"] = "complete"
        journal["rolled_back_at"] = now()
        atomic_json(root / "journal.json", journal)
        raise
    return report


def command_init(args):
    project = Path(args.project).resolve()
    source, output = Path(args.source).resolve(), Path(args.output).resolve()
    require(source != output, "Output must not overwrite the source EPUB")
    require(re.fullmatch(r"[A-Za-z0-9-]{2,35}", args.source_language), "Invalid source-language code")
    with writer_lock(project) as root:
        require(not (root / "workflow.json").exists(), "Project is already initialized; use status or next")
        require(not output.exists(), "Output file already exists; choose a new master filename")
        index = index_source(source, args.start_member, args.skip_member)
        index_data = json_bytes(index)
        atomic_bytes(root / "source-index.json", index_data)
        stamp = now()
        state = {"schema_version": VERSION, "project_path": str(root), "source_path": str(source),
                 "source_sha256": index["source_sha256"], "source_language": args.source_language,
                 "title": args.title, "output_path": str(output), "created_at": stamp, "updated_at": stamp,
                 "source_index_file": "source-index.json", "source_index_sha256": sha(index_data),
                 "revision": 0, "commits": [], "output_sha256": None,
                 "published_commit_count": 0, "published_revision": 0, "published_updated_at": stamp}
        state["coverage"] = summary(state, index, [], 0)
        atomic_json(root / "workflow.json", state)
    return state["coverage"]


def command_next(args):
    root, state, index, batches, covered = load_project(args.project)
    point = display_pointer(index, covered, batches)
    if point is None:
        return {**summary(state, index, batches, covered), "paragraphs": [], "message": "Selected paragraphs covered; inspect unresolved nonparagraph blocks before claiming whole-book completion."}
    chapter = chapter_map(index)[point["source_member"]]
    start = point["source_p"]
    end = min(chapter["paragraph_count"], start + args.max_paragraphs - 1)
    return {"source_path": state["source_path"], "source_sha256": state["source_sha256"],
            "source_member": point["source_member"], "title": point["title"], "source_title": point["source_title"], "start_p": start, "end_p": end,
            "paragraphs": chapter["paragraphs"][start - 1:end],
            "unresolved_nonparagraph_blocks": chapter["unresolved_nonparagraph_blocks"],
            "scope_note": index["extraction_scope"]}


def stage_batch(root, state, index, batches, covered, batch):
    """Accept one complete-paragraph batch without assembling or changing the EPUB."""
    verify_tracked_output(state)
    titles = {}
    for old in batches + [batch]:
        for section in old["sections"]:
            member = section["source_member"]
            require(member not in titles or titles[member] == section["title"], "A continued chapter changed its title; use one original chapter heading")
            titles[member] = section["title"]
    new_state = copy.deepcopy(state)
    preserve_publication_checkpoint(new_state, batches)
    batch_data = json_bytes(batch)
    relative = f'batches/{batch["batch_id"]}--{sha(batch_data)[:16]}.json'
    target = project_file(root, relative)
    if target.exists():
        require(target.read_bytes() == batch_data, "Immutable batch filename conflict")
    else:
        atomic_bytes(target, batch_data)
    # The immutable file is written first. A failed/killed state write leaves
    # only an unreferenced file, which an identical retry can reuse safely.
    new_state["commits"].append({"batch_id": batch["batch_id"], "file": relative, "sha256": sha(batch_data)})
    new_state["revision"] += 1
    new_state["updated_at"] = now()
    result = summary(new_state, index, batches + [batch], covered)
    new_state["coverage"] = result
    atomic_json(root / "workflow.json", new_state)
    return {**result, "status": "STAGED", "master_rebuilt": False}


def command_commit(args):
    with writer_lock(args.project) as root:
        recover_pending(root)
        root, state, index, batches, covered = load_project(root)
        proposed = read_json(args.batch)
        ident = proposed.get("batch_id") if isinstance(proposed, dict) else None
        for previous in batches:
            if previous["batch_id"] == ident:
                # Canonical validation may normalize named entities. Validate
                # the proposal from the prior batch's exact source offset.
                before = 0
                for old in batches:
                    if old["batch_id"] == ident:
                        break
                    before += sum(s["end_p"] - s["start_p"] + 1 for s in old["sections"])
                checked, _ = validate_batch(proposed, state, index, before)
                require(json_bytes(checked) == json_bytes(previous), "Conflicting reuse of a committed batch_id")
                verify_tracked_output(state)
                return {**summary(state, index, batches, covered), "status": "UNCHANGED", "idempotent": True}
        checked, new_covered = validate_batch(proposed, state, index, covered)
        if getattr(args, "defer_build", False):
            return stage_batch(root, state, index, batches, new_covered, checked)
        return publish(root, state, index, batches + [checked], new_covered, checked)


def command_build(args):
    with writer_lock(args.project) as root:
        recover_pending(root)
        root, state, index, batches, covered = load_project(root)
        return publish(root, state, index, batches, covered)


def command_status(args):
    root, state, index, batches, covered = load_project(args.project)
    result = summary(state, index, batches, covered)
    result["pending_transaction"] = (root / "journal.json").exists() and read_json(root / "journal.json").get("phase") != "complete"
    return result


def command_audit(args):
    root, state, index, batches, covered = load_project(args.project)
    generation = summary(state, index, batches, covered)
    if not state.get("output_sha256"):
        require(not Path(state["output_path"]).exists(), "Untracked master file exists")
        return {"status": "NOT_ASSEMBLED", "audit_scope": "No master EPUB has been published", "generation_status": generation}
    snapshot, published, published_covered = publication_snapshot(state, batches)
    members, report = package_members(snapshot, index, published, published_covered)
    report.update(audit_epub(state["output_path"], members))
    require(report["sha256"] == state["output_sha256"], "Master hash differs from committed state")
    report["staging_matches_master"] = all(project_file(root / "staging" / "current", n).is_file() and project_file(root / "staging" / "current", n).read_bytes() == d for n, d in members.items())
    report["audit_scope"] = "Published master only; generation_status also describes unassembled explanations"
    report["generation_status"] = generation
    return report


def command_recover(args):
    with writer_lock(args.project) as root:
        recovered = recover_pending(root)
    return {"recovered": recovered, **command_status(args)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("init", help="Index a source EPUB and create a resumable project")
    p.add_argument("--source", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--source-language", default="fr")
    p.add_argument("--output", required=True)
    p.add_argument("--start-member")
    p.add_argument("--skip-member", action="append", default=[])
    p.set_defaults(run=command_init)
    for name, func in [("next", command_next), ("commit", command_commit), ("stage", command_commit), ("build", command_build), ("status", command_status), ("audit", command_audit), ("recover", command_recover)]:
        p = commands.add_parser(name)
        p.add_argument("--project", required=True)
        p.set_defaults(run=func)
        if name == "next":
            p.add_argument("--max-paragraphs", type=int, default=12)
        if name in {"commit", "stage"}:
            p.add_argument("--batch", required=True)
            if name == "stage":
                p.set_defaults(defer_build=True)
            else:
                p.add_argument("--defer-build", action="store_true", help="Validate and save explanations without assembling the master EPUB")
    args = parser.parse_args(argv)
    if hasattr(args, "max_paragraphs") and args.max_paragraphs < 1:
        parser.error("--max-paragraphs must be positive")
    try:
        result = args.run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, OSError, ValueError, zipfile.BadZipFile, etree.XMLSyntaxError) as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
