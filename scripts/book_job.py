"""Detached book supervisor: renew small runs within one durable total grant."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, uuid, threading
from pathlib import Path
import agent_api as api
import epub_workflow as w
import generation_scheduler as scheduler
import chunk_pipeline as chunks

TERMINAL = {"assembled","blocked","export_blocked","stopped","total_budget_exhausted","failed","interrupted"}


def save_state(root, state):
    # Windows readers can briefly deny rename/delete sharing while polling.
    # Retry that transient conflict; never truncate the live state in place.
    for attempt in range(12):
        try:
            w.atomic_json(root/"book-job.json",state)
            return
        except PermissionError:
            if os.name!="nt" or attempt==11:raise
            time.sleep(min(.01*2**attempt,.1))


def status(project):
    root=Path(project).resolve()
    state=api.read(root,"book-job.json",{"status":"not_started"})
    launch=api.read(root,"book-job-launch.json",{})
    pid=launch.get("pid") if launch.get("job_id")==state.get("job_id") else None
    alive=bool(pid and w.process_alive(pid))
    if pid and not alive and state.get("status") in ["queued","running"]:state["status"]="interrupted"
    return {**state,"pid":pid,"process_alive":alive}


def start(project, worker_command, total_budget, batch=None):
    root=Path(project).resolve()
    api.frozen(root);api.approval(root);chunks.load(root)
    for key in ["max_calls","max_seconds","max_output_tokens"]:
        w.require(type(total_budget.get(key)) is int and total_budget[key]>0,"Explicit positive whole-book budget required")
    w.require(isinstance(worker_command,list) and worker_command and all(isinstance(x,str) and x for x in worker_command),"Worker command must be an argument array")
    settings={"max_calls":20,"max_seconds":600,"timeout_seconds":180,"max_retries":2,"max_split_depth":6,**(batch or {})}
    w.require(set(settings)<={"max_calls","max_seconds","timeout_seconds","max_retries","max_split_depth"},"Unknown batch setting")
    for key in ["max_calls","max_seconds","timeout_seconds"]:w.require(type(settings[key]) is int and settings[key]>0,"Invalid batch limit")
    with w.writer_lock(root/"book-job-launch-lock"):
        old=status(root)
        w.require(not old.get("process_alive"),"A book job is already running")
        # A deliberate new start grants a new finite total budget; chunk retry
        # and coverage checkpoints are retained by the underlying scheduler.
        ident=uuid.uuid4().hex
        request={"project":str(root),"worker_command":worker_command,"total_budget":total_budget,"batch":settings,"job_id":ident}
        w.atomic_json(root/"book-job-request.json",request)
        w.atomic_json(root/"book-job.json",{"job_id":ident,"status":"queued","request_sha256":api.digest(request),"budget":total_budget,"reserved":{"calls":0,"seconds":0,"output_tokens":0},"batches":0})
        (root/"book-job.stop").unlink(missing_ok=True)
        flags=(subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW) if os.name=="nt" else 0
        with (root/"book-job.log").open("ab") as out:
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--project",str(root),"--run"],
                stdin=subprocess.DEVNULL,stdout=out,stderr=out,cwd=root,close_fds=True,
                creationflags=flags,start_new_session=os.name!="nt",env={**os.environ,"PYTHONIOENCODING":"utf-8"})
        w.atomic_json(root/"book-job-launch.json",{"job_id":ident,"pid":process.pid})
        # Reap while the launcher host remains alive, without tying the child
        # lifetime to that host or keeping the caller waiting.
        threading.Thread(target=process.wait,daemon=True).start()
        return {"job_id":ident,"pid":process.pid,"status":"submitted","project":str(root),"log":str(root/"book-job.log")}


def stop(project):
    root=Path(project).resolve()
    w.atomic_bytes(root/"book-job.stop",b"stop requested\n")
    return {"status":"stop_requested"}


def run(project):
    root=Path(project).resolve()
    with w.writer_lock(root/"book-job-owner"):
        request=w.read_json(root/"book-job-request.json");state=w.read_json(root/"book-job.json")
        w.require(api.digest(request)==state["request_sha256"],"Job grant changed")
        budget=request["total_budget"];batch=request["batch"];used=state["reserved"]
        _,_,_,_,plan,_,_=chunks.load(root)
        per_call=plan["budget"]["safe_output_tokens"]
        def finish(value):
            state["status"]=value;state["finished_at"]=w.now();save_state(root,state)
            return state
        try:
            while True:
                if (root/"book-job.stop").exists():return finish("stopped")
                _,_,_,_,current_plan,_,accepted=chunks.load(root)
                if len(accepted)==len(current_plan["chunks"]):
                    try:api.execute("assemble",root)
                    except w.WorkflowError:return finish("export_blocked")
                    return finish("assembled")
                remaining={k:budget["max_"+k]-used[k] for k in used}
                count=min(batch["max_calls"],remaining["calls"],remaining["output_tokens"]//per_call)
                seconds=min(batch["max_seconds"],remaining["seconds"])
                if count<1 or seconds<1:return finish("total_budget_exhausted")
                # Reserve the entire next batch before dispatch. A crash cannot
                # silently reset permission or grant itself fresh budget.
                used["calls"]+=count;used["seconds"]+=seconds;used["output_tokens"]+=count*per_call
                state.update(status="running",batches=state["batches"]+1)
                save_state(root,state)
                began=time.monotonic()
                result=scheduler.run(root,request["worker_command"],max_calls=count,max_seconds=seconds,
                    timeout_seconds=min(batch["timeout_seconds"],seconds),max_output_tokens_total=count*per_call,
                    max_retries=batch["max_retries"],max_split_depth=batch["max_split_depth"],assemble=True,
                    stop_file=str(root/"book-job.stop"))
                elapsed=min(seconds,max(1,int(time.monotonic()-began+.999)))
                used["calls"]-=count-result["run_calls"]
                used["output_tokens"]-=count*per_call-result["reserved_output_tokens"]
                used["seconds"]-=seconds-elapsed
                state["last_batch_status"]=result["status"]
                save_state(root,state)
                if result["status"]!="budget_exhausted":return finish(result["status"])
                # Batch exhaustion is an internal boundary, not a new user turn.
        except Exception as error:
            state["error_type"]=type(error).__name__
            state["error_message"]=str(error)[:500]
            return finish("failed")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",required=True);parser.add_argument("--run",action="store_true")
    args=parser.parse_args();print(json.dumps(run(args.project) if args.run else status(args.project)))
if __name__=="__main__":main()
