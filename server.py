"""Na fali — serwer kursu krotkofalarskiego.

Strona jest samowystarczalna (`web/index.html` zawiera caly bank pytan w
`const DATA=`), a serwer dokłada trwalosc: konta uzytkownikow, ich podejscia
do pytan i stan egzaminu probnego w SQLite. BANK i BANK_VERSION sa parsowane
z tej samej strony, wiec tresc i format danych maja jedno zrodlo prawdy i nie
moga sie rozjechac miedzy klientem a backendem.

Tresc kursu jest publiczna; historia, postep i egzaminy naleza do zalogowanego
uzytkownika (sesja w cookie HttpOnly, hasla scrypt).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / 'web' / 'index.html'
MARKER = 'const DATA='
VERSION_RE = re.compile(r"const BANK_VERSION='([^']+)'")
LOOPBACK = {'127.0.0.1', 'localhost', '::1', '[::1]'}
MAX_BODY = 8 * 1024 * 1024
AUTH_BODY = 4096                      # register/login/logout nie potrzebuja wiecej
COOKIE = 'nafali_session'
SESSION_SECONDS = 30 * 24 * 3600
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,32}$')
PASSWORD_MIN, PASSWORD_MAX = 8, 128
# 16 MiB i ~0,1 s na hash. Nie podnosic n do 2**15 bez maxmem — OpenSSL ma
# domyslny limit 32 MiB i scrypt rzuca wtedy wyjatek zamiast liczyc.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
LEGACY_USER = '#legacy'               # nie przechodzi USERNAME_RE, wiec nie da sie na niego zalogowac
_scrypt = hashlib.scrypt              # brak scrypt w OpenSSL ma wywalic import, nie pierwsza rejestracje


class BadRequest(ValueError):
    """Dane od klienta nie przechodza walidacji -> HTTP 400."""


class Unauthorized(Exception):
    """Brak waznej sesji albo zle poswiadczenia -> HTTP 401."""


class Conflict(Exception):
    """Klient pracuje na nieaktualnym stanie -> HTTP 409."""


class TooManyRequests(Exception):
    """Za duzo prob logowania z jednego adresu -> HTTP 429."""


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


def _extract_bank_version(source):
    """Wersja formatu danych (`const BANK_VERSION='…'`), ktora klient porownuje ze stanem."""
    found = VERSION_RE.findall(source)
    if len(found) != 1:
        raise ValueError(f'oczekiwano jednej definicji BANK_VERSION, znaleziono {len(found)}')
    return found[0]


def load_bank(page=PAGE):
    return json.loads(_extract_data(page.read_text(encoding='utf-8')))


_SOURCE = PAGE.read_text(encoding='utf-8')
BANK = json.loads(_extract_data(_SOURCE))
BANK_VERSION = _extract_bank_version(_SOURCE)
del _SOURCE
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


# --- hasla i sesje ---------------------------------------------------------

def hash_password(password):
    salt = os.urandom(16)
    digest = _scrypt(password.encode('utf-8'), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}'


def verify_password(password, stored):
    """Stala w czasie porownanie; kazdy nieparsowalny zapis (np. '!') to False."""
    try:
        scheme, n, r, p, salt, digest = stored.split('$')
        if scheme != 'scrypt':
            return False
        candidate = _scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p))
    except (AttributeError, ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


_DUMMY_HASH = []


def _dummy_hash():
    """Hash do porownania, gdy uzytkownik nie istnieje — wyrownuje czas odpowiedzi."""
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password(secrets.token_urlsafe(16)))
    return _DUMMY_HASH[0]


def _token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class RateLimit:
    """Okno przesuwne w pamieci: `limit` prob na `window` sekund na klucz."""

    def __init__(self, limit=10, window=300):
        self.limit, self.window = limit, window
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, key):
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 10000:      # nie rosnij bez konca przy skanowaniu
                self._hits = {k: v for k, v in self._hits.items()
                              if v and now - v[-1] < self.window}
            hits = [stamp for stamp in self._hits.get(key, ()) if now - stamp < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                raise TooManyRequests('zbyt wiele prob; sprobuj ponownie za kilka minut')
            hits.append(now)
            self._hits[key] = hits


V3_DDL = (
    """CREATE TABLE IF NOT EXISTS users(
        id           INTEGER PRIMARY KEY,
        username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
        passwordHash TEXT NOT NULL,
        generation   INTEGER NOT NULL DEFAULT 0,
        createdAt    TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions(
        tokenHash TEXT PRIMARY KEY,
        userId    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        createdAt TEXT NOT NULL,
        expiresAt INTEGER NOT NULL)""",
    'CREATE INDEX IF NOT EXISTS sessions_user ON sessions(userId)',
    """CREATE TABLE IF NOT EXISTS attempts(
        userId     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        id         TEXT NOT NULL,
        sessionId  TEXT NOT NULL,
        questionId TEXT NOT NULL,
        at         TEXT NOT NULL,
        chosen     INTEGER,
        help       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(userId, id))""",
    """CREATE TABLE IF NOT EXISTS exams(
        userId    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        revision  INTEGER NOT NULL,
        workspace TEXT)""",
    'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    """CREATE TABLE IF NOT EXISTS catalog(
        position INTEGER PRIMARY KEY,
        kind     TEXT NOT NULL,
        id       TEXT NOT NULL,
        title    TEXT NOT NULL,
        text     TEXT NOT NULL DEFAULT '')""",
)


class Store:
    """Trwalosc kont, podejsc i egzaminow.

    Zapisy sa idempotentne: ponowione podejscie o tym samym id jest scalane,
    a nie duplikowane ani nadpisywane pustka. Dzieki temu opozniony retry z
    klienta nie kasuje odpowiedzi zapisanej w miedzyczasie. Kazda metoda
    danych bierze `user_id` jako pierwszy argument — historia jest per konto.
    """

    SCHEMA_VERSION = 3

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=NORMAL')
        self._db.execute('PRAGMA foreign_keys=ON')
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
                self._create_v3()
            else:
                if version < 2:
                    self._upgrade_v1_to_v2()
                if version < 3:
                    self._upgrade_v2_to_v3()
            self._db.execute(f'PRAGMA user_version={self.SCHEMA_VERSION}')

    def _create_v3(self):
        # Pojedyncze execute, nie executescript: ten drugi commituje po drodze
        # i migracja przestalaby byc jedna transakcja.
        for statement in V3_DDL:
            self._db.execute(statement)

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

    def _upgrade_v2_to_v3(self):
        """v2 mial jedna wspolna historie; v3 przypina ja do kont.

        Istniejace wiersze (jesli sa) trafiaja do uzytkownika zastepczego
        `#legacy`, na ktorego nie da sie zalogowac — historia nie ginie, a w
        razie potrzeby mozna ja przepiac SQL-em do prawdziwego konta.
        """
        row = self._db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        generation = int(row[0]) if row else 0
        count = self._db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
        exam = self._db.execute('SELECT revision, workspace FROM exams WHERE id = 1').fetchone()

        self._db.execute('ALTER TABLE attempts RENAME TO attempts_v2')
        self._db.execute('ALTER TABLE exams RENAME TO exams_v2')
        self._create_v3()
        if count or generation or exam:
            cursor = self._db.execute(
                "INSERT INTO users(username, passwordHash, generation, createdAt)"
                " VALUES(?, '!', ?, ?)", (LEGACY_USER, generation, _now()))
            legacy = cursor.lastrowid
            self._db.execute(
                'INSERT INTO attempts(userId, id, sessionId, questionId, at, chosen, help)'
                ' SELECT ?, id, sessionId, questionId, at, chosen, help FROM attempts_v2',
                (legacy,))
            if exam:
                self._db.execute(
                    'INSERT INTO exams(userId, revision, workspace) VALUES(?, ?, ?)',
                    (legacy, exam[0], exam[1]))
        self._db.execute('DROP TABLE attempts_v2')
        self._db.execute('DROP TABLE exams_v2')
        self._db.execute("DELETE FROM meta WHERE key='generation'")

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

    # --- konta ---------------------------------------------------------

    def register(self, username, password, code=None, required_code=''):
        """Zaklada konto; przy ustawionym kodzie rejestracji wymaga jego podania."""
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            raise BadRequest('username: 3-32 znaki — litery, cyfry, kropka, myslnik, podkreslenie')
        if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            raise BadRequest(f'password: od {PASSWORD_MIN} do {PASSWORD_MAX} znakow')
        if required_code and not (isinstance(code, str) and hmac.compare_digest(code, required_code)):
            raise BadRequest('code: nieprawidlowy kod rejestracji')

        digest = hash_password(password)     # kosztowne, wiec poza blokada
        with self._lock, self._db:
            try:
                cursor = self._db.execute(
                    'INSERT INTO users(username, passwordHash, generation, createdAt)'
                    ' VALUES(?, ?, 0, ?)', (username, digest, _now()))
            except sqlite3.IntegrityError:
                raise Conflict('username: ta nazwa jest juz zajeta') from None
            return {'id': cursor.lastrowid, 'username': username}

    def find_user(self, username):
        with self._lock:
            row = self._db.execute(
                'SELECT id, username, passwordHash, generation FROM users WHERE username = ?',
                (username,)).fetchone()
        if row is None:
            return None
        return {'id': row[0], 'username': row[1], 'passwordHash': row[2], 'generation': row[3]}

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            raise BadRequest('username/password: oczekiwano tekstu')
        user = self.find_user(username)
        stored = user['passwordHash'] if user else _dummy_hash()
        if not verify_password(password, stored) or user is None:
            raise Unauthorized('nieprawidlowa nazwa uzytkownika lub haslo')
        return {'id': user['id'], 'username': user['username']}

    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute('DELETE FROM sessions WHERE expiresAt < ?', (now,))
            self._db.execute(
                'INSERT INTO sessions(tokenHash, userId, createdAt, expiresAt) VALUES(?, ?, ?, ?)',
                (_token_hash(token), user_id, _now(), now + SESSION_SECONDS))
        return token

    def session_user(self, token):
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                'SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.userId'
                ' WHERE s.tokenHash = ? AND s.expiresAt >= ?',
                (_token_hash(token), int(time.time()))).fetchone()
        return None if row is None else {'id': row[0], 'username': row[1]}

    def delete_session(self, token):
        if not token:
            return
        with self._lock, self._db:
            self._db.execute('DELETE FROM sessions WHERE tokenHash = ?', (_token_hash(token),))

    # --- podejscia -----------------------------------------------------

    def _generation(self, user_id):
        row = self._db.execute('SELECT generation FROM users WHERE id = ?', (user_id,)).fetchone()
        if row is None:
            raise Unauthorized('konto nie istnieje')
        return row[0]

    def _require_generation(self, user_id, generation):
        current = self._generation(user_id)
        if generation != current:
            raise Conflict(f'generation: oczekiwano {current}, otrzymano {generation}')

    def save(self, user_id, attempts, generation):
        """Scala podejscia w jednej transakcji — albo wszystkie, albo zadne."""
        if not isinstance(attempts, list):
            raise BadRequest('attempts: oczekiwano listy')
        for attempt in attempts:            # walidacja przed transakcja
            _validate_attempt(attempt)

        with self._lock, self._db:
            self._require_generation(user_id, generation)
            for attempt in attempts:
                row = self._db.execute(
                    'SELECT chosen, help FROM attempts WHERE userId = ? AND id = ?',
                    (user_id, attempt['id'])).fetchone()
                if row is None:
                    self._db.execute(
                        'INSERT INTO attempts(userId, id, sessionId, questionId, at, chosen, help)'
                        ' VALUES(?, ?, ?, ?, ?, ?, ?)',
                        (user_id, attempt['id'], attempt['sessionId'], attempt['questionId'],
                         attempt['at'], attempt['chosen'], int(attempt['help'])))
                    continue

                stored_chosen, stored_help = row
                chosen = attempt['chosen']
                if chosen is not None and stored_chosen is not None and chosen != stored_chosen:
                    raise Conflict(
                        f'attempt {attempt["id"]}: odpowiedz juz zapisana jako {stored_chosen}')
                self._db.execute(
                    'UPDATE attempts SET chosen = ?, help = ? WHERE userId = ? AND id = ?',
                    (stored_chosen if chosen is None else chosen,
                     int(bool(stored_help) or attempt['help']),
                     user_id, attempt['id']))
        return self.state(user_id)

    def state(self, user_id):
        """Ksztalt, ktorego oczekuje `acceptState` na stronie (schemaVersion + bankVersion)."""
        with self._lock:
            rows = self._db.execute(
                'SELECT id, sessionId, questionId, at, chosen, help'
                ' FROM attempts WHERE userId = ? ORDER BY at, id', (user_id,)).fetchall()
            generation = self._generation(user_id)
        return {
            'schemaVersion': 2,
            'bankVersion': BANK_VERSION,
            'generation': generation,
            'attempts': [
                {'id': r[0], 'sessionId': r[1], 'questionId': r[2],
                 'at': r[3], 'chosen': r[4], 'help': bool(r[5])}
                for r in rows],
        }

    def clear(self, user_id, generation):
        """Kasuje historie konta i podbija generacje, uniewazniajac stare karty."""
        with self._lock, self._db:
            self._require_generation(user_id, generation)
            self._db.execute('DELETE FROM attempts WHERE userId = ?', (user_id,))
            self._db.execute('UPDATE users SET generation = ? WHERE id = ?',
                             (generation + 1, user_id))
        return self.state(user_id)       # klient robi acceptState() na wyniku

    # --- egzaminy ------------------------------------------------------

    def _revision(self, user_id):
        row = self._db.execute(
            'SELECT revision, workspace FROM exams WHERE userId = ?', (user_id,)).fetchone()
        return (0, None) if row is None else (row[0], row[1])

    def exams(self, user_id, workspace=None, revision=None):
        """Bez workspace czyta stan; z workspace zapisuje przy zgodnej rewizji."""
        if workspace is None:
            with self._lock:
                current, stored = self._revision(user_id)
            # Pusty workspace w ksztalcie, ktory zna klient (emptyExams()).
            return {'revision': current,
                    'workspace': json.loads(stored) if stored else {'active': None, 'history': []}}

        from exam_validation import validate_workspace
        validate_workspace(workspace)

        with self._lock, self._db:
            current, _ = self._revision(user_id)
            if revision != current:
                raise Conflict(f'revision: oczekiwano {current}, otrzymano {revision}')
            current += 1
            self._db.execute(
                'INSERT INTO exams(userId, revision, workspace) VALUES(?, ?, ?)'
                ' ON CONFLICT(userId) DO UPDATE SET revision = excluded.revision,'
                ' workspace = excluded.workspace',
                (user_id, current, json.dumps(workspace, ensure_ascii=False)))
        return {'revision': current, 'workspace': workspace}


class Handler(BaseHTTPRequestHandler):
    """Routing API. Strona, /api/health i /api/me sa publiczne; dane wymagaja sesji."""

    server_version = 'na-fali'
    protocol_version = 'HTTP/1.1'
    AUTH_ROUTES = ('/api/register', '/api/login', '/api/logout')

    def log_message(self, fmt, *args):
        if os.environ.get('ACCESS_LOG'):
            super().log_message(fmt, *args)

    def handle_one_request(self):
        self._cookie = None              # keep-alive: jeden handler obsluguje wiele zadan
        super().handle_one_request()

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
        if getattr(self, '_cookie', None):
            self.send_header('Set-Cookie', self._cookie)
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

    def _body(self, limit=MAX_BODY):
        length = int(self.headers.get('Content-Length') or 0)
        if length > limit:
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
        except Unauthorized as error:
            self._send(401, {'error': str(error)})
        except Conflict as error:
            self._send(409, {'error': str(error)})
        except TooManyRequests as error:
            self._send(429, {'error': str(error)})
        except ValueError as error:          # BadRequest i ValueError z walidacji egzaminu
            self._send(400, {'error': str(error)})

    # --- sesja ---------------------------------------------------------

    def _cookie_header(self, token, clear=False):
        parts = [f'{COOKIE}={"" if clear else token}', 'Path=/', 'HttpOnly', 'SameSite=Lax',
                 f'Max-Age={0 if clear else SESSION_SECONDS}']
        # Secure tylko z jawnej flagi: origin mowi czystym HTTP za Cloudflare,
        # wiec ze schematu polaczenia nie da sie tego wywnioskowac.
        if self.server.secure_cookies:
            parts.append('Secure')
        return '; '.join(parts)

    def _session_token(self):
        # Reczny parser: SimpleCookie odrzuca caly naglowek przy cookie Cloudflare.
        header = '; '.join(self.headers.get_all('Cookie') or [])
        for part in header.split(';'):
            name, _, value = part.strip().partition('=')
            if name == COOKIE:
                return value.strip() or None
        return None

    def _user(self):
        user = self.server.store.session_user(self._session_token())
        if user is None:
            raise Unauthorized('wymagane logowanie')
        return user

    def _client_key(self):
        # Za Cloudflare prawdziwy adres jest w CF-Connecting-IP; firewall originu
        # wpuszcza tylko Cloudflare, wiec naglowkowi mozna ufac.
        return self.headers.get('CF-Connecting-IP') or self.client_address[0]

    def _start_session(self, user):
        token = self.server.store.create_session(user['id'])
        self._cookie = self._cookie_header(token)
        return {'user': {'username': user['username']}}

    def _register(self, body):
        self.server.limiter.check(self._client_key())
        user = self.server.store.register(body.get('username'), body.get('password'),
                                          body.get('code'), self.server.registration_code)
        return self._start_session(user)

    def _login(self, body):
        self.server.limiter.check(self._client_key())
        return self._start_session(self.server.store.login(body.get('username'), body.get('password')))

    def _logout(self):
        self.server.store.delete_session(self._session_token())
        self._cookie = self._cookie_header('', clear=True)
        return {'user': None}

    def _me(self):
        user = self.server.store.session_user(self._session_token())
        return {'user': None if user is None else {'username': user['username']},
                'codeRequired': bool(self.server.registration_code)}

    def _check_bank(self, body):
        if body.get('bankVersion') != BANK_VERSION:
            raise BadRequest(f'bankVersion: oczekiwano {BANK_VERSION!r}, otrzymano {body.get("bankVersion")!r}')

    # --- trasy ---------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':            # bez kontroli hosta: sonda kubeletu uzywa IP poda
            return self._send(200, {'status': 'ok'})
        if not self._host_allowed():
            return self._send(403, {'error': 'niedozwolony host albo origin'})
        if path in ('/', '/index.html'):
            return self._send(200, self.server.page, 'text/html; charset=utf-8')
        store = self.server.store
        if path == '/api/me':
            return self._dispatch(self._me)
        if path == '/api/state':
            return self._dispatch(lambda: store.state(self._user()['id']))
        if path == '/api/exams':
            return self._dispatch(lambda: store.exams(self._user()['id']))
        return self._send(404, {'error': 'nie znaleziono'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._guard():
            return
        if path not in self.AUTH_ROUTES + ('/api/attempts', '/api/exams'):
            return self._send(404, {'error': 'nie znaleziono'})
        try:                         # strumien zadania czytamy dokladnie raz
            body = self._body(AUTH_BODY if path in self.AUTH_ROUTES else MAX_BODY)
        except BadRequest as error:
            return self._send(400, {'error': str(error)})
        store = self.server.store
        if path == '/api/register':
            return self._dispatch(lambda: self._register(body))
        if path == '/api/login':
            return self._dispatch(lambda: self._login(body))
        if path == '/api/logout':
            return self._dispatch(self._logout)
        if path == '/api/attempts':
            return self._dispatch(lambda: (
                self._check_bank(body),
                store.save(self._user()['id'], body.get('attempts'), body.get('generation')))[1])
        return self._dispatch(lambda: (
            self._check_bank(body),
            store.exams(self._user()['id'], body.get('workspace'), body.get('revision')))[1])

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
        store = self.server.store
        return self._dispatch(lambda: (
            self._check_bank(body),
            store.clear(self._user()['id'], body.get('generation')))[1])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _truthy(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def make_server(port=0, db_path=None, host='127.0.0.1', allowed_hosts=None, page=PAGE,
                secure_cookies=None, registration_code=None):
    """Buduje serwer gotowy do serve_forever(). port=0 wybiera wolny port.

    secure_cookies: flaga Secure na cookie sesji (env SECURE_COOKIES).
    registration_code: kod wymagany przy rejestracji; pusty = rejestracja otwarta
    (env REGISTRATION_CODE).
    """
    server = Server((host, port), Handler)
    server.store = Store(db_path or ROOT / 'data' / 'course.sqlite3')
    server.page = Path(page).read_bytes()
    names = allowed_hosts if allowed_hosts is not None else os.environ.get('ALLOWED_HOSTS', '')
    if isinstance(names, str):
        names = [part.strip() for part in names.split(',') if part.strip()]
    server.allowed_hosts = LOOPBACK | {name.lower() for name in names}
    if secure_cookies is None:
        secure_cookies = _truthy(os.environ.get('SECURE_COOKIES', ''))
    if registration_code is None:
        registration_code = os.environ.get('REGISTRATION_CODE', '')
    server.secure_cookies = bool(secure_cookies)
    server.registration_code = (registration_code or '').strip()
    server.limiter = RateLimit()
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
