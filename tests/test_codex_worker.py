import importlib.util, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agents'))
import codex_worker

class CodexAdapterTests(unittest.TestCase):
 def events(self,usage=5,extra=None):
  data=[{'type':'item.completed','item':{'type':'agent_message','text':'{"units":[]}'}},{'type':'turn.completed','usage':{'output_tokens':usage}}]
  if extra:data.insert(0,{'type':'item.completed','item':{'type':extra}})
  return '\n'.join(json.dumps(x) for x in data)
 def test_parse_and_overflow(self):
  self.assertEqual(codex_worker.parse_events(self.events(),10)['status'],'completed')
  self.assertEqual(codex_worker.parse_events(self.events(20),10)['status'],'overflow')
 def test_reject_tool_use(self):self.assertEqual(codex_worker.parse_events(self.events(extra='command_execution'),10)['status'],'refused')
