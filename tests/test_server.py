import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store, BadRequest, Conflict, make_server


def attempt(**overrides):
    value = dict(id=str(uuid.uuid4()), sessionId=str(uuid.uuid4()), questionId='d1',
                 at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                 chosen=0, help=False)
    return value | overrides


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'course.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_hint_and_answer_are_one_attempt_and_retries_are_idempotent(self):
        a = attempt(chosen=None, help=True)
        self.store.save([a], 0)
        self.store.save([a | {'chosen': 0}], 0)
        self.store.save([a], 0)  # delayed retry must not remove the answer
        state = Store(self.path).state()
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['attempts'][0]['chosen'], 0)
        self.assertTrue(state['attempts'][0]['help'])

    def test_concurrent_writes_do_not_overwrite_other_answers(self):
        records = [attempt() for _ in range(20)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda a: self.store.save([a], 0), records + records))
        self.assertEqual(len(self.store.state()['attempts']), 20)

    def test_invalid_import_is_atomic(self):
        a = attempt()
        with self.assertRaises(BadRequest):
            self.store.save([a, attempt(chosen=8)], 0)
        self.assertEqual(self.store.state()['attempts'], [])
        self.store.save([a], 0)
        with self.assertRaises(Conflict):
            self.store.save([attempt(), a | {'chosen': 2}], 0)
        self.assertEqual(len(self.store.state()['attempts']), 1)

    def test_reset_rejects_stale_tabs(self):
        self.store.save([attempt()], 0)
        result = self.store.clear(0)
        self.assertEqual(result['generation'], 1)
        with self.assertRaises(Conflict):
            self.store.save([attempt()], 0)
        self.assertEqual(self.store.state()['attempts'], [])

    def test_real_process_restart_preserves_data(self):
        env = os.environ | {'HOST': '127.0.0.1', 'PORT': '0', 'DB_PATH': str(self.path)}
        def start():
            proc = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve().parents[1] / 'server.py')],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            line = proc.stdout.readline().strip()
            return proc, line.split('http://')[1]
        proc, addr = start()
        req = urllib.request.Request('http://' + addr + '/api/attempts',
            data=json.dumps({'bankVersion':'1','generation':0,'attempts':[attempt()]}).encode(),
            headers={'Content-Type':'application/json','X-Na-Fali':'1'}, method='POST')
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
        proc.terminate(); proc.wait(timeout=5); proc.stdout.close()
        proc, addr = start()
        with urllib.request.urlopen('http://' + addr + '/api/state') as r:
            self.assertEqual(len(json.load(r)['attempts']), 1)
        proc.terminate(); proc.wait(timeout=5); proc.stdout.close()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(port=0, db_path=Path(self.temp.name)/'db.sqlite3')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def req(self, path, data=None, method='GET', **headers):
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data else None,
          method=method, headers={'Content-Type':'application/json', 'X-Na-Fali':'1'} | headers)
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_serves_page_and_health_but_never_database(self):
        self.assertEqual(self.req('/')[0], 200)
        self.assertIn(b'const DATA=', self.req('/')[1])
        self.assertEqual(self.req('/api/health')[0], 200)
        self.assertEqual(self.req('/data/course.sqlite3')[0], 404)

    def test_rejects_cross_origin_and_invalid_answers(self):
        body = dict(bankVersion='1', generation=0, attempts=[attempt()])
        self.assertEqual(self.req('/api/attempts', body, 'POST', Origin='https://example.org')[0], 403)
        self.assertEqual(self.req('/api/attempts', body, 'POST', Host='evil.example')[0], 403)
        body['attempts'][0]['chosen'] = True
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 400)
        self.assertEqual(json.loads(self.req('/api/state')[1])['attempts'], [])

    def test_export_import_and_reset(self):
        a = attempt()
        body = dict(bankVersion='1', generation=0, attempts=[a])
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 200)
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 200)
        self.assertEqual(len(json.loads(self.req('/api/state')[1])['attempts']), 1)
        self.assertEqual(self.req('/api/history', dict(bankVersion='1', generation=0), 'DELETE')[0], 200)
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 409)

if __name__ == '__main__':
    unittest.main()
