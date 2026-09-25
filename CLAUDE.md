# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Na fali" — a Polish ham-radio course app. A single self-contained page
(`web/index.html`, ~1.7 MB) plus a Python standard-library server that adds
persistence and user accounts. No third-party runtime dependencies, no build step.

## Commands

```sh
# suites that currently pass (see "Known-failing tests")
python3 -m unittest tests.test_server tests.test_exams -v

# a single module / class / test
python3 -m unittest tests.test_exams
python3 -m unittest tests.test_server.PersistenceTests
python3 -m unittest tests.test_server.PersistenceTests.test_reset_rejects_stale_tabs

# full discovery — needs `-t .` because tests import server.py from the repo root
python3 -m unittest discover -s tests -t .

# run locally (ACCESS_LOG=1 turns on per-request logging, off by default)
HOST=127.0.0.1 PORT=8080 DB_PATH=./data/course.sqlite3 ACCESS_LOG=1 python3 -u server.py

# the image, as CI builds it — no build stage, it just copies the three inputs
docker build -t na-fali .
```

Python 3.12. There is no linter, formatter, or dependency manifest in the repo. The k3s host
has neither docker nor podman, which is why the image build lives in CI.

## Architecture

### Content has one source of truth: the HTML page

`BANK` is **parsed out of `web/index.html`** at import time — `server.py` locates the
`const DATA=` literal and brace-matches it (`_extract_data`). The page ships the whole
question bank inline so it renders standalone; the server re-reads that same literal so
backend and client can never disagree about content. `BANK_VERSION` (the *storage format*
version the page's `acceptState` checks, currently `'1'`; unrelated to `contentRevision`)
is parsed from the same file and there must be exactly one `const BANK_VERSION='…'` literal.

Consequences worth knowing before editing:
- Changing questions, Q-codes, bands or `contentRevision` means editing the JSON literal
  inside `web/index.html`, not a separate data file.
- `tests/test_content.py` asserts exact counts (297 questions, 75 qCatalog, 11 bandDetails,
  26 alphabet, 16 district regions across 9 prefixes) and that every question has exactly
  3 distinct options. Content edits break these deliberately.
- `Store.catalog()` materializes a flat index of that content into SQLite, rebuilt only when
  `BANK['contentRevision']` changes.

### HTTP surface

`server.py` routes on exact paths — no router, and no static file serving beyond the page
itself, which lives in memory (`PAGE`, read from `web/index.html` at import; tests inject
their own through `make_server(page=...)`). **Editing the page needs a server restart.**

| Route | Handler | Note |
|---|---|---|
| `GET /api/health` | `{"status": "ok"}` | the only route that skips the host check |
| `GET /`, `GET /index.html` | the in-memory page | public |
| `GET /api/me` | `{"user": {"username"} \| null, "codeRequired"}` | public; never 401 |
| `POST /api/register` | `Store.register` + session cookie | rate-limited, body ≤ 4 KB |
| `POST /api/login` | `Store.login` + session cookie | rate-limited, body ≤ 4 KB |
| `POST /api/logout` | `Store.delete_session`, clears cookie | |
| `GET /api/state` | `Store.state(user_id)` | needs session |
| `GET /api/exams` | `Store.exams(user_id)` | needs session |
| `POST /api/attempts` | `Store.save(user_id, attempts, generation)` | needs session + `bankVersion` |
| `POST /api/exams` | `Store.exams(user_id, workspace, revision)` | bumps `revision` |
| `DELETE /api/history` | `Store.clear(user_id, generation)` | bumps `generation`, returns full state |

`do_HEAD` delegates to `do_GET`. Status codes are mapped in one place, `_dispatch`:
`Unauthorized` -> 401, `Conflict` -> 409, `TooManyRequests` -> 429, `ValueError` (hence also
`BadRequest`) -> 400, unknown path -> 404, rejected host -> 403. Adding a route means touching
the right `do_*` method and nothing else. The current user is resolved *inside* the
`_dispatch` lambda (`self._user()`), so a missing session goes through the same mapping.

**Response shapes the page depends on** (all three broke the deployed site once):
`state()` must carry `schemaVersion: 2` and `bankVersion == BANK_VERSION` or the client's
`acceptState` throws and shows the misleading "Uruchom serwer kursu…" banner; an empty exam
workspace is `{"active": null, "history": []}`, never `null`; `clear()` returns the full
state because the client feeds it straight into `acceptState`.

### Persistence: merge, never overwrite

Every data method on `Store` takes `user_id` first; history, `generation` and the exam
workspace are per account. `Store.save()` merges attempts by `(userId, id)` rather than
replacing them — the same attempt `id` may legitimately exist under two accounts (exports
can be imported elsewhere). A delayed client retry
carrying `chosen: null` must not erase an answer stored in the meantime, and `help` is
sticky once set. Trying to change an already-stored answer to a different value raises
`Conflict`. All attempts are validated *before* the transaction opens, so one bad record in
an import rejects the whole batch without persisting any of it.

`Server` is a `ThreadingHTTPServer` with `daemon_threads`, but `Store` keeps a **single**
SQLite connection, opened `check_same_thread=False` with `timeout=30`. Concurrency rests on
SQLite's own locking rather than a connection pool, so keep transactions short; a long one
blocks every other request for up to 30 s and then raises.

Two optimistic-concurrency counters guard stale browser tabs:
- `generation` — bumped by `Store.clear()`; a `save()` with an old generation is a `Conflict`.
- `revision` — bumped by each `Store.exams()` write.

### Schema migrations

`Store` carries `SCHEMA_VERSION` (now 3) and migrates on open, keyed off `PRAGMA user_version`,
as a chain: v1 (snake_case columns, `meta(id, generation)`) -> v2 (camelCase, key-value `meta`)
-> v3 (`users`, `sessions`, `attempts` keyed by `(userId, id)`, `exams` keyed by `userId`,
`generation` on `users`). A v1 database with `user_version=0` but an existing `attempts` table
is treated as v1. The v2 -> v3 step moves any pre-existing shared history to a placeholder
user `#legacy` (name fails `USERNAME_RE`, hash `'!'`, so nobody can log in as it) and creates
no such user when the database is empty. Write new migration steps with per-statement
`execute`, not `executescript` — the latter commits mid-way and breaks the single transaction.
`tests/test_content.py` and `tests/test_server.py` exercise v1 and v2 fixtures.

### Circular import

`exam_validation.py` needs `BANK`, and `server.Store.exams()` needs `validate_workspace`.
The import in `Store.exams()` is **deliberately lazy** (function-local) to break the cycle.
Keep it that way.

### Request protection

- **Accounts.** Self-registration (`USERNAME_RE` = 3–32 of `[A-Za-z0-9_.-]`, case-insensitive
  uniqueness via `COLLATE NOCASE`, password 8–128). Passwords are `hashlib.scrypt` with
  `n=2**14, r=8, p=1` — **do not raise `n` to 2**15** without `maxmem`; OpenSSL's 32 MiB default
  makes scrypt raise instead of hash. Hashing happens outside `Store._lock`. Sessions are
  random tokens stored as SHA-256 in `sessions` (30 days; logout revokes; they survive restarts),
  sent as cookie `nafali_session; Path=/; HttpOnly; SameSite=Lax`. `Secure` is added only when
  `SECURE_COOKIES=1` — the origin speaks plain HTTP behind Cloudflare, so it cannot be inferred.
  The `Cookie` header is parsed by hand because `SimpleCookie` rejects Cloudflare's own cookies.
  `REGISTRATION_CODE` (env, empty = open) gates registration; `/api/me` exposes only whether a
  code is required. Login/register share an in-memory `RateLimit` (10 per 5 min per
  `CF-Connecting-IP` or peer address).
- The course content is public; anything under `/api/state`, `/api/exams`, `/api/attempts`,
  `/api/history` is 401 without a session. The page checks `/api/me` first and shows a login
  dialog (`#authDialog`, bottom-right `#accountBar`) instead of the storage-failure banner.
- `Host` and `Origin` must resolve to an allowed name — loopback always, plus whatever
  `ALLOWED_HOSTS` (comma-separated) lists. This blocks DNS rebinding. **Deploying under a new
  domain requires updating `ALLOWED_HOSTS` or every request 403s.**
- Mutating requests, including login and register, must carry `X-Na-Fali: 1`; a cross-site
  form cannot send a custom header. Mutating data requests must also send
  `bankVersion == BANK_VERSION` or get a 400.
- `/api/health` is intentionally exempt from the host check — the kubelet probe sends the
  pod IP as `Host`, so enforcing it there would crash-loop the pod.
- `validate_workspace` raises `ValueError`; `BadRequest` subclasses `ValueError`, so the HTTP
  layer maps both to 400 and `Conflict` to 409.

### Exam rules come from data

`BANK['examRules']` drives `exam_validation.py`: 4 subject blocks in a fixed order, 8 questions
each, 480 s per block. A block's `deadline` must equal that block's `startedAt + 480000` exactly,
`order` must be a permutation of the option indices, and a block that has not started cannot
hold answers.

## Deployment

k3s cluster `julia120`, ArgoCD, Traefik, published at `https://na-fali.shugo.com.pl`.
Manifests live in `k3s/`; `k3s/README.md` holds the runbook and `analiza-wdrozenia.md`
the fuller write-up of cluster state and open items.

- The default branch is `main` (CI triggers on it and nothing else).
- CI (`.github/workflows/deploy.yml`) tests, builds the image to ghcr.io, then writes the new
  tag into `k3s/20-deployment.yaml` and commits it back — ArgoCD syncs from that commit.
  Do not hand-edit the image tag expecting it to survive. Because every push to `main` earns
  a `deploy: <sha>` bot commit, **`git pull` before starting the next change** or the next
  push is rejected as non-fast-forward.
- SQLite sits on a ReadWriteOnce local-path volume, so the Deployment is pinned to
  `replicas: 1` with `strategy: Recreate`. Scaling out needs a different database.
- TLS terminates at Cloudflare; the origin serves plain HTTP. The cluster's IPv4 is private —
  the origin is reachable over IPv6 only.
- Because of that, the node's IPv6 inbound traffic is filtered by the nftables table
  `cf-origin`, generated and loaded by `k3s/host/cf-origin-refresh` (weekly systemd timer).
  It default-denies on `eth0`, allowing established/related, all ICMPv6, `tcp/22`, `tcp/6443`,
  and `tcp/80`+`tcp/443` only from Cloudflare's published IPv6 ranges — which also closes
  kubelet `10250` and flannel's unauthenticated VXLAN `8472/udp`. Diagnose with
  `nft list counters table inet cf-origin`; the node is an LXC container, so netfilter `log`
  goes to the host's kernel ring and is invisible from inside. Never restart
  `nftables.service` while k3s runs — `/etc/nftables.conf` starts with `flush ruleset`.
- The repo is public, so `k3s/argocd/application.yaml` clones it anonymously over HTTPS and
  needs no credential; `k3s/argocd/repo-secret.example.yaml` survives only as a template for
  a return to a private repo.
- Two hand-made secrets live outside git in the `na-fali` namespace: `ghcr-pull` (below) and the
  optional `na-fali-registration` (`key: code`), which the Deployment references with
  `optional: true` — absent secret means open registration. Env is read at start, so changing
  the code needs `kubectl -n na-fali rollout restart deploy/na-fali`.
- The **ghcr package is private** — package visibility is a separate setting from repo
  visibility. `k3s/20-deployment.yaml` therefore carries `imagePullSecrets: ghcr-pull`, and
  that secret is created by hand in the `na-fali` namespace. It is deliberately absent from
  git, so recreating the namespace means recreating it; ArgoCD does not know about it and
  will not prune it.
- ArgoCD's `prune` only removes resources it previously stamped with its own tracking
  metadata. The hand-applied `na-fali-placeholder` Deployment and ConfigMap are invisible to
  it and must be deleted manually — they carry the Service's selector label, so while they
  exist they take a share of the traffic.

## Known-failing tests (2 of 14)

Both fail on files that are absent from the repo, and both are left failing on purpose:

- `tests/test_content.py::test_existing_answer_keys_...` needs
  `tests/fixtures/v2-question-hashes.json` — 116 question fingerprints. Regenerating it from
  the current `DATA` would make the regression guard assert nothing, and which 116 of the 297
  questions are the v2 set is not recoverable from the repo.
- `tests/test_deployment.py` needs `scripts/configure-k3s.py`, a manifest generator.

CI therefore runs only `tests.test_server tests.test_exams`. Widen it once the files exist.

## Resolved: one architecture (stdlib + SQLite with built-in accounts)

`tests/test_deployment.py` describes a **different application** (Django with `accounts/`,
Postgres StatefulSet, `na-fali-db`/`na-fali-bootstrap`/`na-fali-db-admin` secrets, two
NetworkPolicies, `na-fali-tls` on the Ingress). On 2026-09-25 that path was abandoned: user
accounts were built into the existing standard-library server on SQLite instead. The empty
`accounts/`, `templates/registration/`, `config/`, `src/`, `offline/` and `scripts/` directories
are leftovers of the unbuilt Django variant; `tests/test_deployment.py` stays unbuilt and out of
CI. Do not start a second deployment path.
