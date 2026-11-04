#!/usr/bin/env python3
"""One bounded run, many fresh worker processes; durable local recovery."""
from __future__ import annotations
import argparse, copy, json, math, os, subprocess, tempfile, time, signal
from pathlib import Path
import agent_api as api
import epub_workflow as w
import plan_chunks as planner
import chunk_pipeline as chunks
import short_term


class WorkerFailure(Exception):
    def __init__(self, kind): self.kind = kind


def status(project):
    root = Path(project).resolve()
    state = api.read(root, "generation-run.json", {})
    return {"status": state.get("status", "not_started"), "calls": state.get("calls", 0),
            "parents": {key: {"state": value["state"], "leaves": len(value["leaves"]),
                "next_failure":next(({"task_id":leaf["chunk"]["chunk_id"],"state":leaf["state"],"reason":leaf.get("reason"),"attempts":leaf["attempts"]} for leaf in value["leaves"] if leaf["state"] not in ["checkpointed","pending"]),None)} for key,value in state.get("parents", {}).items()}}


def transition(root, state, parent, leaf, value, reason=None):
    leaf["state"] = value
    leaf["reason"] = reason
    state["revision"] += 1
    w.atomic_json(root/"generation-run.json", state)
    # Events contain identities and states, never raw book text or credentials.
    with (root/"generation-events.jsonl").open("a", encoding="utf8") as f:
        f.write(json.dumps({"revision":state["revision"],"parent":parent,"task":leaf["chunk"]["chunk_id"],"state":value,"reason":reason,"at":w.now()})+"\n")


def split(chunk, cap):
    spans = chunk["spans"]
    if len(spans) > 1:
        middle = len(spans)//2
        groups = [spans[:middle],spans[middle:]]
    else:
        source = spans[0]
        cuts = planner.boundaries(source["text"], "sentence") or planner.boundaries(source["text"], "clause")
        cuts = [x for x in cuts if source["text"][:x].strip() and source["text"][x:].strip()]
        if not cuts: raise WorkerFailure("indivisible_source")
        cut = min(cuts,key=lambda x:abs(x-len(source["text"])/2))
        groups = [[{**source,"end":source["start"]+cut,"text":source["text"][:cut]}],
                  [{**source,"start":source["start"]+cut,"text":source["text"][cut:]}]]
    children=[]
    for n, group in enumerate(groups,1):
        notes=[note for note in chunk["footnotes"] if any(s["source_p"]==note["source_p"] and
               (s["start"]<=note["call_offset"]<s["end"] or note["call_offset"]==s["end"]==s["paragraph_length"]) for s in group)]
        child={**chunk,"chunk_id":chunk["chunk_id"]+f"-s{n}","spans":group,"footnotes":notes}
        child["estimated"]=planner.estimate(group,notes,cap)
        children.append(child)
    return children


def invoke(command, packet, timeout, output_limit, max_bytes, stop_file=None):
    """stdin is the only task data; cwd is a fresh empty directory, no shell."""
    with tempfile.TemporaryDirectory(prefix="reading-worker-") as name:
        folder=Path(name)
        request={"worker_protocol":"1.0","task":packet,"max_output_tokens":output_limit}
        (folder/"input.json").write_bytes(w.json_bytes(request))
        with (folder/"input.json").open("rb") as inp, (folder/"output.json").open("wb") as out, (folder/"stderr.txt").open("wb") as err:
            env={**os.environ,"PYTHONIOENCODING":"utf-8"}
            flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=="nt" else 0
            proc=subprocess.Popen(command,stdin=inp,stdout=out,stderr=err,cwd=folder,env=env,shell=False,creationflags=flags,start_new_session=os.name!="nt")
            deadline=time.monotonic()+timeout
            try:
                while proc.poll() is None:
                    if stop_file and Path(stop_file).exists():raise WorkerFailure("user_stopped")
                    if (folder/"output.json").stat().st_size>max_bytes or (folder/"stderr.txt").stat().st_size>max_bytes:
                        raise WorkerFailure("output_overflow")
                    if time.monotonic()>deadline: raise WorkerFailure("timeout")
                    time.sleep(.03)
            finally:
                if proc.poll() is None:
                    if os.name=="nt":
                        subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    else:
                        try:os.killpg(proc.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                    if proc.poll() is None:proc.kill()
                proc.wait()
        if (folder/"output.json").stat().st_size>max_bytes: raise WorkerFailure("output_overflow")
        if proc.returncode: raise WorkerFailure("worker_exit")
        try: result=json.loads((folder/"output.json").read_text("utf8"))
        except (ValueError,UnicodeError): raise WorkerFailure("invalid_output")
        if not isinstance(result,dict): raise WorkerFailure("invalid_output")
        kind=result.get("status","completed")
        if kind not in ["completed","overflow","transient","refused","configuration_error"]: raise WorkerFailure("invalid_output")
        if kind!="completed": raise WorkerFailure({"overflow":"output_overflow"}.get(kind,kind))
        if not isinstance(result.get("usage",{}),dict): raise WorkerFailure("invalid_usage")
        usage=result.get("usage",{}).get("output_tokens",0)
        if type(usage) is not int or usage<0: raise WorkerFailure("invalid_usage")
        if usage>output_limit: raise WorkerFailure("output_overflow")
        return result.get("draft",result)


def run(project, worker_command, max_calls=100, max_seconds=3600, timeout_seconds=180,
        max_output_tokens_total=500000, max_retries=2, max_split_depth=6,
        assemble=True, allow_incomplete_source_review=False, retry_blocked=False, stop_file=None):
    root=Path(project).resolve()
    w.require(isinstance(worker_command,list) and worker_command and all(isinstance(x,str) and x for x in worker_command),"worker_command must be an argument array")
    for value in [max_calls,max_seconds,timeout_seconds,max_output_tokens_total]: w.require(type(value) is int and value>0,"Run limits must be positive integers")
    w.require(type(max_retries) is int and 0<=max_retries<=10 and type(max_split_depth) is int and 0<=max_split_depth<=12,"Invalid recovery limits")
    # One owner for the whole run; individual API writes retain their own locks.
    with w.writer_lock(root/"continuous-run-lock"):
        api.frozen(root); api.approval(root)
        _,_,_,_,plan,_,_=chunks.load(root)
        cap=plan["capacity"]; limit=plan["budget"]["safe_output_tokens"]
        c=api.profile(root)
        state=api.read(root,"generation-run.json",{"schema_version":1,"revision":0,"plan_sha256":plan["plan_sha256"],"parents":{},"calls":0,"status":"running"})
        w.require(state["plan_sha256"]==plan["plan_sha256"],"Run/plan identity changed")
        checkpoint=state.get("short_term_checkpoint")
        if checkpoint:
            data=w.project_file(root,checkpoint["file"]).read_bytes()
            w.require(w.sha(data)==checkpoint["sha256"],"Short-term recovery checkpoint changed")
            short_term.remember(root,checkpoint["chunk"],json.loads(data),c)
        started=time.monotonic();calls=0;reserved=0
        state["status"]="running"
        def finish(value):
            state["status"]=value;w.atomic_json(root/"generation-run.json",state)
            return {**status(root),"run_calls":calls,"reserved_output_tokens":reserved}
        while True:
            if stop_file and Path(stop_file).exists():return finish("stopped")
            api.frozen(root);api.approval(root)
            _,_,_,_,plan,_,accepted=chunks.load(root)
            if len(accepted)==len(plan["chunks"]):
                if assemble:
                    try: api.execute("assemble",root,{"allow_incomplete_source_review":allow_incomplete_source_review})
                    except w.WorkflowError: return finish("export_blocked")
                return finish("assembled" if assemble else "generation_complete")
            parent=plan["chunks"][len(accepted)];ident=parent["chunk_id"]
            entry=state["parents"].setdefault(ident,{"state":"pending","leaves":[{"chunk":parent,"state":"pending","attempts":0,"depth":0}]})
            leaves=entry["leaves"]
            if all(x["state"]=="checkpointed" for x in leaves):
                parts=[]
                for leaf in leaves:
                    data=w.project_file(root,leaf["file"]).read_bytes()
                    w.require(w.sha(data)==leaf["sha256"],"Leaf checkpoint changed")
                    draft=json.loads(data)
                    chunks.validate_draft(draft,leaf["chunk"],plan,c["parameters"]["source_language"])
                    parts.append({"chunk":leaf["chunk"],"draft":draft})
                combined={"chunk_id":ident,"plan_sha256":plan["plan_sha256"],"units":[u for p in parts for u in p["draft"]["units"]],"footnotes_integrated":[n for p in parts for n in p["draft"]["footnotes_integrated"]],"generation_parts":parts}
                api.commit_parts(root,combined)
                entry["state"]="checkpointed";w.atomic_json(root/"generation-run.json",state)
                continue
            leaf=next(x for x in leaves if x["state"]!="checkpointed")
            if leaf["state"]=="blocked":
                if not retry_blocked: return finish("blocked")
                leaf["attempts"]=0;transition(root,state,ident,leaf,"pending","explicit_retry")
            if calls>=max_calls or time.monotonic()-started>=max_seconds or reserved+limit>max_output_tokens_total:
                return finish("budget_exhausted")
            try:
                packet=api.context(root,leaf["chunk"],plan["plan_sha256"])
                input_estimate=planner.tokens(w.json_bytes({"worker_protocol":"1.0","task":packet,"max_output_tokens":limit}).decode(),cap)
                if input_estimate+limit>plan["budget"]["safe_context_tokens"]: raise WorkerFailure("context_overflow")
                cached=root/"worker-results"/(leaf["chunk"]["chunk_id"]+".json")
                if leaf["state"]=="validating" and cached.exists():
                    w.require(w.sha(cached.read_bytes())==leaf["response_sha256"],"Worker result checkpoint changed")
                    if leaf["response_context_sha256"]!=packet["context_sha256"]: raise WorkerFailure("stale_context")
                    draft=w.read_json(cached)
                else:
                    leaf["attempts"]+=1;calls+=1;reserved+=limit;state["calls"]+=1
                    transition(root,state,ident,leaf,"running")
                    draft=invoke(worker_command,packet,min(timeout_seconds,max(1,int(max_seconds-(time.monotonic()-started)))),limit,max(4096,int(limit*cap["chars_per_token"]*8)),stop_file)
                    # Persist complete model output before validation, allowing replay.
                    w.atomic_json(cached,draft)
                    leaf["response_sha256"]=w.sha(cached.read_bytes())
                    leaf["response_context_sha256"]=packet["context_sha256"]
                    transition(root,state,ident,leaf,"validating")
                if api.context(root,leaf["chunk"],plan["plan_sha256"])["context_sha256"]!=packet["context_sha256"]: raise WorkerFailure("stale_context")
                api.validate_language(draft,c)
                if isinstance(draft,dict) and "generation_parts" in draft: raise WorkerFailure("invalid_output")
                checked=chunks.validate_draft(draft,leaf["chunk"],plan,c["parameters"]["source_language"])
                data=w.json_bytes(checked);relative=f"generation-checkpoints/{leaf['chunk']['chunk_id']}--{w.sha(data)[:16]}.json"
                w.atomic_bytes(w.project_file(root,relative),data)
                leaf.update(file=relative,sha256=w.sha(data))
                state["short_term_checkpoint"]={"file":relative,"sha256":w.sha(data),"chunk":leaf["chunk"]}
                transition(root,state,ident,leaf,"checkpointed")
                short_term.remember(root,leaf["chunk"],checked,c)
            except (WorkerFailure,w.WorkflowError,ValueError,KeyError,TypeError,OSError) as error:
                kind=error.kind if isinstance(error,WorkerFailure) else ("configuration_error" if isinstance(error,OSError) else "context_overflow" if "context" in str(error).lower() else "output_overflow" if "safe output budget" in str(error) else "validation_failed")
                transition(root,state,ident,leaf,"failed",kind)
                if kind=="user_stopped":return finish("stopped")
                if kind in ["configuration_error","refused"]:
                    entry["state"]="blocked"
                    transition(root,state,ident,leaf,"blocked",kind);return finish("blocked")
                if kind not in ["output_overflow","context_overflow"] and leaf["attempts"]<=max_retries:
                    transition(root,state,ident,leaf,"retry_wait",kind)
                    if kind in ["transient","timeout","worker_exit"]: time.sleep(min(2**max(0,leaf["attempts"]-1),2,max(0,max_seconds-(time.monotonic()-started))))
                    continue
                try:
                    if leaf["depth"]>=max_split_depth or len(leaves)>=4096: raise WorkerFailure("split_limit")
                    children=split(leaf["chunk"],cap)
                except WorkerFailure as terminal:
                    entry["state"]="blocked"
                    transition(root,state,ident,leaf,"blocked",terminal.kind);return finish("blocked")
                transition(root,state,ident,leaf,"replanned",kind)
                position=leaves.index(leaf)
                leaves[position:position+1]=[{"chunk":child,"state":"pending","attempts":0,"depth":leaf["depth"]+1} for child in children]
                entry["state"]="replanned";w.atomic_json(root/"generation-run.json",state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request",required=True)
    args=parser.parse_args()
    request=w.read_json(args.request)
    print(json.dumps(run(request["project"],**request["parameters"]),indent=2))
if __name__=="__main__": main()
