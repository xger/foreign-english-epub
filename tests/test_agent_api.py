import copy, json, tempfile, unittest, zipfile
from pathlib import Path
import test_workflow as base
import agent_api as api
import epub_workflow as w

class SchedulerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.source=base.write_book(self.root/'input.epub',with_note=False);self.project=self.root/'project'
 def op(self,op,**p):return api.execute(op,self.project,p)
 def start(self,target='en'):
  self.op('config.update',patch={'parameters':{'explanation_language':target}})
  self.op('init',source=str(self.source))
 def draft(self,packet):
  units=[]
  for s in packet['chunk']['spans']:
   g=copy.deepcopy(base.GUIDANCE[s['text'].strip()])
   units.append({'source_ps':[s['source_p']],'original':s['text'],'translation_lead':'',**g})
  return {'plan_sha256':packet['plan_sha256'],'chunk_id':packet['task_id'],'units':units,'footnotes_integrated':[]}
 def preview(self):
  self.op('preview.select')
  for category in ['simple','complex','logical']:
   packet=self.op('preview.context',category=category)
   self.assertNotIn('source-index',json.dumps(packet))
   self.op('preview.save',category=category,draft=self.draft(packet))
  record=self.op('preview.export')
  self.assertFalse((self.project/'chunk-plan.json').exists())
  self.assertFalse((self.project/'Guided_Reading.epub').exists())
  return record
 def approve(self,r):return self.op('preview.approve',user_confirmed=True,confirmation='Test user confirms the actual preview.',epub_sha256=r['epub_sha256'])
 def test_preview_calibration_gate_and_target_language(self):
  self.start('zh')
  with self.assertRaises(OSError):self.op('plan')
  r=self.preview()
  with self.assertRaises(w.WorkflowError):self.op('preview.approve',epub_sha256=r['epub_sha256'])
  self.approve(r);self.op('plan')
  while True:
   p=self.op('task.next')
   if p.get('phase')=='ready_to_assemble':break
   self.assertEqual(set(p)-{'context_sha256'},{'task_id','kind','config','consistency_memory','chunk','plan_sha256','instruction'})
   self.op('task.save',draft=self.draft(p),context_sha256=p['context_sha256'])
  self.assertEqual(self.op('status')['completed_paragraphs'],3)
  self.op('plan');self.assertEqual(self.op('status')['completed_paragraphs'],3)
  self.op('assemble');self.assertEqual(self.op('audit')['status'],'PASS')
  for path in [self.project/'Preview.epub',self.project/'Guided_Reading.epub']:
   with zipfile.ZipFile(path) as z:self.assertEqual(w.parse_xml(z.read('OEBPS/content.opf')).get(w.XML_LANG),'zh')
 def test_config_staleness_and_immutable_source(self):
  self.start();self.approve(self.preview())
  self.op('config.update',patch={'parameters':{'language_depth':'brief'}})
  with self.assertRaises(w.WorkflowError):self.op('plan')
  self.approve(self.preview());self.op('plan')
  with self.assertRaises(w.WorkflowError):self.op('config.update',patch={'parameters':{'language_depth':'detailed'}})
  self.source.write_bytes(b'original changed')
  self.assertIn('chunk',self.op('task.next'))
  (self.project/'immutable/source.epub').write_bytes(b'changed snapshot')
  with self.assertRaises(w.WorkflowError):self.op('task.next')
 def test_selective_memory_revision_and_retry(self):
  self.start();self.approve(self.preview());self.op('plan')
  p=self.op('task.next');d=self.draft(p)
  with self.assertRaises(w.WorkflowError):self.op('memory.update',chunk_id=d['chunk_id'],expected_revision=0)
  self.op('task.save',draft=d,context_sha256=p['context_sha256'])
  self.assertFalse((self.project/'consistency-memory.json').exists())
  following=self.op('task.next')
  self.op('memory.update',chunk_id=d['chunk_id'],expected_revision=0,upsert=[{'kind':'term','key':'carnet','value':'notebook'}])
  with self.assertRaises(w.WorkflowError):self.op('task.save',draft=self.draft(following),context_sha256=following['context_sha256'])
  with self.assertRaises(w.WorkflowError):self.op('memory.update',chunk_id=d['chunk_id'],expected_revision=1,summary='x'*3000)
  self.op('task.save',draft=d,context_sha256=p['context_sha256'])
 def test_changed_preview_requires_reexport(self):
  self.start();r=self.preview();path=self.project/'preview-drafts/simple.json'
  d=w.read_json(path);d['units'][0]['thought']=['Revised interpretation.'];w.atomic_json(path,d)
  with self.assertRaises(w.WorkflowError):self.approve(r)
 def test_required_constraints_and_api_version(self):
  with self.assertRaises(w.WorkflowError):self.op('config.update',patch={'validation_constraints':{'exact_source':False}})
  with self.assertRaises(w.WorkflowError):api.dispatch({'api_version':'99','operation':'capabilities'})
  self.assertIn('preview.export',api.dispatch({'api_version':'1.0','operation':'capabilities'})['result']['operations'])
