"""One fresh non-interactive Codex task; no session resume or tool output."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
from openai_worker import INSTRUCTIONS


def parse_events(text, limit):
    try: events=[json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:return {"status":"transient"}
    for e in events:
        item=e.get("item",{})
        if item.get("type") in ["command_execution","mcp_tool_call","web_search","file_change"]:
            return {"status":"refused"}
    done=next((e for e in reversed(events) if e.get("type")=="turn.completed"),None)
    if not done:return {"status":"transient"}
    usage=done.get("usage",{})
    # CLI has no max-output-token flag: this is post-generation validation,
    # not a hard provider billing cap. Use the API worker for that guarantee.
    if usage.get("output_tokens",0)>limit:return {"status":"overflow"}
    messages=[e["item"]["text"] for e in events if e.get("type")=="item.completed" and e.get("item",{}).get("type")=="agent_message"]
    try:draft=json.loads(messages[-1])
    except (ValueError,IndexError):return {"status":"completed","draft":{}}
    if draft.get("overflow") is True:return {"status":"overflow"}
    return {"status":"completed","draft":draft,"usage":usage}


def main():
    request=json.load(sys.stdin)
    prompt=INSTRUCTIONS+'\nUse no tools, files, network or skills. Return only the draft JSON. If the task cannot fit the requested limit, return {"overflow":true}.\n'+json.dumps(request,ensure_ascii=False)
    command=[os.environ.get("READING_CODEX_EXE","codex"),"exec","--ephemeral","--skip-git-repo-check","--ignore-user-config","--sandbox","read-only","--json"]
    if os.environ.get("READING_CODEX_MODEL"):command.extend(["--model",os.environ["READING_CODEX_MODEL"]])
    command.append("-")
    with tempfile.TemporaryDirectory(prefix="codex-text-task-") as folder:
        with open(Path(folder)/"events.jsonl","wb") as out,open(Path(folder)/"stderr.log","wb") as err:
            result=subprocess.run(command,input=prompt.encode("utf8"),stdout=out,stderr=err,cwd=folder,shell=False)
        if result.returncode:return {"status":"configuration_error"}
        path=Path(folder)/"events.jsonl"
        if path.stat().st_size>16*1024*1024:return {"status":"overflow"}
        return parse_events(path.read_text("utf8"),request["max_output_tokens"])


if __name__=="__main__":
    try: result=main()
    except (OSError,ValueError,KeyError,TypeError,AttributeError):result={"status":"configuration_error"}
    print(json.dumps(result,ensure_ascii=False))
