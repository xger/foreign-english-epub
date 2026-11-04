"""Original structural fixture: never a production translation adapter."""
import json, os, sys, time
from pathlib import Path

mode,log=sys.argv[1:3]
request=json.load(sys.stdin);packet=request["task"]
path=Path(log);count=len(path.read_text().splitlines()) if path.exists() else 0
with path.open("a",encoding="utf8") as f:
 f.write(json.dumps({"pid":os.getpid(),"task":packet["task_id"],"context":packet.get("short_term_memory"),"cwd_has_book":Path("source-index.json").exists()})+"\n")
if mode=="timeout":time.sleep(5)
if mode=="transient" and count==0:
 print(json.dumps({"status":"transient"}));raise SystemExit(0)
if mode=="overflow" or mode=="split" and len(packet["chunk"]["spans"])>1:
 print(json.dumps({"status":"overflow"}));raise SystemExit(0)
if mode=="invalid": print('{');raise SystemExit(0)
if mode=="refused":print(json.dumps({"status":"refused"}));raise SystemExit(0)
units=[]
for span in packet["chunk"]["spans"]:
 if not span["text"].strip():continue
 units.append({"source_ps":[span["source_p"]],"original":span["text"],"language":["Structural grammar fixture. "*(330 if mode=="split" else 2)],"translation":"A structural test rendering.","translation_lead":"","thought":["This is a persistence and coverage test, not a translation-quality example."]})
draft={"chunk_id":packet["task_id"],"plan_sha256":packet["plan_sha256"],"units":units,"footnotes_integrated":[n["id"] for n in packet["chunk"]["footnotes"]]}
print(json.dumps({"status":"completed","draft":draft,"usage":{"output_tokens":1}}))
