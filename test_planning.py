import importlib,sys,types,unittest,copy
from pathlib import Path
from datetime import date,timedelta
root=Path(__file__).resolve().parent/"custom_components/household_planner"
# Candidate engine uses unchanged production supporting modules.
pkg=types.ModuleType('candidate');pkg.__path__=[str(root)];sys.modules['candidate']=pkg
p=importlib.import_module('candidate.planning')
class Smoothing(unittest.TestCase):
 def entry(self,i,offset=1,**kw):
  e=dict(task_id=str(i),occurrence_id=str(i),weight=2,area='',contains_vacuuming=False,canonical_task_type='unknown',_due_day=date(2026,10,15)+timedelta(days=offset),_smooth_eligible=True,_fixed=False,_projected=False);e.update(kw);return e
 def test_balances_and_preserves_due(self):
  entries=[self.entry(i) for i in range(4)];before=copy.deepcopy(entries)
  week,_=p.schedule(date(2026,10,15),[8]*7,entries)
  self.assertEqual([sum(e['weight'] for e in d) for d in week][:2],[4,4]);self.assertEqual(entries,before)
 def test_disabled(self):
  week,_=p.schedule(date(2026,10,15),[8]*7,[self.entry(i) for i in range(4)],smoothing=False);self.assertEqual(week[0],[])
 def test_exemptions(self):
  entries=[self.entry(1,_fixed=True),self.entry(2,_projected=True),self.entry(3,_smooth_eligible=False),self.entry(4,3)]
  week,_=p.schedule(date(2026,10,15),[20]*7,entries);self.assertEqual(week[0],[])
 def test_preserved_and_zero(self):
  keep=self.entry('done',0); week,_=p.schedule(date(2026,10,15),[0]+[8]*6,[self.entry(i) for i in range(4)],[keep]);self.assertEqual(week[0],[keep])
 def test_sunday(self):
  sun=date(2026,10,18);entries=[self.entry(i,_due_day=sun+timedelta(days=1),contains_vacuuming=True,canonical_task_type='vacuum') for i in range(4)]
  week,_=p.schedule(sun,[8]*7,entries);self.assertEqual(week[0],[])
if __name__=='__main__':unittest.main()
