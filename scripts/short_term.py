"""Bounded, replaceable text continuity; separate from durable terminology."""
import re
import epub_workflow as w
import plan_chunks as planner


def reserve(config):
    value = config["parameters"].get("short_term_tokens", 768)
    w.require(type(value) is int and 0 <= value <= 4096, "Invalid short-term reserve")
    return value


def packet(root, chunk, config):
    budget = reserve(config)
    cap = config["parameters"]["capacity"]
    # JSON keys/escaping also count; prune text until the entire object fits.
    result = {"read_only": True, "before": "", "after": "", "previous_guidance": ""}
    if budget == 0: return {}
    index = w.read_json(root / "source-index.json")
    ch = w.chapter_map(index)[chunk["source_member"]]
    first, last = chunk["spans"][0], chunk["spans"][-1]
    before = ch["paragraphs"][first["source_p"]-1]["text"][:first["start"]]
    after = ch["paragraphs"][last["source_p"]-1]["text"][last["end"]:]
    if not before and first["source_p"] > 1: before = ch["paragraphs"][first["source_p"]-2]["text"]
    if not after and last["source_p"] < ch["paragraph_count"]: after = ch["paragraphs"][last["source_p"]]["text"]
    position = next(i for i,c in enumerate(index["chapters"]) if c["source_member"] == chunk["source_member"])
    if not before and first["source_p"] == 1 and first["start"] == 0:
        before = next((p["text"] for c in reversed(index["chapters"][:position]) for p in reversed(c["paragraphs"]) if p["text"]), "")
    if not after and last["source_p"] == ch["paragraph_count"] and last["end"] == last["paragraph_length"]:
        after = next((p["text"] for c in index["chapters"][position+1:] for p in c["paragraphs"] if p["text"]), "")
    memory = w.read_json(root/"short-term-memory.json") if (root/"short-term-memory.json").exists() else {}
    state = w.read_json(root/"workflow.json")
    w.require(not memory or memory.get("source_sha256") == state["source_sha256"], "Short-term source mismatch")
    size = max(0, int((budget-80)*cap["chars_per_token"] / 3))
    result.update(before=before[-size:] if size else "", after=after[:size], previous_guidance=str(memory.get("guidance_tail", ""))[-size:] if size else "")
    while planner.tokens(w.json_bytes(result).decode(), cap) > budget:
        key = max(["before", "after", "previous_guidance"], key=lambda k: len(result[k]))
        if not result[key]: return {}
        result[key] = result[key][len(result[key])//2:] if key != "after" else result[key][:len(result[key])//2]
    return result


def remember(root, chunk, draft, config):
    size = int(reserve(config)*config["parameters"]["capacity"]["chars_per_token"] / 3)
    last = draft["units"][-1] if draft["units"] else {}
    text = " ".join([last.get("translation", ""), *last.get("thought", [])])
    text = re.sub(r"<[^>]+>", " ", text)
    w.atomic_json(root/"short-term-memory.json", {"schema_version":1,
        "source_sha256":w.read_json(root/"workflow.json")["source_sha256"],
        "last_task":chunk["chunk_id"], "guidance_tail":text[-size:] if size else ""})
