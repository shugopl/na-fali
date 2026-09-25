import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import BANK, Store
ROOT=Path(__file__).resolve().parents[1]

class ContentTests(unittest.TestCase):
    def test_existing_answer_keys_and_question_identities_are_unchanged(self):
        old=json.loads((ROOT/'tests/fixtures/v2-question-hashes.json').read_text())
        current={q['id']:q for q in BANK['questions']}
        self.assertEqual(len(old),116)
        for id, fingerprint in old.items():
            q=current[id]
            actual=hashlib.sha256(json.dumps({k:q[k] for k in ['q','options','answer','module','section']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(actual,fingerprint,id)

    def test_complete_source_coverage_and_corrections(self):
        self.assertEqual(len(BANK['qCatalog']),75)
        self.assertEqual(len(BANK['bandDetails']),11)
        self.assertEqual(len(BANK['alphabet']),26)
        self.assertEqual(sum(len(d['regions']) for d in BANK['districts']),16)
        self.assertEqual(len({d['prefix'] for d in BANK['districts']}),9)
        qsa=next(x for x in BANK['qCatalog'] if x['code']=='QSA')
        self.assertIn('1–5',qsa['meaning'])
        self.assertIn('1...9',qsa['sourceText'])
        band=next(x for x in BANK['bandDetails'] if x['band']=='30 m')
        self.assertEqual(band['sourceMode'],'LSB')
        self.assertIn('CW',band['teachingMode'])
        self.assertEqual(BANK['alphabet'][0]['word'],'Alfa')
        self.assertTrue(all(len(q['options'])==3 and len(set(q['options']))==3 for q in BANK['questions']))
        self.assertEqual(len(BANK['questions']),297)

    def test_real_v1_database_migrates_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'course.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript('''CREATE TABLE meta(id INTEGER PRIMARY KEY, generation INTEGER NOT NULL);
                INSERT INTO meta VALUES(1,7);
                CREATE TABLE attempts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,question_id TEXT NOT NULL,at TEXT NOT NULL,chosen INTEGER,help INTEGER NOT NULL);
                INSERT INTO attempts VALUES('old-attempt','old-session','d1','2026-01-01T00:00:00.000Z',0,0);
                PRAGMA user_version=1;''')
            store=Store(path)
            uid=store.find_user('#legacy')['id']   # wspolna historia v1/v2 laduje na koncie zastepczym
            self.assertEqual(store.state(uid)['generation'],7)
            self.assertEqual(store.state(uid)['attempts'][0]['id'],'old-attempt')
            catalog=store.catalog()['items']
            self.assertEqual(sum(x['kind']=='question' for x in catalog),297)
            self.assertEqual(sum(x['kind']=='q_code' for x in catalog),75)
            self.assertEqual(sum(x['kind']=='band' for x in catalog),11)
            self.assertEqual(Store(path).catalog(),store.catalog())
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)
