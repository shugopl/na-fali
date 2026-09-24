# Srodowisko k3s — na-fali

Publiczny adres: **https://na-fali.shugo.com.pl** (przez Cloudflare -> Traefik na klastrze `julia120`).

## Pliki

| Plik | Co robi |
|---|---|
| `00-namespace.yaml` | namespace `na-fali` |
| `10-pvc.yaml` | PVC `na-fali-data` (1Gi, local-path) na baze SQLite |
| `20-deployment.yaml` | Deployment aplikacji — **wymaga podmiany `image:`** |
| `30-service.yaml` | Service `na-fali` :80 -> kontener :8080 |
| `40-ingress.yaml` | Ingress Traefik dla hosta `na-fali.shugo.com.pl` |
| `argocd/application.yaml` | ArgoCD Application — **wymaga podmiany `repoURL:`** |

`argocd/` jest podkatalogiem, wiec ArgoCD (bez `recurse: true`) go nie zaciaga — Application nie zarzadza sam soba.

## Co juz jest na klastrze

Namespace, PVC, Service i Ingress sa zastosowane. Ruch po hoscie jest zweryfikowany
end-to-end (takze przez publiczne IPv6). Tymczasowo Service obsluguje
`deploy/na-fali-placeholder` (nginx), zeby sciezka byla sprawdzalna przed zbudowaniem obrazu.

## Uruchomienie — od zera do wdrozenia

Repo jest **prywatne**, a ten host nie ma klucza SSH, wiec kroki 1-2 musisz wykonac sam.

1. **Wypchnij kod** (commit lokalny juz istnieje, remote ustawiony):

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''   # jesli nie masz klucza
   # klucz .pub dodaj w GitHub -> Settings -> SSH keys
   git push -u origin main
   ```

2. **Daj ArgoCD dostep do repo** — patrz `argocd/repo-secret.example.yaml`
   (deploy key read-only wystarczy).

3. **Wlacz Application**:

   ```sh
   kubectl apply -f k3s/argocd/application.yaml
   ```

4. **Poczekaj na obraz.** Push na `main` uruchamia workflow, ktory buduje obraz
   do `ghcr.io/shugopl/na-fali:<sha>`, wpisuje tag do `k3s/20-deployment.yaml`
   i commituje go z powrotem — ArgoCD podchwytuje zmiane i synchronizuje.
   Pakiet w ghcr musi byc widoczny dla klastra: albo ustaw go na public
   (Package settings -> Change visibility), albo dodaj `imagePullSecrets`.

5. **Usun placeholder**, gdy realny pod wstanie:

   ```sh
   kubectl -n na-fali delete deploy,cm na-fali-placeholder
   ```

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
