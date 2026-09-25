# Srodowisko k3s — na-fali

Publiczny adres: **https://na-fali.shugo.com.pl** (przez Cloudflare -> Traefik na klastrze `julia120`).

## Pliki

| Plik | Co robi |
|---|---|
| `00-namespace.yaml` | namespace `na-fali` |
| `10-pvc.yaml` | PVC `na-fali-data` (1Gi, local-path) na baze SQLite |
| `20-deployment.yaml` | Deployment aplikacji — tag `image:` wpisuje CI, nie edytuj recznie |
| `30-service.yaml` | Service `na-fali` :80 -> kontener :8080 |
| `40-ingress.yaml` | Ingress Traefik dla hosta `na-fali.shugo.com.pl` |
| `argocd/application.yaml` | ArgoCD Application — gotowa, klonuje repo po HTTPS |

`argocd/` jest podkatalogiem, wiec ArgoCD (bez `recurse: true`) go nie zaciaga — Application nie zarzadza sam soba.

## Kto czym zarzadza

ArgoCD Application `na-fali` synchronizuje katalog `k3s/` z brancha `main`
(`prune: true`, `selfHeal: true`), wiec namespace, PVC, Deployment, Service i Ingress
sa w calosci opisane w repo — recznych `kubectl apply` na nie nie trzeba, a `selfHeal`
cofnie zmiany zrobione w klastrze obok gita.

Poza gitem zostaja **cztery** sekrety w namespace `na-fali` (repo jest publiczne, wiec
nie moga trafic do historii). Wszystkie poza `ghcr-pull` sa `optional: true` — pod wstaje
bez nich, tylko z ograniczona funkcja. Env jest czytany przy starcie, wiec po kazdej zmianie
sekretu: `kubectl -n na-fali rollout restart deploy/na-fali`.

- `ghcr-pull` — poswiadczenie do prywatnego pakietu w ghcr (krok 2 nizej), wymagany.
- `na-fali-admin` — pierwsze konto administratora. Tworzone przy starcie idempotentnie:
  istniejace haslo nie jest nadpisywane (chyba ze dodasz `ADMIN_RESET_PASSWORD=1`).

  ```sh
  kubectl -n na-fali create secret generic na-fali-admin \
    --from-literal=ADMIN_EMAIL=tadzioikona@gmail.com \
    --from-literal=ADMIN_PASSWORD='...'
  ```

- `na-fali-mail` — SMTP do kodow weryfikacyjnych (rejestracja, reset hasla). Dla Gmaila:
  wlacz weryfikacje dwuetapowa, wygeneruj **haslo aplikacji** (16 znakow), `SMTP_FROM` musi
  byc tym samym kontem Gmail (inaczej Gmail podmienia nadawce). Limit ok. 500 wiadomosci/dzien.
  Bez tego sekretu kody trafiaja wylacznie do logu poda (`kubectl -n na-fali logs deploy/na-fali`).

  ```sh
  kubectl -n na-fali create secret generic na-fali-mail \
    --from-literal=SMTP_HOST=smtp.gmail.com --from-literal=SMTP_PORT=587 \
    --from-literal=SMTP_USER=tadzioikona@gmail.com \
    --from-literal=SMTP_PASSWORD='haslo-aplikacji' \
    --from-literal=SMTP_FROM=tadzioikona@gmail.com
  ```

- `na-fali-registration` (klucz `code`) — tylko ziarno kodu zaproszenia przy pierwszym
  starcie; potem kod i otwarcie/zamkniecie rejestracji ustawia sie w panelu admina.

ArgoCD ich nie zna, wiec ich nie usunie, ale odtworzenie namespace'u od zera wymaga
odtworzenia ich recznie.

## Panel administracyjny

Po zalogowaniu kontem z uprawnieniami admina na stronie pojawia sie zakladka
**Administracja**: przeglad (konta, podejscia, egzaminy, skutecznosc per przedmiot), lista
uzytkownikow z akcjami (nadaj/odbierz admina, potwierdz recznie, reset hasla e-mailem,
wyloguj wszedzie, wyczysc historie, eksport JSON, usun), ustawienia rejestracji (otwarta /
zamknieta, kod zaproszenia) z testem poczty oraz pobranie kopii zapasowej bazy.

Pierwsza rzecz po wdrozeniu: **Ustawienia -> „Wyslij e-mail testowy”** — wysylka jest
synchroniczna i pokazuje blad SMTP wprost (zle haslo aplikacji, brak sieci).

Przywracanie kopii (plik `.sqlite3` z panelu, tryb rollback — bez plikow `-wal`/`-shm`):

```sh
kubectl -n na-fali scale deploy/na-fali --replicas=0
POD=$(kubectl -n na-fali run restore --image=python:3.12-alpine --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"restore","image":"python:3.12-alpine","command":["sleep","600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"na-fali-data"}}]}}' -o name)
kubectl -n na-fali wait --for=condition=Ready $POD
kubectl -n na-fali exec restore -- sh -c 'rm -f /data/course.sqlite3 /data/course.sqlite3-wal /data/course.sqlite3-shm'
kubectl -n na-fali cp ./na-fali-XXXX.sqlite3 restore:/data/course.sqlite3
kubectl -n na-fali exec restore -- chown 10001:10001 /data/course.sqlite3
kubectl -n na-fali delete pod restore
kubectl -n na-fali scale deploy/na-fali --replicas=1
```

## Uruchomienie — od zera do wdrozenia

Repo jest **publiczne**, wiec ArgoCD klonuje je po HTTPS anonimowo — zaden sekret
z poswiadczeniami do gita nie jest potrzebny. Jedyna rzecz, ktora musisz wprowadzic
sam, to dostep do **prywatnego** pakietu w ghcr (krok 2).

1. **Wypchnij kod** na `main`. Push uruchamia workflow, ktory buduje obraz
   do `ghcr.io/shugopl/na-fali:<sha>`, wpisuje tag do `20-deployment.yaml`
   i commituje go z powrotem jako `deploy: <sha>` — wiec po pushu zrob `git pull`,
   zanim zaczniesz kolejna zmiane.

2. **Utworz sekret do ghcr.** Bez niego pod wpada w `ImagePullBackOff`.
   Potrzebny PAT (classic) ze scope **`read:packages`**. Zapisz go do pliku poza
   repo — repo jest publiczne, token nie moze wyladowac w historii:

   ```sh
   kubectl -n na-fali create secret docker-registry ghcr-pull \
     --docker-server=ghcr.io --docker-username=shugopl \
     --docker-password="$(cat ~/.ghcr-pat)"
   rm ~/.ghcr-pat
   ```

   Alternatywa bez sekretu: ustaw pakiet na public (Package settings ->
   Change visibility) i usun blok `imagePullSecrets` z `20-deployment.yaml`.

3. **Wlacz Application**:

   ```sh
   kubectl apply -f k3s/argocd/application.yaml
   ```

4. **Usun placeholder**, jesli jeszcze stoi:

   ```sh
   kubectl -n na-fali delete deploy,cm na-fali-placeholder
   ```

   ArgoCD **nie zrobi tego sam**: `prune` dotyczy wylacznie zasobow, ktore ArgoCD
   wczesniej oznaczyl swoimi metadanymi, a placeholder byl zaaplikowany recznie.
   Dopoki stoi, nosi etykiete `app.kubernetes.io/name: na-fali` — czyli selektor
   Service'u — i przejmuje czesc ruchu.

Kontrakt kontenera: nasluch na `HOST`/`PORT`, baza w `DB_PATH`, health pod
`GET /api/health`, dozwolone domeny w `ALLOWED_HOSTS`, `SECURE_COOKIES=1` (flaga Secure na
cookie sesji — origin mowi czystym HTTP za Cloudflare, wiec sam tego nie wywnioskuje),
opcjonalne `REGISTRATION_CODE`, `SMTP_*`, `ADMIN_EMAIL`/`ADMIN_PASSWORD`. Konta, sesje, kody
i ustawienia zyja w tej samej bazie SQLite co historia, wiec migracja schematu wykonuje sie
przy pierwszym starcie nowego obrazu; poczatek logu poda mowi, co migracja zrobila
(np. `migracja v4: usunieto 1 kont bez adresu e-mail`) i ktory admin zostal zapewniony.

## Ochrona originu

Origin odpowiada po IPv6, wiec bez filtrowania jest osiagalny z internetu
bezposrednio, z pominieciem Cloudflare — a wtedy znika rate limiting i WAF,
a pod spodem stoi serwer z biblioteki standardowej Pythona. Ukrywanie adresu
nie jest kontrola: pojedynczy `/128` w zakresie hostingodawcy jest skanowalny,
a originy za Cloudflare wyciekaja tez przez logi Certificate Transparency
i historyczne rekordy DNS.

Chroni go tabela nftables `cf-origin` na wezle. Domyslnie odrzuca ruch wchodzacy
po IPv6 na `eth0`, przepuszczajac tylko:

- odpowiedzi na polaczenia zainicjowane przez wezel (`ct state established,related`),
- **caly ICMPv6** — bez NDP i Path MTU Discovery IPv6 przestaje dzialac,
- `tcp/22` i `tcp/6443` — administracja,
- `tcp/80` i `tcp/443` **wylacznie z opublikowanych zakresow IPv6 Cloudflare**.

Zamyka to przy okazji dwie rzeczy, ktore wczesniej byly w internecie: kubelet
(`10250/tcp`) oraz VXLAN flannela (`8472/udp`, protokol bez uwierzytelniania).

Zrodlem prawdy jest `host/cf-origin-refresh`: generuje
`/etc/nftables.d/cloudflare-origin.nft` i laduje go, a `cf-origin-refresh.timer`
odswieza liste raz w tygodniu — zwietrzala lista po cichu odcielaby czesc
odwiedzajacych. Nieudane pobranie zostawia dzialajace reguly bez zmian, a kazdy
wygenerowany plik przechodzi `nft -c` przed zaladowaniem.

Instalacja na nowym wezle:

```sh
install -m 750 k3s/host/cf-origin-refresh /usr/local/sbin/
install -m 644 k3s/host/cf-origin-refresh.service k3s/host/cf-origin-refresh.timer /etc/systemd/system/
printf '\ninclude "/etc/nftables.d/*.nft"\n' >> /etc/nftables.conf
systemctl daemon-reload && systemctl enable --now cf-origin-refresh.timer
/usr/local/sbin/cf-origin-refresh
```

Diagnostyka i wycofanie:

```sh
nft list counters table inet cf-origin   # co i ile odpada
nft delete table inet cf-origin          # wycofanie w calosci
```

Liczniki, a nie logi, bo `julia120` jest kontenerem LXC — netfilterowy `log`
trafia do ringu jadra hosta i z wnetrza kontenera jest niewidoczny.

**Nie restartuj `nftables.service`** przy dzialajacym k3s: `/etc/nftables.conf`
zaczyna sie od `flush ruleset`, co zmiotloby takze lancuchy kube-proxy
i kube-routera i na chwile zerwalo siec klastra. Reguly przeladowuj przez
`/usr/local/sbin/cf-origin-refresh`.

## Uwagi

- **DNS**: rekord `na-fali.shugo.com.pl` trzeba dodac w Cloudflare — AAAA na
  publiczne IPv6 wezla, z wlaczonym proxy (tak jak `simple-java-api`).
  Adresu origin nie zapisujemy w repo; odczytaj go z `ip -6 addr show dev eth0`.
  IPv4 klastra (`192.168.1.120`) jest prywatne i nie nadaje sie na origin.
- **TLS** konczy sie na Cloudflare, origin serwuje HTTP. Przy trybie
  Full (strict) dolozyc cert-managera i odkomentowac blok `tls:` w Ingressie.
- **PVC** ma `WaitForFirstConsumer`, wiec zostaje `Pending` dopoki nie wystartuje
  pod, ktory go montuje — to nie jest blad.
- SQLite na wolumenie RWO: `replicas: 1` i `strategy: Recreate`. Skalowanie w poziomie
  wymagaloby zmiany bazy.
