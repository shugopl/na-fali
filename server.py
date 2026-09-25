"""Na fali — serwer kursu krotkofalarskiego.

Strona jest samowystarczalna (`web/index.html` zawiera caly bank pytan w
`const DATA=`), a serwer dokłada trwalosc: konta uzytkownikow, ich podejscia
do pytan i stan egzaminu probnego w SQLite. BANK i BANK_VERSION sa parsowane
z tej samej strony, wiec tresc i format danych maja jedno zrodlo prawdy i nie
moga sie rozjechac miedzy klientem a backendem.

Tresc kursu jest publiczna; historia, postep i egzaminy naleza do zalogowanego
uzytkownika. Nazwa konta to adres e-mail, potwierdzany kodem wysylanym poczta
(rejestracja i reset hasla). Administrator ma panel pod /api/admin/*.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / 'web' / 'index.html'
MARKER = 'const DATA='
VERSION_RE = re.compile(r"const BANK_VERSION='([^']+)'")
LOOPBACK = {'127.0.0.1', 'localhost', '::1', '[::1]'}
MAX_BODY = 8 * 1024 * 1024
AUTH_BODY = 4096                      # trasy kont nie potrzebuja wiecej
COOKIE = 'nafali_session'
SESSION_SECONDS = 30 * 24 * 3600
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
EMAIL_MAX = 254
PASSWORD_MIN, PASSWORD_MAX = 8, 128
CODE_TTL = 900                        # kod z e-maila wazny 15 minut
CODE_MAX_TRIES = 5
CODE_RESEND_SECONDS = 60
UNVERIFIED_TTL = 24 * 3600            # niepotwierdzone konta znikaja po dobie
# 16 MiB i ~0,1 s na hash. Nie podnosic n do 2**15 bez maxmem — OpenSSL ma
# domyslny limit 32 MiB i scrypt rzuca wtedy wyjatek zamiast liczyc.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
LEGACY_USER = '#legacy'               # nie jest adresem e-mail, wiec nie da sie na niego zalogowac
_scrypt = hashlib.scrypt              # brak scrypt w OpenSSL ma wywalic import, nie pierwsza rejestracje
ADMIN_USER_RE = re.compile(r'^/api/admin/users/(\d+)(/export)?$')


class BadRequest(ValueError):
    """Dane od klienta nie przechodza walidacji -> HTTP 400."""


class Unauthorized(Exception):
    """Brak waznej sesji albo zle poswiadczenia -> HTTP 401."""


class Unverified(Unauthorized):
    """Poprawne haslo, ale adres e-mail nie zostal jeszcze potwierdzony -> 401 + unverified."""


class Forbidden(Exception):
    """Zalogowany, ale bez uprawnien (albo rejestracja zamknieta) -> HTTP 403."""


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


def exam_result(run):
    """Ta sama regula co ExamEngine.result na stronie: blok zdany od passPerSubject
    poprawnych, egzamin zdany gdy ukonczony i wszystkie bloki zdane. `chosen` to
    indeks oryginalnej opcji, `order` sluzy tylko do wyswietlania."""
    rules = BANK['examRules']
    scores = []
    for block in run.get('blocks') or []:
        score = 0
        for item in block.get('questions') or []:
            question = QUESTIONS.get(item.get('id'))
            if question is not None and item.get('chosen') == question['answer']:
                score += 1
        scores.append(score)
    passed = (run.get('status') == 'complete' and len(scores) == len(rules['subjects'])
              and all(score >= rules['passPerSubject'] for score in scores))
    return sum(scores), passed


# --- hasla, kody, sesje --------------------------------------------------------

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


def _code_hash(user_id, purpose, code):
    return hashlib.sha256(f'{user_id}:{purpose}:{code}'.encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def normalize_email(value):
    if not isinstance(value, str):
        raise BadRequest('email: oczekiwano tekstu')
    email = value.strip().lower()
    if len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
        raise BadRequest('email: podaj poprawny adres e-mail')
    return email


def is_email(value):
    return isinstance(value, str) and len(value) <= EMAIL_MAX and bool(EMAIL_RE.match(value))


def check_password(password):
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise BadRequest(f'password: od {PASSWORD_MIN} do {PASSWORD_MAX} znakow')
    return password


def mail_text(purpose, code):
    if purpose == 'verify':
        return ('Na fali — kod weryfikacyjny',
                f'Twój kod potwierdzający adres e-mail w kursie Na fali: {code}\n\n'
                'Kod jest ważny 15 minut. Wpisz go w oknie rejestracji na stronie kursu.\n'
                'Jeśli to nie Ty zakładasz konto, zignoruj tę wiadomość — konto bez '
                'potwierdzenia zniknie samo.\n')
    return ('Na fali — reset hasła',
            f'Twój kod do ustawienia nowego hasła w kursie Na fali: {code}\n\n'
            'Kod jest ważny 15 minut. Jeśli nie prosiłeś o reset hasła, zignoruj tę '
            'wiadomość — hasło pozostaje bez zmian.\n')


class Mailer:
    """Wysylka przez SMTP (STARTTLS). Blad wysylki w tle laduje na stderr."""

    configured = True

    def __init__(self, host, port=587, user='', password='', sender=''):
        self.host, self.port = host, int(port or 587)
        self.user, self.password = user, password
        self.sender = sender or user

    def send(self, to, subject, body):
        message = EmailMessage()
        message['From'] = self.sender
        message['To'] = to
        message['Subject'] = subject
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(message)

    def send_async(self, to, subject, body):
        threading.Thread(target=self._safe_send, args=(to, subject, body), daemon=True).start()

    def _safe_send(self, to, subject, body):
        try:
            self.send(to, subject, body)
        except Exception as error:      # noqa: BLE001 — watek w tle, tylko log
            print(f'mail: blad wysylki do {to}: {error}', file=sys.stderr, flush=True)


class ConsoleMailer:
    """Bez SMTP_HOST: tresc wiadomosci trafia na stdout (praca lokalna)."""

    configured = False

    def send(self, to, subject, body):
        print(f'mail: do {to} | {subject}\n{body}', flush=True)

    send_async = send


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

V4_COLUMNS = (
    ('isAdmin', 'INTEGER NOT NULL DEFAULT 0'),
    ('verifiedAt', 'TEXT'),
    ('lastLoginAt', 'TEXT'),
)

V4_DDL = (
    """CREATE TABLE IF NOT EXISTS codes(
        userId    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        purpose   TEXT NOT NULL,
        codeHash  TEXT NOT NULL,
        issuedAt  INTEGER NOT NULL,
        expiresAt INTEGER NOT NULL,
        tries     INTEGER NOT NULL DEFAULT 0)""",
)

EMAIL_LIKE = "'%_@_%.__%'"           # zgrubny filtr SQL; pelna walidacja w Pythonie


class Store:
    """Trwalosc kont, podejsc i egzaminow.

    Zapisy sa idempotentne: ponowione podejscie o tym samym id jest scalane,
    a nie duplikowane ani nadpisywane pustka. Dzieki temu opozniony retry z
    klienta nie kasuje odpowiedzi zapisanej w miedzyczasie. Kazda metoda
    danych bierze `user_id` jako pierwszy argument — historia jest per konto.
    """

    SCHEMA_VERSION = 4

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

    def _columns(self, table):
        return {row[1] for row in self._db.execute(f'PRAGMA table_info({table})')}

    def _migrate(self):
        with self._lock, self._db:
            # Jawny BEGIN: w trybie legacy sqlite3 nie otwiera transakcji przed DDL,
            # a caly krok migracji ma byc jedna transakcja.
            self._db.execute('BEGIN')
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0 and 'attempts' in self._tables():
                version = 1          # baza sprzed wprowadzenia user_version
            if version < 1:
                self._create_v3()
                self._upgrade_v3_to_v4()
            else:
                if version < 2:
                    self._upgrade_v1_to_v2()
                if version < 3:
                    self._upgrade_v2_to_v3()
                if version < 4:
                    self._upgrade_v3_to_v4()
            self._db.execute(f'PRAGMA user_version={self.SCHEMA_VERSION}')

    def _create_v3(self):
        # Pojedyncze execute, nie executescript: ten drugi commituje po drodze.
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
        self._db.execute('BEGIN')        # executescript zamknal transakcje — otworz na nowo

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

    def _upgrade_v3_to_v4(self):
        """v4: nazwa konta to e-mail, konta potwierdzane kodem, admini, kody.

        Idempotentny (rollback obrazu stempluje user_version=3, wiec krok moze
        pojsc drugi raz na bazie v4). Konta bez adresu e-mail i bez danych nie
        maja jak zostac potwierdzone, wiec znikaja; istniejace konta e-mailowe
        uznajemy za potwierdzone, zeby nikogo nie wylogowac.
        """
        present = self._columns('users')
        for name, definition in V4_COLUMNS:
            if name not in present:
                self._db.execute(f'ALTER TABLE users ADD COLUMN {name} {definition}')
        for statement in V4_DDL:
            self._db.execute(statement)
        removed = self._db.execute(
            f'DELETE FROM users WHERE username NOT LIKE {EMAIL_LIKE}'
            ' AND id NOT IN (SELECT userId FROM attempts)'
            ' AND id NOT IN (SELECT userId FROM exams)').rowcount
        if removed:
            print(f'migracja v4: usunieto {removed} kont bez adresu e-mail i bez danych',
                  file=sys.stderr, flush=True)
        self._db.execute('UPDATE users SET username = lower(username)')
        self._db.execute(
            f'UPDATE users SET verifiedAt = createdAt WHERE verifiedAt IS NULL'
            f' AND username LIKE {EMAIL_LIKE}')

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

    # --- ustawienia (meta) ---------------------------------------------

    def _meta(self, key, default=None):
        row = self._db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return default if row is None else row[0]

    def _set_meta(self, key, value):
        self._db.execute(
            'INSERT INTO meta(key, value) VALUES(?, ?)'
            ' ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))

    def seed_settings(self, registration_code=''):
        """Ustawia tylko brakujace klucze — panel admina ma pierwszenstwo przed env."""
        with self._lock, self._db:
            if self._meta('registrationOpen') is None:
                self._set_meta('registrationOpen', '1')
            if self._meta('registrationCode') is None:
                self._set_meta('registrationCode', (registration_code or '').strip())

    def settings(self):
        with self._lock:
            return {'registrationOpen': self._meta('registrationOpen', '1') == '1',
                    'registrationCode': self._meta('registrationCode', '')}

    def update_settings(self, registration_open=None, registration_code=None):
        with self._lock, self._db:
            if registration_open is not None:
                self._set_meta('registrationOpen', '1' if registration_open else '0')
            if registration_code is not None:
                if not isinstance(registration_code, str) or len(registration_code) > 64:
                    raise BadRequest('registrationCode: tekst do 64 znakow')
                self._set_meta('registrationCode', registration_code.strip())
        return self.settings()

    # --- konta ---------------------------------------------------------

    USER_COLUMNS = 'id, username, passwordHash, generation, isAdmin, verifiedAt, createdAt, lastLoginAt'

    @staticmethod
    def _user(row):
        if row is None:
            return None
        return {'id': row[0], 'username': row[1], 'passwordHash': row[2], 'generation': row[3],
                'isAdmin': bool(row[4]), 'verifiedAt': row[5], 'createdAt': row[6],
                'lastLoginAt': row[7]}

    @staticmethod
    def safe(user):
        """Pelny rekord bez hasha — do panelu admina."""
        return None if user is None else {k: v for k, v in user.items() if k != 'passwordHash'}

    @staticmethod
    def public(user):
        """To, co widzi strona o zalogowanym koncie."""
        if user is None:
            return None
        return {'username': user['username'], 'email': user['username'],
                'isAdmin': bool(user.get('isAdmin'))}

    def find_user(self, username):
        with self._lock:
            row = self._db.execute(
                f'SELECT {self.USER_COLUMNS} FROM users WHERE username = ?', (username,)).fetchone()
        return self._user(row)

    def get_user(self, user_id):
        with self._lock:
            row = self._db.execute(
                f'SELECT {self.USER_COLUMNS} FROM users WHERE id = ?', (user_id,)).fetchone()
        return self._user(row)

    def register(self, email, password, code=None):
        """Zaklada niepotwierdzone konto (albo odswieza istniejace niepotwierdzone).

        Kod z e-maila wysyla warstwa HTTP (issue_code); konto staje sie uzywalne
        po verify(). Ponowna rejestracja tego samego, niepotwierdzonego adresu
        podmienia haslo — dzieki temu nikt nie zablokuje cudzego adresu.
        """
        email = normalize_email(email)
        check_password(password)
        settings = self.settings()
        if not settings['registrationOpen']:
            raise Forbidden('rejestracja jest obecnie zamknieta')
        required = settings['registrationCode']
        if required and not (isinstance(code, str) and hmac.compare_digest(code.strip(), required)):
            raise BadRequest('code: nieprawidlowy kod rejestracji')

        digest = hash_password(password)     # kosztowne, wiec poza blokada
        with self._lock, self._db:
            self._db.execute(
                'DELETE FROM users WHERE verifiedAt IS NULL AND isAdmin = 0 AND createdAt < ?',
                (datetime.fromtimestamp(time.time() - UNVERIFIED_TTL, timezone.utc)
                 .strftime('%Y-%m-%dT%H:%M:%SZ'),))
            row = self._user(self._db.execute(
                f'SELECT {self.USER_COLUMNS} FROM users WHERE username = ?', (email,)).fetchone())
            if row is not None and row['verifiedAt']:
                raise Conflict('username: ten adres e-mail ma juz konto — zaloguj sie albo zresetuj haslo')
            if row is not None:
                self._db.execute('UPDATE users SET passwordHash = ? WHERE id = ?', (digest, row['id']))
                return row | {'passwordHash': digest}
            cursor = self._db.execute(
                'INSERT INTO users(username, passwordHash, generation, createdAt)'
                ' VALUES(?, ?, 0, ?)', (email, digest, _now()))
            return self._user(self._db.execute(
                f'SELECT {self.USER_COLUMNS} FROM users WHERE id = ?', (cursor.lastrowid,)).fetchone())

    def issue_code(self, user_id, purpose):
        """Nowy 6-cyfrowy kod (jeden aktywny na konto); zwraca go jawnie do wysylki."""
        now = int(time.time())
        code = f'{secrets.randbelow(10 ** 6):06d}'
        with self._lock, self._db:
            row = self._db.execute(
                'SELECT issuedAt FROM codes WHERE userId = ?', (user_id,)).fetchone()
            if row is not None and now - row[0] < CODE_RESEND_SECONDS:
                raise Conflict('kod zostal juz wyslany — odczekaj minute przed ponowna proba')
            self._db.execute(
                'INSERT INTO codes(userId, purpose, codeHash, issuedAt, expiresAt, tries)'
                ' VALUES(?, ?, ?, ?, ?, 0) ON CONFLICT(userId) DO UPDATE SET'
                ' purpose = excluded.purpose, codeHash = excluded.codeHash,'
                ' issuedAt = excluded.issuedAt, expiresAt = excluded.expiresAt, tries = 0',
                (user_id, purpose, _code_hash(user_id, purpose, code), now, now + CODE_TTL))
        return code

    def _consume_code(self, user_id, purpose, code):
        """Wewnatrz blokady i transakcji: zuzywa kod albo podnosi BadRequest."""
        row = self._db.execute(
            'SELECT purpose, codeHash, expiresAt, tries FROM codes WHERE userId = ?',
            (user_id,)).fetchone()
        # Nieudana proba musi zostac zapisana mimo wyjatku, ktory cofnie transakcje
        # wywolujacego — stad jawny commit przed raise.
        if row is None or row[0] != purpose or row[2] < int(time.time()):
            self._db.execute('DELETE FROM codes WHERE userId = ?', (user_id,))
            self._db.commit()
            raise BadRequest('code: brak waznego kodu — popros o nowy')
        if not isinstance(code, str) or not hmac.compare_digest(
                _code_hash(user_id, purpose, code.strip()), row[1]):
            tries = row[3] + 1
            if tries >= CODE_MAX_TRIES:
                self._db.execute('DELETE FROM codes WHERE userId = ?', (user_id,))
                self._db.commit()
                raise BadRequest('code: za duzo blednych prob — popros o nowy kod')
            self._db.execute('UPDATE codes SET tries = ? WHERE userId = ?', (tries, user_id))
            self._db.commit()
            raise BadRequest('code: nieprawidlowy kod')
        self._db.execute('DELETE FROM codes WHERE userId = ?', (user_id,))

    def verify(self, email, password, code):
        """Potwierdza adres: wymaga hasla, zeby cudzy kod nie przejal cudzego konta."""
        email = normalize_email(email)
        user = self.find_user(email)
        if user is None or not verify_password(password if isinstance(password, str) else '',
                                               user['passwordHash']):
            if user is None:
                verify_password('', _dummy_hash())
            raise Unauthorized('nieprawidlowy adres e-mail lub haslo')
        with self._lock, self._db:
            if not user['verifiedAt']:
                self._consume_code(user['id'], 'verify', code)
                self._db.execute('UPDATE users SET verifiedAt = ? WHERE id = ?', (_now(), user['id']))
        return self.get_user(user['id'])

    def login(self, email, password):
        if not isinstance(email, str) or not isinstance(password, str):
            raise BadRequest('email/password: oczekiwano tekstu')
        user = self.find_user(email.strip().lower())
        stored = user['passwordHash'] if user else _dummy_hash()
        if not verify_password(password, stored) or user is None:
            raise Unauthorized('nieprawidlowy adres e-mail lub haslo')
        if not user['verifiedAt']:
            raise Unverified('adres e-mail nie zostal jeszcze potwierdzony')
        with self._lock, self._db:
            self._db.execute('UPDATE users SET lastLoginAt = ? WHERE id = ?', (_now(), user['id']))
        return user

    def request_reset(self, email):
        """Zwraca uzytkownika do wyslania kodu albo None — odpowiedz HTTP jest taka sama."""
        try:
            email = normalize_email(email)
        except BadRequest:
            return None
        user = self.find_user(email)
        if user is None or not user['verifiedAt']:
            return None
        return user

    def confirm_reset(self, email, code, password):
        email = normalize_email(email)
        check_password(password)
        user = self.find_user(email)
        if user is None:
            raise BadRequest('code: brak waznego kodu — popros o nowy')
        digest = hash_password(password)
        with self._lock, self._db:
            self._consume_code(user['id'], 'reset', code)
            self._db.execute(
                'UPDATE users SET passwordHash = ?, verifiedAt = COALESCE(verifiedAt, ?) WHERE id = ?',
                (digest, _now(), user['id']))
            self._db.execute('DELETE FROM sessions WHERE userId = ?', (user['id'],))
        return self.get_user(user['id'])

    def ensure_admin(self, email, password, reset=False):
        """Idempotentny bootstrap: tworzy admina albo nadaje uprawnienia istniejacemu."""
        email = normalize_email(email)
        user = self.find_user(email)
        if user is None:
            check_password(password)
            digest = hash_password(password)
            with self._lock, self._db:
                self._db.execute(
                    'INSERT INTO users(username, passwordHash, generation, isAdmin, verifiedAt, createdAt)'
                    ' VALUES(?, ?, 0, 1, ?, ?)', (email, digest, _now(), _now()))
            return self.find_user(email)
        digest = None
        if reset:
            check_password(password)
            digest = hash_password(password)
        with self._lock, self._db:
            self._db.execute(
                'UPDATE users SET isAdmin = 1, verifiedAt = COALESCE(verifiedAt, ?) WHERE id = ?',
                (_now(), user['id']))
            if digest:
                self._db.execute('UPDATE users SET passwordHash = ? WHERE id = ?', (digest, user['id']))
        return self.get_user(user['id'])

    # --- sesje ---------------------------------------------------------

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
                f'SELECT {", ".join("u." + c.strip() for c in self.USER_COLUMNS.split(","))}'
                ' FROM sessions s JOIN users u ON u.id = s.userId'
                ' WHERE s.tokenHash = ? AND s.expiresAt >= ?',
                (_token_hash(token), int(time.time()))).fetchone()
        return self._user(row)

    def delete_session(self, token):
        if not token:
            return
        with self._lock, self._db:
            self._db.execute('DELETE FROM sessions WHERE tokenHash = ?', (_token_hash(token),))

    def revoke_sessions(self, user_id):
        with self._lock, self._db:
            self._db.execute('DELETE FROM sessions WHERE userId = ?', (user_id,))

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

    def _attempt_rows(self, user_id):
        return self._db.execute(
            'SELECT id, sessionId, questionId, at, chosen, help'
            ' FROM attempts WHERE userId = ? ORDER BY at, id', (user_id,)).fetchall()

    def state(self, user_id):
        """Ksztalt, ktorego oczekuje `acceptState` na stronie (schemaVersion + bankVersion)."""
        with self._lock:
            rows = self._attempt_rows(user_id)
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

    # --- administracja -------------------------------------------------

    @staticmethod
    def _exam_stats(workspace_json):
        finished = passed = 0
        best = None
        if workspace_json:
            history = (json.loads(workspace_json).get('history') or [])
            for run in history:
                if run.get('status') != 'complete':
                    continue
                total, ok = exam_result(run)
                finished += 1
                passed += int(ok)
                best = total if best is None else max(best, total)
        return finished, passed, best

    def users(self):
        """Lista kont ze statystykami do panelu admina."""
        with self._lock:
            rows = self._db.execute(
                f'SELECT {self.USER_COLUMNS} FROM users ORDER BY createdAt, id').fetchall()
            attempts = self._db.execute(
                'SELECT userId, questionId, chosen, help FROM attempts').fetchall()
            exams = dict(self._db.execute('SELECT userId, workspace FROM exams').fetchall())
        totals = {}
        for user_id, question_id, chosen, help in attempts:
            entry = totals.setdefault(user_id, [0, 0])
            entry[0] += 1
            question = QUESTIONS.get(question_id)
            if question is not None and chosen == question['answer'] and not help:
                entry[1] += 1
        result = []
        for row in rows:
            user = self._user(row)
            count, correct = totals.get(user['id'], (0, 0))
            finished, passed, best = self._exam_stats(exams.get(user['id']))
            result.append({
                'id': user['id'], 'email': user['username'], 'isAdmin': user['isAdmin'],
                'verifiedAt': user['verifiedAt'], 'createdAt': user['createdAt'],
                'lastLoginAt': user['lastLoginAt'], 'attempts': count, 'correct': correct,
                'examsFinished': finished, 'examsPassed': passed, 'bestScore': best,
            })
        return result

    def overview(self):
        with self._lock:
            users = self._db.execute(
                'SELECT COUNT(*), SUM(verifiedAt IS NOT NULL), SUM(isAdmin) FROM users').fetchone()
            recent = self._db.execute(
                'SELECT COUNT(*) FROM users WHERE createdAt >= ?',
                (datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc)
                 .strftime('%Y-%m-%dT%H:%M:%SZ'),)).fetchone()[0]
            attempts = self._db.execute('SELECT questionId, chosen, help FROM attempts').fetchall()
            exams = self._db.execute('SELECT workspace FROM exams').fetchall()
        subjects = {subject: {'attempts': 0, 'correct': 0, 'helped': 0}
                    for subject in BANK['examRules']['subjects']}
        for question_id, chosen, help in attempts:
            question = QUESTIONS.get(question_id)
            if question is None or question['section'] not in subjects:
                continue
            entry = subjects[question['section']]
            entry['attempts'] += 1
            if chosen == question['answer']:
                if help:
                    entry['helped'] += 1
                else:
                    entry['correct'] += 1
        finished = passed = 0
        for (workspace,) in exams:
            done, ok, _ = self._exam_stats(workspace)
            finished += done
            passed += ok
        return {
            'users': {'total': users[0], 'verified': users[1] or 0, 'admins': users[2] or 0,
                      'lastWeek': recent},
            'attempts': len(attempts),
            'exams': {'finished': finished, 'passed': passed},
            'subjects': subjects,
        }

    def user_export(self, user_id):
        with self._lock:
            rows = self._attempt_rows(user_id)
            _, stored = self._revision(user_id)
        return {
            'schemaVersion': 2, 'bankVersion': BANK_VERSION,
            'attempts': [{'id': r[0], 'sessionId': r[1], 'questionId': r[2],
                          'at': r[3], 'chosen': r[4], 'help': bool(r[5])} for r in rows],
            'exams': json.loads(stored) if stored else {'active': None, 'history': []},
        }

    def delete_user(self, user_id):
        with self._lock, self._db:
            if not self._db.execute('DELETE FROM users WHERE id = ?', (user_id,)).rowcount:
                raise BadRequest('user: nie ma takiego konta')

    def set_admin(self, user_id, flag):
        with self._lock, self._db:
            self._db.execute('UPDATE users SET isAdmin = ? WHERE id = ?', (int(bool(flag)), user_id))
        return self.get_user(user_id)

    def mark_verified(self, user_id):
        with self._lock, self._db:
            self._db.execute(
                'UPDATE users SET verifiedAt = COALESCE(verifiedAt, ?) WHERE id = ?', (_now(), user_id))
            self._db.execute('DELETE FROM codes WHERE userId = ?', (user_id,))
        return self.get_user(user_id)

    def clear_all(self, user_id):
        """Admin: czysci historie i egzaminy konta, uniewazniajac otwarte karty."""
        with self._lock, self._db:
            self._db.execute('DELETE FROM attempts WHERE userId = ?', (user_id,))
            self._db.execute('DELETE FROM exams WHERE userId = ?', (user_id,))
            self._db.execute('UPDATE users SET generation = generation + 1 WHERE id = ?', (user_id,))

    def backup(self):
        """Spojna kopia pliku bazy: osobne polaczenie (czytelnik WAL), bez blokady Store."""
        source = sqlite3.connect(self.path, timeout=30)
        target = sqlite3.connect(':memory:')
        try:
            source.backup(target)
            image = bytearray(target.serialize())
            # Naglowek kopii mowi "WAL" (bajty 18-19 = 2), a pliku WAL obok nie ma —
            # przestawiamy na tryb rollback, zeby kopia otwierala sie sama.
            image[18] = image[19] = 1
            return bytes(image)
        finally:
            target.close()
            source.close()


class Handler(BaseHTTPRequestHandler):
    """Routing API. Strona, /api/health i /api/me sa publiczne; dane wymagaja sesji."""

    server_version = 'na-fali'
    protocol_version = 'HTTP/1.1'
    AUTH_ROUTES = ('/api/register', '/api/verify', '/api/login', '/api/logout',
                   '/api/reset', '/api/reset/confirm')
    CODE_ROUTES = ('/api/verify', '/api/reset/confirm')

    def log_message(self, fmt, *args):
        if os.environ.get('ACCESS_LOG'):
            super().log_message(fmt, *args)

    def handle_one_request(self):
        self._cookie = None              # keep-alive: jeden handler obsluguje wiele zadan
        super().handle_one_request()

    # --- pomocnicze ----------------------------------------------------

    def _send(self, status, payload=None, content_type='application/json; charset=utf-8',
              extra_headers=()):
        body = b'' if payload is None else (
            payload if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        for name, value in extra_headers:
            self.send_header(name, value)
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
        except Unverified as error:
            self._send(401, {'error': str(error), 'unverified': True})
        except Unauthorized as error:
            self._send(401, {'error': str(error)})
        except Forbidden as error:
            self._send(403, {'error': str(error)})
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

    def _admin(self):
        user = self._user()
        if not user['isAdmin']:
            raise Forbidden('wymagane uprawnienia administratora')
        return user

    def _client_key(self):
        # Za Cloudflare prawdziwy adres jest w CF-Connecting-IP; firewall originu
        # wpuszcza tylko Cloudflare, wiec naglowkowi mozna ufac.
        return self.headers.get('CF-Connecting-IP') or self.client_address[0]

    def _start_session(self, user):
        token = self.server.store.create_session(user['id'])
        self._cookie = self._cookie_header(token)
        return {'user': Store.public(user)}

    def _send_code(self, user, purpose):
        code = self.server.store.issue_code(user['id'], purpose)
        subject, body = mail_text(purpose, code)
        self.server.mailer.send_async(user['username'], subject, body)

    def _register(self, body):
        self.server.limiter.check(self._client_key())
        user = self.server.store.register(body.get('email'), body.get('password'), body.get('code'))
        self._send_code(user, 'verify')
        return {'pending': True, 'email': user['username']}

    def _verify(self, body):
        self.server.code_limiter.check(self._client_key())
        user = self.server.store.verify(body.get('email'), body.get('password'), body.get('code'))
        return self._start_session(user)

    def _login(self, body):
        self.server.limiter.check(self._client_key())
        return self._start_session(self.server.store.login(body.get('email'), body.get('password')))

    def _logout(self):
        self.server.store.delete_session(self._session_token())
        self._cookie = self._cookie_header('', clear=True)
        return {'user': None}

    def _reset(self, body):
        self.server.limiter.check(self._client_key())
        user = self.server.store.request_reset(body.get('email'))
        if user is not None:
            self._send_code(user, 'reset')
        return {'pending': True}         # zawsze tak samo: nie zdradzamy, czy adres istnieje

    def _reset_confirm(self, body):
        self.server.code_limiter.check(self._client_key())
        user = self.server.store.confirm_reset(body.get('email'), body.get('code'), body.get('password'))
        return self._start_session(user)

    def _me(self):
        store = self.server.store
        settings = store.settings()
        return {'user': Store.public(store.session_user(self._session_token())),
                'codeRequired': bool(settings['registrationCode']),
                'registrationOpen': settings['registrationOpen'],
                'mailConfigured': self.server.mailer.configured}

    def _check_bank(self, body):
        if body.get('bankVersion') != BANK_VERSION:
            raise BadRequest(f'bankVersion: oczekiwano {BANK_VERSION!r}, otrzymano {body.get("bankVersion")!r}')

    # --- administracja -------------------------------------------------

    def _admin_overview(self):
        self._admin()
        store = self.server.store
        return store.overview() | {'settings': store.settings(),
                                   'mailConfigured': self.server.mailer.configured}

    def _admin_user_action(self, user_id, body):
        admin = self._admin()
        store = self.server.store
        target = store.get_user(user_id)
        if target is None:
            raise BadRequest('user: nie ma takiego konta')
        action = body.get('action')
        if action in ('demote', 'delete') and target['id'] == admin['id']:
            raise BadRequest('nie mozna odebrac uprawnien ani usunac wlasnego konta')
        if action == 'promote':
            return {'user': Store.safe(store.set_admin(user_id, True))}
        if action == 'demote':
            return {'user': Store.safe(store.set_admin(user_id, False))}
        if action == 'verify':
            return {'user': Store.safe(store.mark_verified(user_id))}
        if action == 'clear':
            store.clear_all(user_id)
            return {'ok': True}
        if action == 'logout':
            store.revoke_sessions(user_id)
            return {'ok': True}
        if action == 'reset':
            if not is_email(target['username']) or not target['verifiedAt']:
                raise BadRequest('reset: konto bez potwierdzonego adresu e-mail')
            self._send_code(target, 'reset')
            return {'ok': True}
        raise BadRequest(f'action: nieznana akcja {action!r}')

    def _admin_delete_user(self, user_id):
        admin = self._admin()
        if user_id == admin['id']:
            raise BadRequest('nie mozna usunac wlasnego konta')
        self.server.store.delete_user(user_id)
        return {'ok': True}

    def _admin_settings(self, body):
        self._admin()
        open_flag = body.get('registrationOpen')
        if open_flag is not None and not isinstance(open_flag, bool):
            raise BadRequest('registrationOpen: oczekiwano wartosci logicznej')
        return self.server.store.update_settings(open_flag, body.get('registrationCode'))

    def _admin_mail_test(self):
        admin = self._admin()
        try:
            self.server.mailer.send(admin['username'], 'Na fali — test poczty',
                                    'Poczta serwera kursu Na fali dziala. Ten e-mail wyslano z panelu administracyjnego.\n')
        except Exception as error:       # noqa: BLE001 — tresc bledu SMTP ma trafic do panelu
            raise BadRequest(f'wysylka nie powiodla sie: {error}') from error
        return {'ok': True, 'to': admin['username'], 'configured': self.server.mailer.configured}

    def _admin_backup(self):
        try:
            self._admin()
        except Unauthorized as error:
            return self._send(401, {'error': str(error)})
        except Forbidden as error:
            return self._send(403, {'error': str(error)})
        data = self.server.store.backup()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        return self._send(200, data, 'application/octet-stream',
                          [('Content-Disposition', f'attachment; filename="na-fali-{stamp}.sqlite3"')])

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
        if path == '/api/admin/overview':
            return self._dispatch(self._admin_overview)
        if path == '/api/admin/users':
            return self._dispatch(lambda: (self._admin(), store.users())[1])
        if path == '/api/admin/backup':
            return self._admin_backup()
        match = ADMIN_USER_RE.match(path)
        if match and match.group(2):
            user_id = int(match.group(1))
            return self._dispatch(lambda: (self._admin(), store.user_export(user_id))[1])
        return self._send(404, {'error': 'nie znaleziono'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._guard():
            return
        match = ADMIN_USER_RE.match(path)
        known = self.AUTH_ROUTES + ('/api/attempts', '/api/exams', '/api/admin/settings',
                                    '/api/admin/mail-test')
        if path not in known and not (match and not match.group(2)):
            return self._send(404, {'error': 'nie znaleziono'})
        try:                         # strumien zadania czytamy dokladnie raz
            body = self._body(AUTH_BODY if path in self.AUTH_ROUTES or path.startswith('/api/admin/')
                              else MAX_BODY)
        except BadRequest as error:
            return self._send(400, {'error': str(error)})
        store = self.server.store
        if path == '/api/register':
            return self._dispatch(lambda: self._register(body))
        if path == '/api/verify':
            return self._dispatch(lambda: self._verify(body))
        if path == '/api/login':
            return self._dispatch(lambda: self._login(body))
        if path == '/api/logout':
            return self._dispatch(self._logout)
        if path == '/api/reset':
            return self._dispatch(lambda: self._reset(body))
        if path == '/api/reset/confirm':
            return self._dispatch(lambda: self._reset_confirm(body))
        if path == '/api/admin/settings':
            return self._dispatch(lambda: self._admin_settings(body))
        if path == '/api/admin/mail-test':
            return self._dispatch(self._admin_mail_test)
        if match:
            user_id = int(match.group(1))
            return self._dispatch(lambda: self._admin_user_action(user_id, body))
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
        match = ADMIN_USER_RE.match(path)
        if match and not match.group(2):
            user_id = int(match.group(1))
            return self._dispatch(lambda: self._admin_delete_user(user_id))
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


def mailer_from_env(env=os.environ):
    host = env.get('SMTP_HOST', '').strip()
    if not host:
        return ConsoleMailer()
    return Mailer(host, env.get('SMTP_PORT', '587'), env.get('SMTP_USER', ''),
                  env.get('SMTP_PASSWORD', ''), env.get('SMTP_FROM', ''))


def make_server(port=0, db_path=None, host='127.0.0.1', allowed_hosts=None, page=PAGE,
                secure_cookies=None, registration_code=None, mailer=None,
                admin_email=None, admin_password=None, admin_reset=None):
    """Buduje serwer gotowy do serve_forever(). port=0 wybiera wolny port.

    secure_cookies: flaga Secure na cookie sesji (env SECURE_COOKIES).
    registration_code: ziarno ustawienia `registrationCode` przy pierwszym starcie
    (env REGISTRATION_CODE); potem rzadzi panel admina.
    mailer: obiekt z send()/send_async() (domyslnie z env SMTP_*; bez SMTP_HOST — stdout).
    admin_email/admin_password: idempotentny bootstrap administratora (env ADMIN_*).
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
    server.store.seed_settings(registration_code)
    server.mailer = mailer if mailer is not None else mailer_from_env()
    server.limiter = RateLimit()
    server.code_limiter = RateLimit(limit=20, window=300)
    if admin_email is None:
        admin_email = os.environ.get('ADMIN_EMAIL', '')
        admin_password = os.environ.get('ADMIN_PASSWORD', '')
        admin_reset = _truthy(os.environ.get('ADMIN_RESET_PASSWORD', ''))
    if admin_email:
        user = server.store.ensure_admin(admin_email, admin_password or '', bool(admin_reset))
        print(f'admin: {user["username"]}', file=sys.stderr, flush=True)
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
