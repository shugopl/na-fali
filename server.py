"""Na fali — serwer kursu krotkofalarskiego.

Strona jest samowystarczalna (`web/index.html` zawiera caly bank pytan w
`const DATA=`), a serwer dokłada trwalosc: podejscia do pytan i stan egzaminu
probnego w SQLite. BANK jest parsowany z tej samej strony, wiec tresc ma
jedno zrodlo prawdy i nie moze sie rozjechac miedzy klientem a backendem.
"""
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / 'web' / 'index.html'
MARKER = 'const DATA='
LOOPBACK = {'127.0.0.1', 'localhost', '::1', '[::1]'}
MAX_BODY = 8 * 1024 * 1024


class BadRequest(ValueError):
    """Dane od klienta nie przechodza walidacji -> HTTP 400."""


class Conflict(Exception):
    """Klient pracuje na nieaktualnym stanie -> HTTP 409."""


def _extract_data(source):
    """Wycina literal obiektu po `const DATA=`, pomijajac nawiasy w stringach."""
    start = source.index(MARKER) + len(MARKER)
    depth, index, in_string, escaped = 0, start, False, False
    while index < len(source):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise ValueError('nie znaleziono konca literalu DATA')


def load_bank(page=PAGE):
    return json.loads(_extract_data(page.read_text(encoding='utf-8')))


BANK = load_bank()
QUESTIONS = {question['id']: question for question in BANK['questions']}


def _validate_attempt(attempt):
    if not isinstance(attempt, dict):
        raise BadRequest('attempt: oczekiwano obiektu')

    missing = {'id', 'sessionId', 'questionId', 'at', 'chosen', 'help'} - attempt.keys()
    if missing:
        raise BadRequest(f'attempt: brak pol {sorted(missing)}')

    for field in ('id', 'sessionId', 'questionId', 'at'):
        if not isinstance(attempt[field], str) or not attempt[field]:
            raise BadRequest(f'attempt.{field}: oczekiwano niepustego tekstu')

    question = QUESTIONS.get(attempt['questionId'])
    if question is None:
        raise BadRequest(f'attempt.questionId: nieznane pytanie {attempt["questionId"]!r}')

    chosen = attempt['chosen']
    if chosen is not None:
        if isinstance(chosen, bool) or not isinstance(chosen, int):
            raise BadRequest('attempt.chosen: oczekiwano liczby calkowitej albo null')
        if not 0 <= chosen < len(question['options']):
            raise BadRequest(f'attempt.chosen: poza zakresem ({chosen})')

    if not isinstance(attempt['help'], bool):
        raise BadRequest('attempt.help: oczekiwano wartosci logicznej')
    return attempt


def _catalog_rows():
    """Plaski, deterministyczny indeks: pytania, kody Q i pasma."""
    position = 0
    for question in BANK['questions']:
        yield position, 'question', question['id'], question['q'], question.get('explain', '')
        position += 1
    for code in BANK['qCatalog']:
        yield position, 'q_code', code['code'], code['code'], code.get('meaning', '')
        position += 1
    for band in BANK['bandDetails']:
        yield position, 'band', str(band['id']), band['band'], band.get('teachingMode', '')
        position += 1


class Store:
    """Trwalosc podejsc i egzaminow.

    Zapisy sa idempotentne: ponowione podejscie o tym samym id jest scalane,
    a nie duplikowane ani nadpisywane pustka. Dzieki temu opozniony retry z
    klienta nie kasuje odpowiedzi zapisanej w miedzyczasie.
    """

    SCHEMA_VERSION = 2

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=NORMAL')
        self._db.row_factory = None
        self._migrate()
        self._build_catalog()

    def close(self):
        with self._lock:
            self._db.close()

    # --- schemat --------------------------------------------------------

    def _tables(self):
        rows = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row[0] for row in rows}

    def _migrate(self):
        with self._lock, self._db:
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0 and 'attempts' in self._tables():
                version = 1          # baza sprzed wprowadzenia user_version
            if version < 1:
                self._create_v2()
            elif version < 2:
                self._upgrade_v1_to_v2()
            self._db.execute(f'PRAGMA user_version={self.SCHEMA_VERSION}')

    def _create_v2(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS attempts(
                id         TEXT PRIMARY KEY,
                sessionId  TEXT NOT NULL,
                questionId TEXT NOT NULL,
                at         TEXT NOT NULL,
                chosen     INTEGER,
                help       INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exams(
                id        INTEGER PRIMARY KEY CHECK(id = 1),
                revision  INTEGER NOT NULL,
                workspace TEXT);
            CREATE TABLE IF NOT EXISTS catalog(
                position INTEGER PRIMARY KEY,
                kind     TEXT NOT NULL,
                id       TEXT NOT NULL,
                title    TEXT NOT NULL,
                text     TEXT NOT NULL DEFAULT '');
        """)

    def _upgrade_v1_to_v2(self):
        """v1 mial kolumny snake_case i meta(id, generation) zamiast par klucz-wartosc."""
        generation = self._db.execute(
            'SELECT generation FROM meta WHERE id = 1').fetchone()
        self._db.executescript("""
            ALTER TABLE attempts RENAME TO attempts_v1;
            ALTER TABLE meta     RENAME TO meta_v1;
        """)
        self._create_v2()
        self._db.execute(
            'INSERT INTO attempts(id, sessionId, questionId, at, chosen, help)'
            ' SELECT id, session_id, question_id, at, chosen, help FROM attempts_v1')
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES('generation', ?)",
            (str(generation[0] if generation else 0),))
        self._db.executescript('DROP TABLE attempts_v1; DROP TABLE meta_v1;')

    # --- katalog tresci -------------------------------------------------

    def _build_catalog(self):
        """Materializuje indeks tresci z BANK; przebudowa tylko przy zmianie rewizji."""
        with self._lock, self._db:
            stamp = self._db.execute(
                "SELECT value FROM meta WHERE key='catalogRevision'").fetchone()
            if stamp and stamp[0] == BANK['contentRevision']:
                return
            self._db.execute('DELETE FROM catalog')
            self._db.executemany(
                'INSERT INTO catalog(position, kind, id, title, text) VALUES(?, ?, ?, ?, ?)',
                list(_catalog_rows()))
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('catalogRevision', ?)"
                ' ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (BANK['contentRevision'],))

    def catalog(self):
        with self._lock:
            rows = self._db.execute(
                'SELECT kind, id, title, text FROM catalog ORDER BY position').fetchall()
        return {
            'revision': BANK['contentRevision'],
            'items': [{'kind': r[0], 'id': r[1], 'title': r[2], 'text': r[3]} for r in rows],
        }

    # --- podejscia -----------------------------------------------------

    def _generation(self):
        row = self._db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        return int(row[0]) if row else 0

    def _require_generation(self, generation):
        current = self._generation()
        if generation != current:
            raise Conflict(f'generation: oczekiwano {current}, otrzymano {generation}')

    def save(self, attempts, generation):
        """Scala podejscia w jednej transakcji — albo wszystkie, albo zadne."""
        if not isinstance(attempts, list):
            raise BadRequest('attempts: oczekiwano listy')
        for attempt in attempts:            # walidacja przed transakcja
            _validate_attempt(attempt)

        with self._lock, self._db:
            self._require_generation(generation)
            for attempt in attempts:
                row = self._db.execute(
                    'SELECT chosen, help FROM attempts WHERE id = ?',
                    (attempt['id'],)).fetchone()
                if row is None:
                    self._db.execute(
                        'INSERT INTO attempts(id, sessionId, questionId, at, chosen, help)'
                        ' VALUES(?, ?, ?, ?, ?, ?)',
                        (attempt['id'], attempt['sessionId'], attempt['questionId'],
                         attempt['at'], attempt['chosen'], int(attempt['help'])))
                    continue

                stored_chosen, stored_help = row
                chosen = attempt['chosen']
                if chosen is not None and stored_chosen is not None and chosen != stored_chosen:
                    raise Conflict(
                        f'attempt {attempt["id"]}: odpowiedz juz zapisana jako {stored_chosen}')
                self._db.execute(
                    'UPDATE attempts SET chosen = ?, help = ? WHERE id = ?',
                    (stored_chosen if chosen is None else chosen,
                     int(bool(stored_help) or attempt['help']),
                     attempt['id']))
        return self.state()

    def state(self):
        with self._lock:
            rows = self._db.execute(
                'SELECT id, sessionId, questionId, at, chosen, help'
                ' FROM attempts ORDER BY at, id').fetchall()
            generation = self._generation()
        return {
            'bankVersion': BANK['contentRevision'],
            'generation': generation,
            'attempts': [
                {'id': r[0], 'sessionId': r[1], 'questionId': r[2],
                 'at': r[3], 'chosen': r[4], 'help': bool(r[5])}
                for r in rows],
        }

    def clear(self, generation):
        """Kasuje historie i podbija generacje, uniewazniajac stare karty."""
        with self._lock, self._db:
            self._require_generation(generation)
            self._db.execute('DELETE FROM attempts')
            current = generation + 1
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('generation', ?)"
                ' ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (str(current),))
        return {'generation': current}

    # --- egzaminy ------------------------------------------------------

    def _revision(self):
        row = self._db.execute('SELECT revision, workspace FROM exams WHERE id = 1').fetchone()
        return (0, None) if row is None else (row[0], row[1])

    def exams(self, workspace=None, revision=None):
        """Bez argumentow czyta stan; z argumentami zapisuje przy zgodnej rewizji."""
        if workspace is None:
            with self._lock:
                current, stored = self._revision()
            return {'revision': current, 'workspace': json.loads(stored) if stored else None}

        from exam_validation import validate_workspace
        validate_workspace(workspace)

        with self._lock, self._db:
            current, _ = self._revision()
            if revision != current:
                raise Conflict(f'revision: oczekiwano {current}, otrzymano {revision}')
            current += 1
            self._db.execute(
                'INSERT INTO exams(id, revision, workspace) VALUES(1, ?, ?)'
                ' ON CONFLICT(id) DO UPDATE SET revision = excluded.revision,'
                ' workspace = excluded.workspace',
                (current, json.dumps(workspace, ensure_ascii=False)))
        return {'revision': current, 'workspace': workspace}


class Handler(BaseHTTPRequestHandler):
    """Routing API. Strona i /api/health sa publiczne, reszta chroniona."""

    server_version = 'na-fali'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if os.environ.get('ACCESS_LOG'):
            super().log_message(fmt, *args)

    # --- pomocnicze ----------------------------------------------------

    def _send(self, status, payload=None, content_type='application/json; charset=utf-8'):
        body = b'' if payload is None else (
            payload if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _host_allowed(self):
        """Blokuje DNS rebinding i zapytania cross-origin."""
        allowed = self.server.allowed_hosts
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
        if host not in allowed:
            return False
        origin = self.headers.get('Origin')
        if origin:
            name = (urlsplit(origin).hostname or '').lower()
            if name not in allowed:
                return False
        return True

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            raise BadRequest('body: zbyt duze zadanie')
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, UnicodeDecodeError) as error:
            raise BadRequest(f'body: nieprawidlowy JSON ({error})') from error

    def _guard(self):
        """Zwraca True, gdy zadanie mutujace wolno obsluzyc."""
        if not self._host_allowed():
            self._send(403, {'error': 'niedozwolony host albo origin'})
            return False
        # Naglowek niestandardowy: formularz cross-site go nie wysle (ochrona CSRF).
        if self.headers.get('X-Na-Fali') != '1':
            self._send(403, {'error': 'brak naglowka X-Na-Fali'})
            return False
        return True

    def _dispatch(self, action):
        try:
            self._send(200, action())
        except Conflict as error:
            self._send(409, {'error': str(error)})
        except ValueError as error:          # BadRequest i ValueError z walidacji egzaminu
            self._send(400, {'error': str(error)})

    # --- trasy ---------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':            # bez kontroli hosta: sonda kubeletu uzywa IP poda
            return self._send(200, {'status': 'ok'})
        if not self._host_allowed():
            return self._send(403, {'error': 'niedozwolony host albo origin'})
        if path in ('/', '/index.html'):
            return self._send(200, self.server.page, 'text/html; charset=utf-8')
        if path == '/api/state':
            return self._dispatch(self.server.store.state)
        if path == '/api/exams':
            return self._dispatch(self.server.store.exams)
        return self._send(404, {'error': 'nie znaleziono'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._guard():
            return
        if path not in ('/api/attempts', '/api/exams'):
            return self._send(404, {'error': 'nie znaleziono'})
        try:
            body = self._body()          # strumien zadania czytamy dokladnie raz
        except BadRequest as error:
            return self._send(400, {'error': str(error)})
        if path == '/api/attempts':
            return self._dispatch(lambda: self.server.store.save(
                body.get('attempts'), body.get('generation')))
        return self._dispatch(lambda: self.server.store.exams(
            body.get('workspace'), body.get('revision')))

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if not self._guard():
            return
        if path != '/api/history':
            return self._send(404, {'error': 'nie znaleziono'})
        try:
            body = self._body()
        except BadRequest as error:
            return self._send(400, {'error': str(error)})
        return self._dispatch(lambda: self.server.store.clear(body.get('generation')))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(port=0, db_path=None, host='127.0.0.1', allowed_hosts=None, page=PAGE):
    """Buduje serwer gotowy do serve_forever(). port=0 wybiera wolny port."""
    server = Server((host, port), Handler)
    server.store = Store(db_path or ROOT / 'data' / 'course.sqlite3')
    server.page = Path(page).read_bytes()
    names = allowed_hosts if allowed_hosts is not None else os.environ.get('ALLOWED_HOSTS', '')
    if isinstance(names, str):
        names = [part.strip() for part in names.split(',') if part.strip()]
    server.allowed_hosts = LOOPBACK | {name.lower() for name in names}
    return server


def main():
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8080'))
    db_path = Path(os.environ.get('DB_PATH', ROOT / 'data' / 'course.sqlite3'))
    server = make_server(port=port, db_path=db_path, host=host)
    # Pierwsza linia stdout to adres — testy i logi wdrozenia na nia licza.
    print(f'na-fali http://{host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
