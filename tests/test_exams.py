import copy,time,tempfile,unittest
from pathlib import Path
from server import BANK,Store,Conflict
from exam_validation import validate_workspace

def workspace():
    now=int(time.time()*1000)
    return {'history':[],'active':{'id':'exam-123456','contentRevision':'3.1.0','status':'active','startedAt':now,'finishedAt':None,'blockIndex':0,'deadline':now+480000,'blocks':[{'subject':s,'questions':[{'id':q['id'],'order':[2,0,1],'chosen':None} for q in BANK['questions'] if q.get('examSubject')==s and q.get('examEligible')][:8],'startedAt':now if i==0 else None,'endedAt':None,'reason':None} for i,s in enumerate(['radio','safety','operators','law'])]}}
class ExamTests(unittest.TestCase):
    def test_new_deadline_and_round_trip_after_restart(self):
        w=workspace();validate_workspace(w)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'db.sqlite3';s=Store(p)
            self.assertEqual(s.exams()['revision'],0)
            s.exams(w,0)
            self.assertEqual(Store(p).exams(),{'revision':1,'workspace':w})
            with self.assertRaises(Conflict):s.exams(w,0)
    def test_reject_invalid_states_and_choices(self):
        w=workspace()
        for mutate in [lambda r:r.update(deadline=r['deadline']+1),lambda r:r['blocks'][0]['questions'][0].update(chosen=True),lambda r:r['blocks'][0]['questions'][0].update(order=[0,0,1]),lambda r:r['blocks'][1]['questions'][0].update(chosen=0),lambda r:r['blocks'][0].update(subject='law'),lambda r:r.update(status='complete')]:
            v=copy.deepcopy(w);mutate(v['active'])
            with self.assertRaises(ValueError):validate_workspace(v)
