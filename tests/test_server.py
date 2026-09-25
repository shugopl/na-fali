import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
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
import server
from server import Store, BadRequest, Conflict, Unauthorized, LEGACY_USER, make_server

USER = ('alice', 'password123')


def attempt(**overrides):
    value = dict(id=str(uuid.uuid4()), sessionId=str(uuid.uuid4()), questionId='d1',
                 at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                 chosen=0, help=False)
    return value | overrides


def cookie_from(headers):
    """Wartosc cookie sesji z Set-Cookie albo None, gdy naglowek ja kasuje."""
    raw = headers.get('Set-Cookie')
    if raw is None:
        return None, None
    value = raw.split(';', 1)[0].split('=', 1)[1]
    return (value or None), raw


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'course.sqlite3'
        self.store = Store(self.path)
        self.uid = self.store.register(*USER)['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_hint_and_answer_are_one_attempt_and_retries_are_idempotent(self):
        a = attempt(chosen=None, help=True)
        self.store.save(self.uid, [a], 0)
        self.store.save(self.uid, [a | {'chosen': 0}], 0)
        self.store.save(self.uid, [a], 0)  # delayed retry must not remove the answer
        state = Store(self.path).state(self.uid)
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['attempts'][0]['chosen'], 0)
        self.assertTrue(state['attempts'][0]['help'])

    def test_concurrent_writes_do_not_overwrite_other_answers(self):
        records = [attempt() for _ in range(20)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda a: self.store.save(self.uid, [a], 0), records + records))
        self.assertEqual(len(self.store.state(self.uid)['attempts']), 20)

    def test_invalid_import_is_atomic(self):
        a = attempt()
        with self.assertRaises(BadRequest):
            self.store.save(self.uid, [a, attempt(chosen=8)], 0)
        self.assertEqual(self.store.state(self.uid)['attempts'], [])
        self.store.save(self.uid, [a], 0)
        with self.assertRaises(Conflict):
            self.store.save(self.uid, [attempt(), a | {'chosen': 2}], 0)
        self.assertEqual(len(self.store.state(self.uid)['attempts']), 1)

    def test_reset_rejects_stale_tabs(self):
        self.store.save(self.uid, [attempt()], 0)
        result = self.store.clear(self.uid, 0)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(result['schemaVersion'], 2)   # klient robi acceptState() na wyniku
        self.assertEqual(result['attempts'], [])
        with self.assertRaises(Conflict):
            self.store.save(self.uid, [attempt()], 0)
        self.assertEqual(self.store.state(self.uid)['attempts'], [])

    def test_state_matches_page_contract_and_exams_start_empty(self):
        state = self.store.state(self.uid)
        self.assertEqual(state['schemaVersion'], 2)
        self.assertEqual(state['bankVersion'], server.BANK_VERSION)
        self.assertEqual(server.BANK_VERSION, '1')
        self.assertEqual(self.store.exams(self.uid),
                         {'revision': 0, 'workspace': {'active': None, 'history': []}})

    def test_accounts_are_isolated(self):
        bob = self.store.register('bob', 'password123')['id']
        a = attempt()
        self.store.save(self.uid, [a], 0)
        self.store.clear(self.uid, 0)
        self.assertEqual(self.store.state(bob), {'schemaVersion': 2, 'bankVersion': '1',
                                                 'generation': 0, 'attempts': []})
        self.store.save(bob, [a | {'chosen': 1}], 0)      # to samo id u innego konta: bez konfliktu
        self.assertEqual(self.store.state(bob)['attempts'][0]['chosen'], 1)
        self.assertEqual(self.store.state(self.uid)['attempts'], [])

    def test_register_login_and_sessions(self):
        for name in ('ab', 'a b', "x'y", 'x' * 33, None):
            with self.assertRaises(BadRequest):
                self.store.register(name, 'password123')
        with self.assertRaises(BadRequest):
            self.store.register('carol', 'short')
        with self.assertRaises(Conflict):
            self.store.register('Alice', 'password123')      # bez rozrozniania wielkosci liter
        self.assertEqual(self.store.login('ALICE', 'password123')['id'], self.uid)
        with self.assertRaises(Unauthorized):
            self.store.login('alice', 'password124')
        with self.assertRaises(Unauthorized):
            self.store.login('nobody', 'password123')
        with self.assertRaises(BadRequest):
            self.store.register('dave', 'password123', None, 'tajne')
        self.assertEqual(self.store.register('dave', 'password123', 'tajne', 'tajne')['username'], 'dave')

        token = self.store.create_session(self.uid)
        self.assertEqual(Store(self.path).session_user(token)['username'], 'alice')
        self.assertIsNone(self.store.session_user('nie-ten-token'))
        self.store.delete_session(token)
        self.assertIsNone(self.store.session_user(token))

    def test_v2_database_migrates_to_v3_keeping_legacy_rows(self):
        path = Path(self.temp.name) / 'v2.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE attempts(id TEXT PRIMARY KEY, sessionId TEXT NOT NULL, questionId TEXT NOT NULL,
                    at TEXT NOT NULL, chosen INTEGER, help INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE exams(id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL, workspace TEXT);
                CREATE TABLE catalog(position INTEGER PRIMARY KEY, kind TEXT NOT NULL, id TEXT NOT NULL,
                    title TEXT NOT NULL, text TEXT NOT NULL DEFAULT '');
                INSERT INTO attempts VALUES('old-attempt','old-session','d1','2026-01-01T00:00:00.000Z',1,0);
                INSERT INTO meta VALUES('generation','3');
                INSERT INTO exams VALUES(1, 2, '{"active": null, "history": []}');
                PRAGMA user_version=2;''')
        store = Store(path)
        legacy = store.find_user(LEGACY_USER)
        self.assertEqual(legacy['generation'], 3)
        self.assertEqual(store.state(legacy['id'])['attempts'][0]['id'], 'old-attempt')
        self.assertEqual(store.exams(legacy['id'])['revision'], 2)
        with self.assertRaises(Unauthorized):
            store.login(LEGACY_USER, '!')
        fresh = store.register('erin', 'password123')['id']
        self.assertEqual(store.state(fresh)['attempts'], [])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone())

    def test_empty_v2_database_gets_no_legacy_user(self):
        path = Path(self.temp.name) / 'empty-v2.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE attempts(id TEXT PRIMARY KEY, sessionId TEXT NOT NULL, questionId TEXT NOT NULL,
                    at TEXT NOT NULL, chosen INTEGER, help INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE exams(id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL, workspace TEXT);
                PRAGMA user_version=2;''')
        store = Store(path)
        self.assertIsNone(store.find_user(LEGACY_USER))
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_real_process_restart_preserves_data(self):
        env = os.environ | {'HOST': '127.0.0.1', 'PORT': '0', 'DB_PATH': str(self.path)}
        def start():
            proc = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve().parents[1] / 'server.py')],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            line = proc.stdout.readline().strip()
            return proc, line.split('http://')[1]
        def call(addr, path, data=None, method='GET', cookie=None):
            headers = {'Content-Type': 'application/json', 'X-Na-Fali': '1'}
            if cookie:
                headers['Cookie'] = f'{server.COOKIE}={cookie}'
            req = urllib.request.Request('http://' + addr + path, method=method, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r), r.headers
        proc, addr = start()
        status, _, headers = call(addr, '/api/register', {'username': 'bob', 'password': 'password123'}, 'POST')
        self.assertEqual(status, 200)
        cookie, _ = cookie_from(headers)
        status, _, _ = call(addr, '/api/attempts',
                            {'bankVersion': '1', 'generation': 0, 'attempts': [attempt()]}, 'POST', cookie)
        self.assertEqual(status, 200)
        proc.terminate(); proc.wait(timeout=5); proc.stdout.close()
        proc, addr = start()
        status, state, _ = call(addr, '/api/state', cookie=cookie)      # sesja przezywa restart
        self.assertEqual(len(state['attempts']), 1)
        proc.terminate(); proc.wait(timeout=5); proc.stdout.close()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(port=0, db_path=Path(self.temp.name)/'db.sqlite3')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.cookie = None

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def req(self, path, data=None, method='GET', **headers):
        base = {'Content-Type': 'application/json', 'X-Na-Fali': '1'}
        if self.cookie:
            base['Cookie'] = f'{server.COOKIE}={self.cookie}'
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
          method=method, headers=base | headers)
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if 'Set-Cookie' in response.headers:
                self.cookie, _ = cookie_from(response.headers)
            return response.status, response.read(), response.headers

    def register(self, name='alice', password='password123', **extra):
        return self.req('/api/register', dict(username=name, password=password, **extra), 'POST')

    def test_serves_page_and_health_but_never_database(self):
        self.assertEqual(self.req('/')[0], 200)
        self.assertIn(b'const DATA=', self.req('/')[1])
        self.assertEqual(self.req('/api/health')[0], 200)
        self.assertEqual(self.req('/data/course.sqlite3')[0], 404)

    def test_data_routes_require_login(self):
        for path, data, method in [('/api/state', None, 'GET'), ('/api/exams', None, 'GET'),
                                   ('/api/attempts', {'bankVersion': '1', 'generation': 0, 'attempts': []}, 'POST'),
                                   ('/api/exams', {'bankVersion': '1', 'revision': 0, 'workspace': {}}, 'POST'),
                                   ('/api/history', {'bankVersion': '1', 'generation': 0}, 'DELETE')]:
            status, body, _ = self.req(path, data, method)
            self.assertEqual(status, 401, path)
            self.assertIn('error', json.loads(body))
        self.assertEqual(json.loads(self.req('/api/me')[1]), {'user': None, 'codeRequired': False})

    def test_register_sets_session_cookie(self):
        status, body, headers = self.register()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'user': {'username': 'alice'}})
        raw = headers['Set-Cookie']
        for part in ('HttpOnly', 'SameSite=Lax', 'Path=/'):
            self.assertIn(part, raw)
        self.assertNotIn('Secure', raw)
        self.assertEqual(json.loads(self.req('/api/me')[1])['user'], {'username': 'alice'})
        state = json.loads(self.req('/api/state')[1])
        self.assertEqual((state['schemaVersion'], state['bankVersion'], state['generation']), (2, '1', 0))
        self.assertEqual(json.loads(self.req('/api/exams')[1]),
                         {'revision': 0, 'workspace': {'active': None, 'history': []}})

    def test_secure_flag_and_registration_code_come_from_config(self):
        secure = make_server(port=0, db_path=Path(self.temp.name)/'secure.sqlite3',
                             secure_cookies=True, registration_code='tajne')
        thread = threading.Thread(target=secure.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (secure.shutdown(), secure.server_close(), thread.join()))
        url = f'http://127.0.0.1:{secure.server_port}'
        def post(data):
            req = urllib.request.Request(url + '/api/register', data=json.dumps(data).encode(), method='POST',
                                         headers={'Content-Type': 'application/json', 'X-Na-Fali': '1'})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.headers
            except urllib.error.HTTPError as error:
                return error.code, error.headers
        with urllib.request.urlopen(url + '/api/me') as r:
            self.assertTrue(json.load(r)['codeRequired'])
        self.assertEqual(post({'username': 'alice', 'password': 'password123'})[0], 400)
        self.assertEqual(post({'username': 'alice', 'password': 'password123', 'code': 'zle'})[0], 400)
        status, headers = post({'username': 'alice', 'password': 'password123', 'code': 'tajne'})
        self.assertEqual(status, 200)
        self.assertIn('Secure', headers['Set-Cookie'])

    def test_login_logout_and_csrf_header(self):
        self.register()
        self.cookie = None
        self.assertEqual(self.req('/api/login', {'username': 'alice', 'password': 'zle-haslo'}, 'POST')[0], 401)
        self.assertIsNone(self.cookie)
        status, body, _ = self.req('/api/login', {'username': 'alice', 'password': 'password123'}, 'POST')
        self.assertEqual((status, json.loads(body)['user']['username']), (200, 'alice'))
        token = self.cookie
        self.assertEqual(self.req('/api/state')[0], 200)
        status, body, headers = self.req('/api/logout', {}, 'POST')
        self.assertEqual((status, json.loads(body)), (200, {'user': None}))
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertIsNone(self.cookie)
        self.cookie = token                         # stary token po wylogowaniu jest martwy
        self.assertEqual(self.req('/api/state')[0], 401)
        self.assertEqual(self.req('/api/login', {'username': 'alice', 'password': 'password123'}, 'POST',
                                  **{'X-Na-Fali': '0'})[0], 403)
        self.assertEqual(self.register('Alice')[0], 409)

    def test_login_is_rate_limited(self):
        self.register()
        self.server.limiter = server.RateLimit(limit=3)   # rejestracja tez liczy sie do limitu
        for _ in range(3):
            self.assertEqual(self.req('/api/login', {'username': 'alice', 'password': 'x' * 8}, 'POST')[0], 401)
        status, body, _ = self.req('/api/login', {'username': 'alice', 'password': 'password123'}, 'POST')
        self.assertEqual(status, 429)
        self.assertIn('error', json.loads(body))

    def test_rejects_cross_origin_and_invalid_answers(self):
        self.register()
        body = dict(bankVersion='1', generation=0, attempts=[attempt()])
        self.assertEqual(self.req('/api/attempts', body, 'POST', Origin='https://example.org')[0], 403)
        self.assertEqual(self.req('/api/attempts', body, 'POST', Host='evil.example')[0], 403)
        self.assertEqual(self.req('/api/attempts', body | {'bankVersion': '0'}, 'POST')[0], 400)
        body['attempts'][0]['chosen'] = True
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 400)
        self.assertEqual(json.loads(self.req('/api/state')[1])['attempts'], [])

    def test_export_import_and_reset(self):
        self.register()
        a = attempt()
        body = dict(bankVersion='1', generation=0, attempts=[a])
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 200)
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 200)
        self.assertEqual(len(json.loads(self.req('/api/state')[1])['attempts']), 1)
        status, cleared, _ = self.req('/api/history', dict(bankVersion='1', generation=0), 'DELETE')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(cleared)['schemaVersion'], 2)
        self.assertEqual(self.req('/api/attempts', body, 'POST')[0], 409)

    def test_users_see_only_their_own_history(self):
        self.register()
        a = attempt()
        self.assertEqual(self.req('/api/attempts', dict(bankVersion='1', generation=0, attempts=[a]), 'POST')[0], 200)
        self.cookie = None
        self.register('bob')
        self.assertEqual(json.loads(self.req('/api/state')[1])['attempts'], [])
        self.assertEqual(self.req('/api/attempts', dict(bankVersion='1', generation=0,
                                                        attempts=[a | {'chosen': 1}]), 'POST')[0], 200)
        self.assertEqual(json.loads(self.req('/api/state')[1])['attempts'][0]['chosen'], 1)

if __name__ == '__main__':
    unittest.main()
