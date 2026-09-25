# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Na fali" — a Polish ham-radio course app. A single self-contained page
(`web/index.html`, ~1.7 MB) plus a Python standard-library server that adds
persistence. No third-party runtime dependencies, no build step.

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

# run locally
HOST=127.0.0.1 PORT=8080 DB_PATH=./data/course.sqlite3 python3 -u server.py
```

Python 3.12. There is no linter, formatter, or dependency manifest in the repo.

## Architecture

### Content has one source of truth: the HTML page

`BANK` is **parsed out of `web/index.html`** at import time — `server.py` locates the
`const DATA=` literal and brace-matches it (`_extract_data`). The page ships the whole
question bank inline so it renders standalone; the server re-reads that same literal so
backend and client can never disagree about content.

Consequences worth knowing before editing:
- Changing questions, Q-codes, bands or `contentRevision` means editing the JSON literal
  inside `web/index.html`, not a separate data file.
- `tests/test_content.py` asserts exact counts (297 questions, 75 qCatalog, 11 bandDetails,
  26 alphabet, 16 district regions across 9 prefixes) and that every question has exactly
  3 distinct options. Content edits break these deliberately.
- `Store.catalog()` materializes a flat index of that content into SQLite, rebuilt only when
  `BANK['contentRevision']` changes.

### Persistence: merge, never overwrite

`Store.save()` merges attempts by `id` rather than replacing them. A delayed client retry
carrying `chosen: null` must not erase an answer stored in the meantime, and `help` is
sticky once set. Trying to change an already-stored answer to a different value raises
`Conflict`. All attempts are validated *before* the transaction opens, so one bad record in
an import rejects the whole batch without persisting any of it.

Two optimistic-concurrency counters guard stale browser tabs:
- `generation` — bumped by `Store.clear()`; a `save()` with an old generation is a `Conflict`.
- `revision` — bumped by each `Store.exams()` write.

### Schema migrations

`Store` carries `SCHEMA_VERSION` and migrates on open, keyed off `PRAGMA user_version`.
v1 (snake_case columns, `meta(id, generation)`) upgrades to v2 (camelCase, key-value `meta`)
without losing history. A v1 database with `user_version=0` but an existing `attempts` table
is treated as v1. `tests/test_content.py` exercises this against a hand-built v1 database.

### Circular import

`exam_validation.py` needs `BANK`, and `server.Store.exams()` needs `validate_workspace`.
The import in `Store.exams()` is **deliberately lazy** (function-local) to break the cycle.
Keep it that way.

### Request protection

- `Host` and `Origin` must resolve to an allowed name — loopback always, plus whatever
  `ALLOWED_HOSTS` (comma-separated) lists. This blocks DNS rebinding. **Deploying under a new
  domain requires updating `ALLOWED_HOSTS` or every request 403s.**
- Mutating requests must carry `X-Na-Fali: 1`; a cross-site form cannot send a custom header.
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

- CI (`.github/workflows/deploy.yml`) tests, builds the image to ghcr.io, then writes the new
  tag into `k3s/20-deployment.yaml` and commits it back — ArgoCD syncs from that commit.
  Do not hand-edit the image tag expecting it to survive.
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

## Unresolved: two competing architectures

`tests/test_deployment.py` describes a **different application** from the one that is
implemented and deployed:

| | `test_server.py` (implemented) | `test_deployment.py` (not implemented) |
|---|---|---|
| Storage | SQLite on a PVC | Postgres StatefulSet |
| Framework | Python standard library | Django (`django-key`, bootstrap Job) |
| Secrets | none | `na-fali-db`, `na-fali-bootstrap`, `na-fali-db-admin` |
| Network | — | two NetworkPolicies |
| TLS | Cloudflare | `na-fali-tls` on the Ingress |

The empty `accounts/`, `templates/registration/`, `config/`, `src/`, `offline/` and `scripts/`
directories belong to that unbuilt Django variant. Do not build a second deployment path
alongside the working one without settling this first.
