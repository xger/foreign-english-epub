import importlib.util, io, json, os, unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('reading_openai_worker',Path(__file__).resolve().parents[1]/'agents/openai_worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

class AdapterTests(unittest.TestCase):
 def test_fresh_bounded_requests_and_usage(self):
  captured=[]
  def transport(req,timeout):
   captured.append(json.loads(req.data))
   return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"units":[]}'}]}],'usage':{'output_tokens':50}}).encode())
  with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key','READING_MODEL':'test-model'}):
   for i in range(2):self.assertEqual(worker.generate({'task':{'task_id':str(i)},'max_output_tokens':100},transport)['status'],'completed')
  self.assertEqual(len(captured),2)
  for p in captured:
   self.assertFalse(p['store']);self.assertNotIn('previous_response_id',p);self.assertEqual(p['max_output_tokens'],100);self.assertNotIn('tools',p)
 def test_truncation_and_missing_credentials(self):
  def transport(req,timeout):return io.BytesIO(b'{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}')
  with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key','READING_MODEL':'test-model'}):
   self.assertEqual(worker.generate({'task':{},'max_output_tokens':100},transport)['status'],'overflow')
  with patch.dict(os.environ,{},clear=True):self.assertEqual(worker.generate({},transport)['status'],'configuration_error')
 def test_provider_context_error_is_replannable(self):
  def transport(req,timeout):raise urllib.error.HTTPError(req.full_url,400,'Bad request',{},io.BytesIO(b'{"error":{"code":"context_length_exceeded"}}'))
  with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key','READING_MODEL':'test-model'}):
   self.assertEqual(worker.generate({'task':{},'max_output_tokens':100},transport)['status'],'overflow')
