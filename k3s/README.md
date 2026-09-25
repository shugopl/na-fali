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

Poza gitem zostaje **jedna** rzecz: sekret `ghcr-pull` w namespace `na-fali`
(poswiadczenie do prywatnego pakietu w ghcr). ArgoCD go nie zna, wiec go nie usunie,
ale odtworzenie namespace'u od zera wymaga odtworzenia go recznie — patrz krok 2 nizej.

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
`GET /api/health`, dozwolone domeny w `ALLOWED_HOSTS`.

## Uwagi

- **DNS**: rekord `na-fali.shugo.com.pl` trzeba dodac w Cloudflare — AAAA na
  `2a01:4f9:2b:289c::120` z wlaczonym proxy (tak jak `simple-java-api`).
  IPv4 klastra (`192.168.1.120`) jest prywatne i nie nadaje sie na origin.
- **TLS** konczy sie na Cloudflare, origin serwuje HTTP. Przy trybie
  Full (strict) dolozyc cert-managera i odkomentowac blok `tls:` w Ingressie.
- **PVC** ma `WaitForFirstConsumer`, wiec zostaje `Pending` dopoki nie wystartuje
  pod, ktory go montuje — to nie jest blad.
- SQLite na wolumenie RWO: `replicas: 1` i `strategy: Recreate`. Skalowanie w poziomie
  wymagaloby zmiany bazy.
