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

Poza gitem zostaja **dwa** sekrety w namespace `na-fali` (repo jest publiczne, wiec
nie moga trafic do historii):

- `ghcr-pull` — poswiadczenie do prywatnego pakietu w ghcr (krok 2 nizej), wymagany;
- `na-fali-registration` (klucz `code`) — kod, ktory trzeba podac przy zakladaniu konta.
  Deployment odwoluje sie do niego z `optional: true`: bez sekretu rejestracja jest otwarta.

  ```sh
  kubectl -n na-fali create secret generic na-fali-registration --from-literal=code='...'
  kubectl -n na-fali rollout restart deploy/na-fali     # env czytany przy starcie
  ```

ArgoCD ich nie zna, wiec ich nie usunie, ale odtworzenie namespace'u od zera wymaga
odtworzenia ich recznie.

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
opcjonalny `REGISTRATION_CODE`. Konta i sesje zyja w tej samej bazie SQLite co historia,
wiec migracja schematu (v2 -> v3) wykonuje sie przy pierwszym starcie nowego obrazu.

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
