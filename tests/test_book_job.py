import json, subprocess, sys, time
from pathlib import Path
from test_agent_api import SchedulerTests
import book_job
import agent_api as api
import epub_workflow as w

class BookJobTests(SchedulerTests):
 def ready(self):self.start();self.approve(self.preview());self.op('plan')
 def command(self,mode='normal'):return [sys.executable,str(Path(__file__).with_name('mock_generation_worker.py').resolve()),mode,str(self.root/'calls.jsonl')]
 def wait_job(self,timeout=20):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   state=book_job.status(self.project)
   if state['status'] in book_job.TERMINAL and not state['process_alive']:return state
   time.sleep(.05)
  self.fail('detached job did not finish: '+str(state)+' log='+str((self.project/'book-job.log').read_text('utf8')))
 def test_launcher_process_exits_but_supervisor_renews_batches(self):
  self.ready();request={'api_version':'1.0','operation':'job.start','project':str(self.project),'parameters':{'worker_command':self.command(),'total_budget':{'max_calls':10,'max_seconds':60,'max_output_tokens':100000},'batch':{'max_calls':1,'max_seconds':10}}}
  cli=Path(api.__file__).resolve()
  launched=subprocess.run([sys.executable,str(cli),'--request','-'],input=json.dumps(request),text=True,capture_output=True,encoding='utf8',timeout=10)
  self.assertEqual(launched.returncode,0,launched.stdout)
  state=self.wait_job();self.assertEqual(state['status'],'assembled',state);self.assertGreaterEqual(state['batches'],2)
  self.assertEqual(state['reserved']['calls'],2);self.assertEqual(self.op('audit')['status'],'PASS')
 def test_total_grant_exhaustion_does_not_renew_forever(self):
  self.ready();book_job.start(self.project,self.command(),{'max_calls':1,'max_seconds':30,'max_output_tokens':100000},{'max_calls':1})
  state=self.wait_job();self.assertEqual(state['status'],'total_budget_exhausted',state);self.assertFalse((self.project/'Guided_Reading.epub').exists())
 def test_stop_during_worker_and_duplicate_launch(self):
  self.ready();book_job.start(self.project,self.command('timeout'),{'max_calls':5,'max_seconds':60,'max_output_tokens':100000})
  end=time.monotonic()+10
  while not (self.root/'calls.jsonl').exists() and time.monotonic()<end:time.sleep(.05)
  with self.assertRaises(w.WorkflowError):book_job.start(self.project,self.command(),{'max_calls':5,'max_seconds':60,'max_output_tokens':100000})
  book_job.stop(self.project);self.assertEqual(self.wait_job()['status'],'stopped')

for name in list(SchedulerTests.__dict__):
 if name.startswith('test_'):setattr(BookJobTests,name,None)
del SchedulerTests
