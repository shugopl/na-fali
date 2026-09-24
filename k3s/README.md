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

## Uruchomienie wlasciwej aplikacji

1. Zbuduj obraz i wypchnij go (np. `ghcr.io/shugopl/na-fali:<tag>`).
2. Wpisz tag do `20-deployment.yaml` i `repoURL` do `argocd/application.yaml`.
3. Wypchnij to repo na GitHuba, potem:

   ```sh
   kubectl apply -f k3s/argocd/application.yaml
   kubectl -n na-fali delete deploy,cm na-fali-placeholder   # usun placeholder
   ```

Kontrakt kontenera (wg `tests/test_server.py`): nasluch na `HOST`/`PORT`,
baza w `DB_PATH`, health pod `GET /api/health`.

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
