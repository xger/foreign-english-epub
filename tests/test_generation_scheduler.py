import json, sys, unittest
from pathlib import Path
from unittest.mock import patch
from test_agent_api import SchedulerTests
import agent_api as api
import generation_scheduler as scheduler
import epub_workflow as w
import chunk_pipeline as chunks
import plan_chunks as planner

class ContinuousTests(SchedulerTests):
 # Inherit fixture helpers, not the parent's test cases, below.
 def ready(self):self.start();self.approve(self.preview());self.op('plan')
 def command(self,mode):return [sys.executable,str(Path(__file__).with_name('mock_generation_worker.py').resolve()),mode,str(self.root/'worker-log.jsonl')]
 def logs(self):return [json.loads(x) for x in (self.root/'worker-log.jsonl').read_text().splitlines()]
 def test_one_launch_many_fresh_tasks_and_final_book(self):
  self.ready();r=scheduler.run(self.project,self.command('normal'))
  self.assertEqual(r['status'],'assembled');self.assertEqual(r['run_calls'],2)
  log=self.logs();self.assertEqual(len({x['pid'] for x in log}),2);self.assertFalse(any(x['cwd_has_book'] for x in log))
  self.assertTrue(log[1]['context']['previous_guidance']);self.assertTrue(log[1]['context']['before'])
  self.assertEqual(self.op('audit')['status'],'PASS')
 def test_local_split_aggregate_over_single_call_limit_and_resume(self):
  self.ready();plan=(self.project/'chunk-plan.json').read_bytes()
  r=scheduler.run(self.project,self.command('split'),max_calls=2)
  self.assertEqual(r['status'],'budget_exhausted')
  ledger=w.read_json(self.project/'generation-run.json');self.assertEqual(ledger['parents']['chunk-000001']['leaves'][0]['state'],'checkpointed')
  files=list((self.project/'generation-checkpoints').glob('*.json'));before={x.name:x.read_bytes() for x in files}
  r=scheduler.run(self.project,self.command('split'));self.assertEqual(r['status'],'assembled')
  self.assertEqual((self.project/'chunk-plan.json').read_bytes(),plan)
  for name,data in before.items():self.assertEqual((self.project/'generation-checkpoints'/name).read_bytes(),data)
  self.assertEqual(self.op('status')['completed_paragraphs'],3)
  _,_,_,_,p,_,drafts=chunks.load(self.project)
  self.assertGreater(planner.tokens(w.json_bytes(drafts[0]).decode(),p['capacity']),p['budget']['safe_output_tokens'])
  self.assertEqual(self.op('audit')['status'],'PASS')
 def test_transient_retry_is_bounded(self):
  self.ready();r=scheduler.run(self.project,self.command('transient'),max_calls=1)
  self.assertEqual(r['status'],'budget_exhausted')
  r=scheduler.run(self.project,self.command('transient'));self.assertEqual(r['status'],'assembled')
  self.assertEqual(len(self.logs()),3)
 def test_indivisible_overflow_blocks_and_does_not_publish(self):
  self.ready();r=scheduler.run(self.project,self.command('overflow'))
  self.assertEqual(r['status'],'blocked');self.assertFalse((self.project/'Guided_Reading.epub').exists())
  count=len(self.logs());self.assertEqual(scheduler.run(self.project,self.command('normal'))['status'],'blocked');self.assertEqual(len(self.logs()),count)
  self.assertEqual(scheduler.run(self.project,self.command('normal'),retry_blocked=True)['status'],'assembled')
 def test_timeout_and_refusal_stop_with_limits(self):
  self.ready();r=scheduler.run(self.project,self.command('timeout'),timeout_seconds=1,max_calls=1)
  self.assertEqual(r['status'],'budget_exhausted');self.assertEqual(r['run_calls'],1)
  self.assertEqual(scheduler.run(self.project,self.command('refused'))['status'],'blocked')
 def test_output_budget_and_short_term_have_separate_limits(self):
  self.ready();r=scheduler.run(self.project,self.command('normal'),max_output_tokens_total=1)
  self.assertEqual(r['run_calls'],0)
  packet=self.op('task.next');cap=packet['config']['parameters']['capacity']
  self.assertLessEqual(planner.tokens(w.json_bytes(packet['short_term_memory']).decode(),cap),packet['context_budget']['short_term_reserved_tokens'])
 def test_cached_response_replays_after_validation_interruption(self):
  self.ready();real=chunks.validate_draft
  def interrupt(draft,*args):
   if (self.project/'worker-results/chunk-000001.json').exists(): raise KeyboardInterrupt()
   return real(draft,*args)
  with patch.object(chunks,'validate_draft',interrupt):
   with self.assertRaises(KeyboardInterrupt):scheduler.run(self.project,self.command('normal'))
  self.assertEqual(scheduler.run(self.project,self.command('normal'))['status'],'assembled')
  self.assertEqual(len(self.logs()),2)
 def test_short_term_recovers_after_checkpoint_interruption(self):
  self.ready()
  import short_term
  with patch.object(short_term,'remember',side_effect=KeyboardInterrupt()):
   with self.assertRaises(KeyboardInterrupt):scheduler.run(self.project,self.command('normal'))
  self.assertEqual(scheduler.run(self.project,self.command('normal'))['status'],'assembled')
  self.assertEqual(len(self.logs()),2)
  self.assertTrue(self.logs()[1]['context']['previous_guidance'])
 def test_model_cannot_self_declare_aggregate_budget_exemption(self):
  self.ready();packet=self.op('task.next');draft=self.draft(packet);draft['generation_parts']=[]
  with self.assertRaises(w.WorkflowError):self.op('task.save',draft=draft,context_sha256=packet['context_sha256'])

# Avoid re-running inherited test methods: they remain covered by test_agent_api.
for name in list(SchedulerTests.__dict__):
 if name.startswith('test_'):setattr(ContinuousTests,name,None)
del SchedulerTests
