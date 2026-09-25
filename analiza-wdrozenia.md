# Na fali — analiza wdrozenia na k3s

Stan na 2026-09-24. Klaster `julia120` (k3s v1.34.6), ArgoCD v3.3.6, Traefik.
Publiczny adres docelowy: **https://na-fali.shugo.com.pl**

---

## 1. Naprawy w klastrze

ArgoCD bylo niesprawne od ~173 dni — zaden Application nie mogl sie zsynchronizowac.

| Komponent | Objaw | Przyczyna | Naprawa |
|---|---|---|---|
| `argocd-repo-server` | status `Unknown`, init-container w petli | `copyutil` przerywal na `/bin/ln: Already exists` — nieświezy emptyDir po nieczystym restarcie wezla | odtworzenie poda (swiezy emptyDir) |
| `argocd-applicationset-controller` | CrashLoopBackOff, 35185 restartow | brak CRD `applicationsets.argoproj.io` | instalacja CRD w wersji zgodnej z ArgoCD (v3.3.6) |

Efekt uboczny: `simple-java-api` wrocil z `Unknown` na **Synced / Healthy**.
ArgoCD ma teraz 7/7 podow `Running`.

## 2. Srodowisko na-fali

Zastosowane na klastrze: namespace `na-fali`, PVC `na-fali-data` (1Gi, local-path),
Service `na-fali` (:80 -> :8080), Ingress Traefik dla hosta `na-fali.shugo.com.pl`.

Sciezka ruchu zweryfikowana end-to-end:

```
https://na-fali.shugo.com.pl/         -> HTTP 200   (Cloudflare -> Traefik -> Service)
Host: na-fali.shugo.com.pl @ IPv6     -> HTTP 200
/api/health                           -> ok
Host: nieistnieje.example             -> HTTP 404   (routing po hoscie dziala)
```

Tymczasowo Service obsluguje `deploy/na-fali-placeholder` (nginx) — do usuniecia,
gdy wstanie realny pod.

**DNS**: rekord dodany, `na-fali.shugo.com.pl` rozwiazuje sie na Cloudflare.
TLS konczy sie na Cloudflare, origin serwuje HTTP (tak jak `simple-java-api`).
IPv4 klastra (`192.168.1.120`) jest prywatne — originem jest publiczne IPv6
wezla (adresu nie zapisujemy w repo, patrz `k3s/README.md` / "Ochrona originu").

## 3. Backend

`server.py` i `exam_validation.py` odtworzone z kontraktu w `tests/`.

- **`BANK` parsowany z `web/index.html`** (literal `const DATA=`). Jedno zrodlo
  prawdy dla tresci — backend i klient nie moga sie rozjechac. Zgodnosc:
  297 pytan, qCatalog 75, bandDetails 11, alphabet 26, districts 16 regionow / 9 prefiksow.
- **Zapisy idempotentne przez scalanie.** Opozniony retry z `chosen: null` nie kasuje
  odpowiedzi zapisanej w miedzyczasie; proba zmiany juz zapisanej odpowiedzi to `409`.
- **Atomowosc**: walidacja w calosci przed transakcja, wiec jeden bledny rekord
  w imporcie nie przepuszcza pozostalych.
- **Migracja bazy v1 -> v2**: kolumny snake_case -> camelCase,
  `meta(id, generation)` -> pary klucz-wartosc, `user_version` 1 -> 2, historia zachowana.
- **`catalog()`**: zmaterializowany indeks tresci (pytania / kody Q / pasma),
  przebudowywany tylko przy zmianie `contentRevision`.
- **Ochrona**: kontrola `Host` i `Origin` (DNS rebinding), naglowek `X-Na-Fali`
  jako zapora CSRF na zadaniach mutujacych, baza nigdy nie jest serwowana.
  `/api/health` celowo poza kontrola hosta — sonda kubeletu uzywa IP poda.
- **Konta (2026-09-25)**: samodzielna rejestracja, hasla scrypt, sesja w cookie
  `HttpOnly; SameSite=Lax` (+`Secure` z `SECURE_COOKIES=1`), historia, `generation`
  i egzaminy per konto (schemat v3, migracja z v2 przenosi ewentualna wspolna historie
  na konto zastepcze `#legacy`). Tresc kursu publiczna, dane za `401`. Opcjonalny
  `REGISTRATION_CODE` z sekretu `na-fali-registration`. Limit 10 prob logowania /
  rejestracji na 5 minut na adres (`CF-Connecting-IP`).
- **Naprawiony baner „Nie mozna otworzyc historii”** po wdrozeniu: serwer zwracal
  `bankVersion: contentRevision` bez `schemaVersion`, pusty workspace egzaminow jako
  `null` i po skasowaniu historii sam licznik zamiast pelnego stanu — klient odrzucal
  kazda z tych odpowiedzi. Teraz `state()` zwraca `schemaVersion: 2` i `BANK_VERSION`
  parsowany ze strony, pusty workspace ma ksztalt `{active: null, history: []}`,
  a `clear()` zwraca pelny stan.

Smoke test lokalny: strona 1 690 125 B z `const DATA=`, zapis i odczyt podejscia,
`/data/course.sqlite3` -> 404, zly Host -> 403, brak `X-Na-Fali` -> 403,
`Host: na-fali.shugo.com.pl` -> 200.

## 4. Pozostale artefakty

`Dockerfile` (python:3.12-alpine, non-root 10001, bez zaleznosci),
`.dockerignore`, `.gitignore`, workflow CI (test -> build do ghcr.io ->
wpisanie taga do manifestu -> ArgoCD podchwytuje), `git init` + remote + 2 commity.
`ALLOWED_HOSTS=na-fali.shugo.com.pl` w Deploymencie — bez tego serwer odrzucalby
ruch z domeny.

Wszystkie manifesty przechodza `kubectl apply --dry-run=server`.

---

## 5. Blokery — stan aktualny

**Rozwiazane: kod jest wypchniety.** Host ma juz klucz SSH (`ssh -T git@github.com`
zwraca `Hi shugopl!`), branch `master` przemianowany na `main`, `origin/main` = `c2de361`.
CI przeszlo i bot wpisal `image: ghcr.io/shugopl/na-fali:7abaacf23...`
do `k3s/20-deployment.yaml`. Dockera ani podmana na tym hoscie nadal nie ma,
dlatego build zostaje w CI.

**Rozwiazane: poswiadczenie repo dla ArgoCD nie jest potrzebne.** Repo jest teraz
**publiczne** (`api.github.com/repos/shugopl/na-fali` -> `visibility=public`),
a `application.yaml` klonuje je po HTTPS anonimowo. `argocd/repo-secret.example.yaml`
zostaje wylacznie jako szablon na wypadek powrotu do prywatnego.

**Otwarte: pakiet w ghcr jest prywatny.** `ghcr.io/token?scope=repository:shugopl/na-fali:pull`
zwraca `HTTP 401` — widocznosc pakietu to ustawienie niezalezne od widocznosci repo.
Wybrany wariant: pakiet zostaje prywatny, Deployment dostaje `imagePullSecrets: ghcr-pull`,
a sekret tworzy sie recznie (PAT ze scope `read:packages`) i **celowo nie ma go w gicie**.

Kroki do wykonania recznie:

```sh
# 1. sekret do prywatnego pakietu w ghcr (PAT classic, scope read:packages)
kubectl -n na-fali create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=shugopl --docker-password="$(cat ~/.ghcr-pat)"
kubectl apply -f k3s/argocd/application.yaml              # 2. wlacz sync
kubectl -n na-fali delete deploy,cm na-fali-placeholder   # 3. ArgoCD sam tego nie wypruneuje
```

Placeholdera ArgoCD nie usunie samodzielnie: `prune` obejmuje tylko zasoby, ktore ArgoCD
wczesniej oznaczyl, a placeholder byl zaaplikowany recznie i takich metadanych nie ma.

## 6. Testy — 12/14

Dwa bledy na brakujacych plikach, ktorych celowo nie sfabrykowalem:

- **`tests/fixtures/v2-question-hashes.json`** — 116 hashy pytan z v2.
  Wygenerowanie ich z obecnego `DATA` sprawiloby, ze test przestalby czegokolwiek
  bronic, a ktore 116 z 297 pytan pochodzi z v2 — nie da sie odgadnac.
- **`scripts/configure-k3s.py`** — generator manifestow.

CI uruchamia na razie `tests.test_server tests.test_exams` (z komentarzem dlaczego).
Inaczej te dwa bledy blokowalyby build obrazu i nic by sie nie wdrozylo.

## 7. Rozstrzygniete: jedna architektura

`tests/test_deployment.py` opisuje **inna aplikacje** niz ta, ktora wdrazamy:

| | `test_server.py` (wdrozone) | `test_deployment.py` (nieobsluzone) |
|---|---|---|
| Baza | SQLite na PVC | Postgres jako StatefulSet |
| Framework | biblioteka standardowa Pythona | Django (`django-key`, Job bootstrapu) |
| Sekrety | `ghcr-pull`, opcjonalny `na-fali-registration` | `na-fali-db`, `na-fali-bootstrap`, `na-fali-db-admin` |
| Siec | — | dwie NetworkPolicy |
| TLS | Cloudflare | `na-fali-tls` w Ingressie |

**Decyzja (2026-09-25): zostajemy przy bibliotece standardowej i SQLite.** Konta
uzytkownikow, ktorych brakowalo, zostaly dobudowane do istniejacego `server.py`
(patrz §3), wiec wariant Django + Postgres nie ma juz powodu istniec. Puste katalogi
`accounts/`, `templates/registration/`, `config/`, `src/`, `offline/`, `scripts/` to
pozostalosci tego niezbudowanego wariantu; `tests/test_deployment.py` zostaje
niezbudowany i poza CI.
