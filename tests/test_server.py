import concurrent.futures
import json
import os
from pathlib import Path
import re
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
from server import (Store, BadRequest, Conflict, Forbidden, Unauthorized, Unverified,
                    LEGACY_USER, make_server)

ALICE = ('alice@example.com', 'password123')
BOB = ('bob@example.com', 'password123')
WORKSPACE_ANSWERED = None   # wypelniane w setUpModule z BANK


def attempt(**overrides):
    value = dict(id=str(uuid.uuid4()), sessionId=str(uuid.uuid4()), questionId='d1',
                 at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                 chosen=0, help=False)
    return value | overrides


def finished_exam(correct_per_block):
    """Ukonczony egzamin z zadana liczba poprawnych odpowiedzi w kazdym bloku."""
    import time as _time
    now = int(_time.time() * 1000) - 10 ** 6
    rules = server.BANK['examRules']
    blocks = []
    for index, subject in enumerate(rules['subjects']):
        pool = [q for q in server.BANK['questions'] if q.get('examSubject') == subject and q.get('examEligible')][:8]
        questions = []
        for slot, q in enumerate(pool):
            chosen = q['answer'] if slot < correct_per_block[index] else (q['answer'] + 1) % 3
            questions.append({'id': q['id'], 'order': [2, 0, 1], 'chosen': chosen})
        blocks.append({'subject': subject, 'questions': questions, 'startedAt': now + index * 1000,
                       'endedAt': now + index * 1000 + 500, 'reason': 'manual'})
    return {'id': f'exam-{uuid.uuid4().hex[:6]}', 'contentRevision': server.BANK['contentRevision'],
            'status': 'complete', 'startedAt': now, 'finishedAt': now + 5000, 'blockIndex': 3,
            'deadline': None, 'blocks': blocks}


class FakeMailer:
    configured = True

    def __init__(self):
        self.sent = []
        self.fail = None

    def send(self, to, subject, body):
        if self.fail:
            raise self.fail
        self.sent.append((to, subject, body))

    send_async = send

    def last_code(self, to):
        for recipient, _, body in reversed(self.sent):
            if recipient == to:
                return re.search(r'\b\d{6}\b', body).group(0)
        return None


def cookie_from(headers):
    """Wartosc cookie sesji z Set-Cookie albo None, gdy naglowek ja kasuje."""
    raw = headers.get('Set-Cookie')
    if raw is None:
        return None, None
    value = raw.split(';', 1)[0].split('=', 1)[1]
    return (value or None), raw


def store_with_user(path, email=ALICE[0], password=ALICE[1], mailer=None):
    """Store + zweryfikowane konto (rejestracja -> kod -> verify)."""
    store = Store(path)
    mailer = mailer or FakeMailer()
    user = store.register(email, password)
    code = store.issue_code(user['id'], 'verify')
    store.verify(email, password, code)
    return store, store.find_user(email)['id']


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'course.sqlite3'
        self.store, self.uid = store_with_user(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def verified(self, email, password='password123'):
        user = self.store.register(email, password)
        self.store.verify(email, password, self.store.issue_code(user['id'], 'verify'))
        return self.store.find_user(email)['id']

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
        bob = self.verified(BOB[0])
        a = attempt()
        self.store.save(self.uid, [a], 0)
        self.store.clear(self.uid, 0)
        self.assertEqual(self.store.state(bob), {'schemaVersion': 2, 'bankVersion': '1',
                                                 'generation': 0, 'attempts': []})
        self.store.save(bob, [a | {'chosen': 1}], 0)      # to samo id u innego konta: bez konfliktu
        self.assertEqual(self.store.state(bob)['attempts'][0]['chosen'], 1)
        self.assertEqual(self.store.state(self.uid)['attempts'], [])

    def test_registration_requires_email_and_verification(self):
        for name in ('alice', 'a b@x', 'x@y', None, 'x' * 250 + '@example.com'):
            with self.assertRaises(BadRequest, msg=name):
                self.store.register(name, 'password123')
        with self.assertRaises(BadRequest):
            self.store.register('carol@example.com', 'short')
        with self.assertRaises(Conflict):
            self.store.register('Alice@Example.com', 'password123')  # zweryfikowane: bez wielkosci liter
        carol = self.store.register('  Carol@Example.com ', 'password123')
        self.assertEqual(carol['username'], 'carol@example.com')
        self.assertIsNone(carol['verifiedAt'])
        with self.assertRaises(Unverified):                 # dobre haslo, brak potwierdzenia
            self.store.login('carol@example.com', 'password123')
        with self.assertRaises(Unauthorized):               # zle haslo nie zdradza statusu
            self.store.login('carol@example.com', 'password124')
        # ponowna rejestracja niepotwierdzonego adresu podmienia haslo
        self.store.register('carol@example.com', 'otherpassword')
        code = self.store.issue_code(carol['id'], 'verify')
        with self.assertRaises(Unauthorized):               # verify wymaga hasla
            self.store.verify('carol@example.com', 'password123', code)
        with self.assertRaises(BadRequest):
            self.store.verify('carol@example.com', 'otherpassword', '000000')
        user = self.store.verify('carol@example.com', 'otherpassword', code)
        self.assertIsNotNone(user['verifiedAt'])
        self.assertEqual(self.store.login('CAROL@example.com', 'otherpassword')['id'], carol['id'])
        self.assertIsNotNone(self.store.get_user(carol['id'])['lastLoginAt'])

    def test_codes_expire_throttle_and_lock_after_bad_tries(self):
        dave = self.store.register('dave@example.com', 'password123')
        code = self.store.issue_code(dave['id'], 'verify')
        with self.assertRaises(Conflict):                   # throttle 60 s
            self.store.issue_code(dave['id'], 'verify')
        for _ in range(server.CODE_MAX_TRIES - 1):
            with self.assertRaises(BadRequest):
                self.store.verify('dave@example.com', 'password123', '111111')
        with self.assertRaises(BadRequest):                 # piata proba kasuje kod
            self.store.verify('dave@example.com', 'password123', '111111')
        with self.assertRaises(BadRequest):                 # prawidlowy kod juz nie dziala
            self.store.verify('dave@example.com', 'password123', code)
        with sqlite3.connect(self.path) as db:              # wygasly kod
            db.execute('UPDATE codes SET issuedAt = issuedAt - 1000 WHERE userId = ?', (dave['id'],))
        code = self.store.issue_code(dave['id'], 'verify')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE codes SET expiresAt = 0 WHERE userId = ?', (dave['id'],))
        with self.assertRaises(BadRequest):
            self.store.verify('dave@example.com', 'password123', code)

    def test_password_reset_revokes_sessions(self):
        self.assertIsNone(self.store.request_reset('nobody@example.com'))
        self.assertIsNone(self.store.request_reset('not-an-email'))
        unverified = self.store.register('erin@example.com', 'password123')
        self.assertIsNone(self.store.request_reset('erin@example.com'))
        user = self.store.request_reset('Alice@example.com')
        self.assertEqual(user['id'], self.uid)
        token = self.store.create_session(self.uid)
        code = self.store.issue_code(self.uid, 'reset')
        with self.assertRaises(BadRequest):
            self.store.confirm_reset(ALICE[0], code, 'short')
        with self.assertRaises(BadRequest):
            self.store.confirm_reset(ALICE[0], '000000', 'newpassword1')
        self.store.confirm_reset(ALICE[0], code, 'newpassword1')
        self.assertIsNone(self.store.session_user(token))
        self.assertEqual(self.store.login(ALICE[0], 'newpassword1')['id'], self.uid)
        with self.assertRaises(Unauthorized):
            self.store.login(ALICE[0], ALICE[1])

    def test_settings_gate_registration(self):
        self.assertEqual(self.store.settings(), {'registrationOpen': True, 'registrationCode': ''})
        self.store.update_settings(registration_code='tajne')
        with self.assertRaises(BadRequest):
            self.store.register('frank@example.com', 'password123')
        with self.assertRaises(BadRequest):
            self.store.register('frank@example.com', 'password123', 'zle')
        self.store.register('frank@example.com', 'password123', 'tajne')
        self.store.update_settings(registration_open=False)
        with self.assertRaises(Forbidden):
            self.store.register('grace@example.com', 'password123', 'tajne')
        self.store.seed_settings('inny')                    # ziarno nie nadpisuje istniejacych
        self.assertEqual(self.store.settings(), {'registrationOpen': False, 'registrationCode': 'tajne'})

    def test_ensure_admin_is_idempotent(self):
        admin = self.store.ensure_admin('Admin@Example.com', 'adminpass1')
        self.assertTrue(admin['isAdmin'] and admin['verifiedAt'])
        again = self.store.ensure_admin('admin@example.com', 'otherpassword')
        self.assertEqual(again['id'], admin['id'])
        self.assertEqual(self.store.login('admin@example.com', 'adminpass1')['id'], admin['id'])
        self.store.ensure_admin('admin@example.com', 'otherpassword', reset=True)
        self.assertEqual(self.store.login('admin@example.com', 'otherpassword')['id'], admin['id'])
        promoted = self.store.ensure_admin(ALICE[0], '')       # istniejace konto: tylko uprawnienia
        self.assertTrue(promoted['isAdmin'])
        self.assertEqual(self.store.login(*ALICE)['id'], self.uid)
        with self.assertRaises(BadRequest):
            self.store.ensure_admin('not-an-email', 'adminpass1')

    def test_admin_lists_stats_and_backup(self):
        bob = self.verified(BOB[0])
        self.store.save(self.uid, [attempt(chosen=0), attempt(chosen=1), attempt(help=True)], 0)
        self.store.exams(self.uid, {'active': None, 'history': [finished_exam([8, 7, 6, 6]),
                                                               finished_exam([8, 8, 5, 8])]}, 0)
        users = {u['email']: u for u in self.store.users()}
        alice = users[ALICE[0]]
        self.assertEqual((alice['attempts'], alice['correct']), (3, 1))   # z podpowiedzia nie liczy sie
        self.assertEqual((alice['examsFinished'], alice['examsPassed'], alice['bestScore']), (2, 1, 29))
        self.assertEqual((users[BOB[0]]['attempts'], users[BOB[0]]['examsFinished']), (0, 0))
        overview = self.store.overview()
        self.assertEqual(overview['users']['total'], 2)
        self.assertEqual(overview['exams'], {'finished': 2, 'passed': 1})
        self.assertEqual(overview['subjects']['radio'], {'attempts': 3, 'correct': 1, 'helped': 1})
        export = self.store.user_export(self.uid)
        self.assertEqual((export['schemaVersion'], export['bankVersion'], len(export['attempts'])), (2, '1', 3))
        self.assertEqual(len(export['exams']['history']), 2)
        data = self.store.backup()
        self.assertTrue(data.startswith(b'SQLite format 3\x00'))
        copy = sqlite3.connect(':memory:')
        copy.deserialize(data)
        self.assertEqual(copy.execute('SELECT COUNT(*) FROM users').fetchone()[0], 2)
        self.store.clear_all(self.uid)
        self.assertEqual(self.store.state(self.uid)['generation'], 1)
        self.assertEqual(self.store.exams(self.uid)['revision'], 0)
        self.store.delete_user(bob)
        self.assertIsNone(self.store.get_user(bob))
        with self.assertRaises(BadRequest):
            self.store.delete_user(bob)

    def test_v3_database_migrates_to_v4(self):
        path = Path(self.temp.name) / 'v3.sqlite3'
        with sqlite3.connect(path) as db:
            for statement in server.V3_DDL:
                db.execute(statement)
            db.executescript('''
                INSERT INTO users(id, username, passwordHash, generation, createdAt) VALUES
                  (1, 'smoke-test', 'x', 0, '2026-09-25T00:00:00Z'),
                  (2, '#legacy', '!', 3, '2026-09-25T00:00:00Z'),
                  (3, 'Old@Example.com', 'x', 0, '2026-09-25T00:00:00Z');
                INSERT INTO attempts VALUES(2,'old-attempt','s','d1','2026-01-01T00:00:00.000Z',1,0);
                INSERT INTO sessions VALUES('t', 1, '2026-09-25T00:00:00Z', 9999999999);
                PRAGMA user_version=3;''')
        store = Store(path)
        self.assertIsNone(store.find_user('smoke-test'))
        self.assertEqual(store.find_user(LEGACY_USER)['generation'], 3)
        old = store.find_user('old@example.com')
        self.assertEqual(old['verifiedAt'], old['createdAt'])
        self.assertFalse(old['isAdmin'])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            self.assertIn('codes', {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            db.execute('PRAGMA user_version=3')             # rollback obrazu: krok v4 idzie drugi raz
        Store(path)

    def test_v2_database_migrates_to_v4_keeping_legacy_rows(self):
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
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone())

    def test_real_process_restart_preserves_data(self):
        self.store.close()
        env = os.environ | {'HOST': '127.0.0.1', 'PORT': '0', 'DB_PATH': str(self.path),
                            'SMTP_HOST': '', 'ADMIN_EMAIL': ''}
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
        status, _, headers = call(addr, '/api/login', {'email': ALICE[0], 'password': ALICE[1]}, 'POST')
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
        self.mailer = FakeMailer()
        self.server = make_server(port=0, db_path=Path(self.temp.name)/'db.sqlite3', mailer=self.mailer,
                                  admin_email='')
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

    def post(self, path, **data):
        status, body, headers = self.req(path, data, 'POST')
        return status, json.loads(body), headers

    def register(self, email=ALICE[0], password=ALICE[1], **extra):
        """Rejestracja -> kod z mailera -> verify. Zwraca odpowiedz verify."""
        status, body, _ = self.post('/api/register', email=email, password=password, **extra)
        self.assertEqual((status, body), (200, {'pending': True, 'email': email.lower()}))
        self.assertIsNone(self.cookie)
        code = self.mailer.last_code(email.lower())
        return self.post('/api/verify', email=email, password=password, code=code)

    def make_admin(self, email='admin@example.com', password='adminpass1'):
        self.server.store.ensure_admin(email, password)
        self.cookie = None
        status, body, _ = self.post('/api/login', email=email, password=password)
        self.assertEqual(status, 200)
        self.assertTrue(body['user']['isAdmin'])
        return self.server.store.find_user(email)['id']

    def test_serves_page_and_health_but_never_database(self):
        self.assertEqual(self.req('/')[0], 200)
        self.assertIn(b'const DATA=', self.req('/')[1])
        self.assertEqual(self.req('/api/health')[0], 200)
        self.assertEqual(self.req('/data/course.sqlite3')[0], 404)

    def test_data_routes_require_login(self):
        for path, data, method in [('/api/state', None, 'GET'), ('/api/exams', None, 'GET'),
                                   ('/api/attempts', {'bankVersion': '1', 'generation': 0, 'attempts': []}, 'POST'),
                                   ('/api/exams', {'bankVersion': '1', 'revision': 0, 'workspace': {}}, 'POST'),
                                   ('/api/history', {'bankVersion': '1', 'generation': 0}, 'DELETE'),
                                   ('/api/admin/users', None, 'GET'), ('/api/admin/backup', None, 'GET')]:
            status, body, _ = self.req(path, data, method)
            self.assertEqual(status, 401, path)
            self.assertIn('error', json.loads(body))
        self.assertEqual(json.loads(self.req('/api/me')[1]),
                         {'user': None, 'codeRequired': False, 'registrationOpen': True, 'mailConfigured': True})

    def test_register_verify_and_session_cookie(self):
        status, body, headers = self.register()
        self.assertEqual(status, 200)
        self.assertEqual(body, {'user': {'username': ALICE[0], 'email': ALICE[0], 'isAdmin': False}})
        raw = headers['Set-Cookie']
        for part in ('HttpOnly', 'SameSite=Lax', 'Path=/'):
            self.assertIn(part, raw)
        self.assertNotIn('Secure', raw)
        self.assertEqual(self.mailer.sent[0][1], 'Na fali — kod weryfikacyjny')
        self.assertEqual(json.loads(self.req('/api/me')[1])['user']['email'], ALICE[0])
        state = json.loads(self.req('/api/state')[1])
        self.assertEqual((state['schemaVersion'], state['bankVersion'], state['generation']), (2, '1', 0))
        self.assertEqual(json.loads(self.req('/api/exams')[1]),
                         {'revision': 0, 'workspace': {'active': None, 'history': []}})
        self.assertEqual(self.post('/api/register', email=ALICE[0], password='whatever1')[0], 409)

    def test_login_before_verification_reports_unverified(self):
        self.post('/api/register', email=BOB[0], password=BOB[1])
        status, body, _ = self.post('/api/login', email=BOB[0], password=BOB[1])
        self.assertEqual((status, body.get('unverified')), (401, True))
        status, body, _ = self.post('/api/login', email=BOB[0], password='wrong-pass')
        self.assertEqual((status, 'unverified' in body), (401, False))
        self.assertEqual(self.post('/api/register', email=BOB[0], password=BOB[1])[0], 409)  # throttle 60 s
        self.assertEqual(self.post('/api/verify', email=BOB[0], password=BOB[1], code='000000')[0], 400)
        code = self.mailer.last_code(BOB[0])
        self.assertEqual(self.post('/api/verify', email=BOB[0], password=BOB[1], code=code)[0], 200)
        self.assertIsNotNone(self.cookie)

    def test_password_reset_over_http(self):
        self.register()
        old_cookie = self.cookie
        self.cookie = None
        status, body, _ = self.post('/api/reset', email='ghost@example.com')
        self.assertEqual((status, body), (200, {'pending': True}))   # nieznany adres: ta sama odpowiedz
        sent_before = len(self.mailer.sent)
        self.assertEqual(self.post('/api/reset', email=ALICE[0])[0], 200)
        self.assertEqual(len(self.mailer.sent), sent_before + 1)
        code = self.mailer.last_code(ALICE[0])
        self.assertEqual(self.post('/api/reset/confirm', email=ALICE[0], code='999999', password='newpassword1')[0], 400)
        status, body, _ = self.post('/api/reset/confirm', email=ALICE[0], code=code, password='newpassword1')
        self.assertEqual((status, body['user']['email']), (200, ALICE[0]))
        new_cookie = self.cookie
        self.cookie = old_cookie
        self.assertEqual(self.req('/api/state')[0], 401)      # stara sesja martwa
        self.cookie = new_cookie
        self.assertEqual(self.req('/api/state')[0], 200)
        self.cookie = None
        self.assertEqual(self.post('/api/login', email=ALICE[0], password='newpassword1')[0], 200)

    def test_secure_flag_and_registration_code_come_from_config(self):
        mailer = FakeMailer()
        secure = make_server(port=0, db_path=Path(self.temp.name)/'secure.sqlite3',
                             secure_cookies=True, registration_code='tajne', mailer=mailer, admin_email='')
        thread = threading.Thread(target=secure.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (secure.shutdown(), secure.server_close(), thread.join()))
        url = f'http://127.0.0.1:{secure.server_port}'
        def post(path, data):
            req = urllib.request.Request(url + path, data=json.dumps(data).encode(), method='POST',
                                         headers={'Content-Type': 'application/json', 'X-Na-Fali': '1'})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.headers
            except urllib.error.HTTPError as error:
                return error.code, error.headers
        with urllib.request.urlopen(url + '/api/me') as r:
            self.assertTrue(json.load(r)['codeRequired'])
        self.assertEqual(post('/api/register', {'email': ALICE[0], 'password': ALICE[1]})[0], 400)
        self.assertEqual(post('/api/register', {'email': ALICE[0], 'password': ALICE[1], 'code': 'zle'})[0], 400)
        self.assertEqual(post('/api/register', {'email': ALICE[0], 'password': ALICE[1], 'code': 'tajne'})[0], 200)
        status, headers = post('/api/verify', {'email': ALICE[0], 'password': ALICE[1],
                                               'code': mailer.last_code(ALICE[0])})
        self.assertEqual(status, 200)
        self.assertIn('Secure', headers['Set-Cookie'])

    def test_login_logout_and_csrf_header(self):
        self.register()
        self.cookie = None
        self.assertEqual(self.post('/api/login', email=ALICE[0], password='zle-haslo')[0], 401)
        self.assertIsNone(self.cookie)
        status, body, _ = self.post('/api/login', email=ALICE[0], password=ALICE[1])
        self.assertEqual((status, body['user']['username']), (200, ALICE[0]))
        token = self.cookie
        self.assertEqual(self.req('/api/state')[0], 200)
        status, body, headers = self.post('/api/logout')
        self.assertEqual((status, body), (200, {'user': None}))
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertIsNone(self.cookie)
        self.cookie = token                         # stary token po wylogowaniu jest martwy
        self.assertEqual(self.req('/api/state')[0], 401)
        self.assertEqual(self.req('/api/login', {'email': ALICE[0], 'password': ALICE[1]}, 'POST',
                                  **{'X-Na-Fali': '0'})[0], 403)

    def test_login_is_rate_limited(self):
        self.register()
        self.server.limiter = server.RateLimit(limit=3)   # rejestracja tez liczy sie do limitu
        for _ in range(3):
            self.assertEqual(self.post('/api/login', email=ALICE[0], password='x' * 8)[0], 401)
        status, body, _ = self.post('/api/login', email=ALICE[0], password=ALICE[1])
        self.assertEqual(status, 429)
        self.assertIn('error', body)

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

    def test_finished_exam_is_accepted_by_server(self):
        self.register()
        workspace = {'active': None, 'history': [finished_exam([8, 8, 8, 8])]}
        status, body, _ = self.post('/api/exams', bankVersion='1', revision=0, workspace=workspace)
        self.assertEqual((status, body['revision']), (200, 1))
        between = finished_exam([8, 8, 8, 8]) | {'status': 'between', 'blockIndex': 1}
        self.assertEqual(self.post('/api/exams', bankVersion='1', revision=1,
                                   workspace={'active': between, 'history': []})[0], 200)
        self.assertEqual(self.post('/api/exams', bankVersion='1', revision=2,
                                   workspace={'active': None, 'history': [between]})[0], 400)

    def test_users_see_only_their_own_history(self):
        self.register()
        a = attempt()
        self.assertEqual(self.req('/api/attempts', dict(bankVersion='1', generation=0, attempts=[a]), 'POST')[0], 200)
        self.cookie = None
        self.register(BOB[0])
        self.assertEqual(json.loads(self.req('/api/state')[1])['attempts'], [])
        self.assertEqual(self.req('/api/attempts', dict(bankVersion='1', generation=0,
                                                        attempts=[a | {'chosen': 1}]), 'POST')[0], 200)
        self.assertEqual(json.loads(self.req('/api/state')[1])['attempts'][0]['chosen'], 1)

    def test_admin_routes_need_admin(self):
        self.register()
        for path, data, method in [('/api/admin/overview', None, 'GET'), ('/api/admin/users', None, 'GET'),
                                   ('/api/admin/backup', None, 'GET'), ('/api/admin/users/1/export', None, 'GET'),
                                   ('/api/admin/settings', {'registrationOpen': False}, 'POST'),
                                   ('/api/admin/mail-test', {}, 'POST'),
                                   ('/api/admin/users/1', {'action': 'promote'}, 'POST'),
                                   ('/api/admin/users/1', None, 'DELETE')]:
            self.assertEqual(self.req(path, data, method)[0], 403, path)
        self.assertTrue(self.server.store.settings()['registrationOpen'])

    def test_admin_panel_flow(self):
        self.register()
        alice = self.server.store.find_user(ALICE[0])['id']
        self.req('/api/attempts', dict(bankVersion='1', generation=0, attempts=[attempt()]), 'POST')
        admin = self.make_admin()
        overview = json.loads(self.req('/api/admin/overview')[1])
        self.assertEqual((overview['users']['total'], overview['users']['admins'], overview['attempts']), (2, 1, 1))
        self.assertEqual(overview['settings'], {'registrationOpen': True, 'registrationCode': ''})
        users = json.loads(self.req('/api/admin/users')[1])
        self.assertEqual([u['email'] for u in users], [ALICE[0], 'admin@example.com'])
        self.assertNotIn('passwordHash', users[0])
        export = json.loads(self.req(f'/api/admin/users/{alice}/export')[1])
        self.assertEqual(len(export['attempts']), 1)
        status, body, headers = self.req('/api/admin/backup')
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b'SQLite format 3\x00'))
        self.assertIn('attachment; filename="na-fali-', headers['Content-Disposition'])
        # akcje
        self.assertEqual(self.post(f'/api/admin/users/{alice}', action='promote')[1]['user']['isAdmin'], True)
        self.assertEqual(self.post(f'/api/admin/users/{alice}', action='demote')[1]['user']['isAdmin'], False)
        self.assertEqual(self.post(f'/api/admin/users/{admin}', action='demote')[0], 400)
        self.assertEqual(self.req(f'/api/admin/users/{admin}', None, 'DELETE')[0], 400)
        self.assertEqual(self.post(f'/api/admin/users/{alice}', action='clear')[0], 200)
        self.assertEqual(self.server.store.state(alice)['generation'], 1)
        sent = len(self.mailer.sent)
        self.assertEqual(self.post(f'/api/admin/users/{alice}', action='reset')[0], 200)
        self.assertEqual(self.mailer.sent[sent][1], 'Na fali — reset hasła')
        self.assertEqual(self.post(f'/api/admin/users/{alice}', action='bogus')[0], 400)
        self.assertEqual(self.post(f'/api/admin/users/{alice}', action='logout')[0], 200)
        self.assertEqual(self.post('/api/admin/mail-test')[1]['ok'], True)
        self.mailer.fail = RuntimeError('SMTP down')
        status, body, _ = self.post('/api/admin/mail-test')
        self.assertEqual(status, 400)
        self.assertIn('SMTP down', body['error'])
        self.mailer.fail = None
        # ustawienia
        status, body, _ = self.post('/api/admin/settings', registrationOpen=False, registrationCode='kod')
        self.assertEqual((status, body), (200, {'registrationOpen': False, 'registrationCode': 'kod'}))
        self.assertEqual(self.post('/api/admin/settings', registrationOpen='nie')[0], 400)
        self.cookie = None
        self.assertEqual(self.post('/api/register', email=BOB[0], password=BOB[1], code='kod')[0], 403)
        self.assertFalse(json.loads(self.req('/api/me')[1])['registrationOpen'])
        # usuwanie
        self.make_admin()
        self.assertEqual(self.req(f'/api/admin/users/{alice}', None, 'DELETE')[0], 200)
        self.assertEqual(self.req(f'/api/admin/users/{alice}', None, 'DELETE')[0], 400)
        self.assertEqual([u['email'] for u in json.loads(self.req('/api/admin/users')[1])], ['admin@example.com'])

if __name__ == '__main__':
    unittest.main()
