"""Walidacja stanu egzaminu probnego.

Trzymana osobno od server.py, bo to czysta logika regul UKE: zestaw blokow
tematycznych, limit czasu na blok i dozwolone odpowiedzi. Kazde naruszenie to
ValueError — warstwa HTTP mapuje je na 400.
"""

STATUSES = {'active', 'finished', 'abandoned'}


def _bank():
    # Import leniwy: server.py laduje BANK i siega tutaj z Store.exams(),
    # wiec import na poziomie modulu zrobilby cykl.
    from server import BANK, QUESTIONS
    return BANK, QUESTIONS


def _int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{label}: oczekiwano liczby calkowitej')
    return value


def _optional_int(value, label):
    return None if value is None else _int(value, label)


def validate_workspace(workspace):
    """Sprawdza cale workspace egzaminow. Podnosi ValueError przy bledzie."""
    bank, questions = _bank()
    rules = bank['examRules']

    if not isinstance(workspace, dict):
        raise ValueError('workspace: oczekiwano obiektu')
    if not isinstance(workspace.get('history'), list):
        raise ValueError('history: oczekiwano listy')
    for past in workspace['history']:
        _validate_exam(past, rules, questions, bank, historical=True)

    active = workspace.get('active')
    if active is not None:
        _validate_exam(active, rules, questions, bank, historical=False)
    return workspace


def _validate_exam(exam, rules, questions, bank, historical):
    if not isinstance(exam, dict):
        raise ValueError('exam: oczekiwano obiektu')
    if not isinstance(exam.get('id'), str) or not exam['id']:
        raise ValueError('exam.id: oczekiwano niepustego tekstu')

    if exam.get('contentRevision') != bank['contentRevision']:
        raise ValueError(
            f'contentRevision: oczekiwano {bank["contentRevision"]!r}, '
            f'otrzymano {exam.get("contentRevision")!r}')

    status = exam.get('status')
    if status not in STATUSES:
        raise ValueError(f'status: niedozwolona wartosc {status!r}')
    if historical and status == 'active':
        raise ValueError('history: egzamin w historii nie moze byc aktywny')

    _int(exam.get('startedAt'), 'startedAt')
    _optional_int(exam.get('finishedAt'), 'finishedAt')

    subjects = rules['subjects']
    blocks = exam.get('blocks')
    if not isinstance(blocks, list) or len(blocks) != len(subjects):
        raise ValueError(f'blocks: oczekiwano {len(subjects)} blokow')

    index = _int(exam.get('blockIndex'), 'blockIndex')
    if not 0 <= index < len(blocks):
        raise ValueError(f'blockIndex: poza zakresem ({index})')

    for position, (block, subject) in enumerate(zip(blocks, subjects)):
        _validate_block(block, subject, position, rules, questions)

    # Limit czasu liczy sie od startu biezacego bloku, nie od startu egzaminu.
    started = blocks[index].get('startedAt')
    if status == 'active' and started is not None:
        expected = started + rules['secondsPerSubject'] * 1000
        if exam.get('deadline') != expected:
            raise ValueError(
                f'deadline: oczekiwano {expected}, otrzymano {exam.get("deadline")}')


def _validate_block(block, subject, position, rules, questions):
    where = f'blocks[{position}]'
    if not isinstance(block, dict):
        raise ValueError(f'{where}: oczekiwano obiektu')
    if block.get('subject') != subject:
        raise ValueError(
            f'{where}.subject: oczekiwano {subject!r}, otrzymano {block.get("subject")!r}')

    started = _optional_int(block.get('startedAt'), f'{where}.startedAt')
    _optional_int(block.get('endedAt'), f'{where}.endedAt')
    reason = block.get('reason')
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f'{where}.reason: oczekiwano tekstu albo null')

    items = block.get('questions')
    if not isinstance(items, list):
        raise ValueError(f'{where}.questions: oczekiwano listy')
    if len(items) > rules['questionsPerSubject']:
        raise ValueError(
            f'{where}.questions: najwyzej {rules["questionsPerSubject"]} pytan')

    seen = set()
    for slot, item in enumerate(items):
        _validate_question(item, f'{where}.questions[{slot}]', subject, questions, started)
        if item['id'] in seen:
            raise ValueError(f'{where}.questions: powtorzone pytanie {item["id"]!r}')
        seen.add(item['id'])


def _validate_question(item, where, subject, questions, block_started):
    if not isinstance(item, dict):
        raise ValueError(f'{where}: oczekiwano obiektu')

    question = questions.get(item.get('id'))
    if question is None:
        raise ValueError(f'{where}.id: nieznane pytanie {item.get("id")!r}')
    if question.get('examSubject') != subject or not question.get('examEligible'):
        raise ValueError(f'{where}.id: pytanie spoza bloku {subject!r}')

    count = len(question['options'])
    order = item.get('order')
    if not isinstance(order, list) or sorted(order) != list(range(count)):
        raise ValueError(f'{where}.order: oczekiwano permutacji 0..{count - 1}')

    chosen = item.get('chosen')
    if chosen is not None:
        if isinstance(chosen, bool) or not isinstance(chosen, int):
            raise ValueError(f'{where}.chosen: oczekiwano liczby calkowitej albo null')
        if not 0 <= chosen < count:
            raise ValueError(f'{where}.chosen: poza zakresem ({chosen})')
        if block_started is None:
            raise ValueError(f'{where}.chosen: blok nie zostal rozpoczety')
