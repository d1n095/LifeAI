# VPS-bootstrap — vad varje skript gör och varför

Detaljerad förklaring av `scripts/vps/*.sh`. Se `scripts/vps/README.md` för körordningen i
kort form och `docs/STRATO_VPS_DEPLOY.md` för hur det här steget passar in i hela
installationssekvensen (Steg 1).

## Designprinciper som gäller alla skripten

- **Idempotens.** Varje skript kan köras om utan att förstöra något — de kontrollerar
  faktiskt tillstånd (finns användaren redan? är katalogen redan skapad? har konfigen redan
  rätt innehåll?) innan de ändrar något, istället för att blint köra samma kommandon igen.
- **`--dry-run` är en riktig garanti, inte bara dokumentation.** Alla muterande kommandon går
  genom `lib.sh`s `run()`-funktion, som i `--dry-run`-läge bara skriver ut kommandot istället
  för att köra det. Det finns ingen kodväg där en muterande åtgärd kan smyga sig förbi den
  kontrollen.
- **`set -euo pipefail`** i varje skript — ett misslyckat kommando stoppar skriptet direkt
  istället för att fortsätta i ett okänt tillstånd.
- **Root kontra icke-root är explicit**, aldrig underförstått — varje skript som behöver root
  anropar `require_root` (eller `require_not_root`) högst upp och ger ett tydligt felmeddelande
  om det körs fel.
- **Aldrig tysta överskrivningar.** `ensure_line_in_file` i `lib.sh` lägger bara till en rad om
  den inte redan finns; kataloger vars ägare/rättigheter redan är satta rörs inte.

## Skript för skript

### `00_preflight.sh` — skrivskyddad

Kontrollerar Ubuntu-version (22.04/24.04), diskutrymme, minne, att portarna 80/443 inte redan
är upptagna av något annat, och (om `--domain` ges) att domänen faktiskt resolvar. Ändrar
ALDRIG något — säker att köra hur ofta som helst, även bara för att dubbelkolla servern.

### `10_install_docker.sh`

Docker Engine + Compose-plugin från Dockers EGET officiella apt-repo (inte Ubuntus ofta äldre
`docker.io`-paket) — GPG-nyckeln verifieras innan repot läggs till. Idempotent: hoppar över
installationen helt om `docker --version` redan visar en `docker-ce`-installation.

### `20_create_deploy_user.sh --user <namn>`

Skapar en dedikerad, icke-root, sudo-kapabel driftanvändare (standard: `lifeai`) och lägger
till den i `docker`-gruppen. **Kopierar aldrig en SSH-nyckel åt dig och stänger aldrig av
lösenordsinloggning** — det är ett separat, medvetet opt-in-steg
(`41_harden_ssh.sh`) som kräver att du själv redan kört `ssh-copy-id` och verifierat att
nyckelbaserad inloggning fungerar, så att den här bootstrap-processen aldrig kan låsa dig ute
från din egen server.

### `30_setup_directories.sh --user <namn>`

Skapar `/opt/lifeai` (ägs av driftanvändaren — själva deploy-checkouten) och `/etc/lifeai`
(root:root, `chmod 700` — **aldrig läsbar för driftanvändaren**, bara för root/Docker-daemonen,
se `docs/VPS_SECRETS_INVENTORY.md`), plus `/opt/lifeai/deployments` (deploy-historik) och
`/var/backups/lifeai` (backuper).

### `40_configure_firewall.sh [--ssh-port N]`

`ufw`: standard neka inkommande, tillåt utgående, tillåt SSH (verifierar FÖRST att `sshd`
faktiskt lyssnar på den port som ska tillåtas — vägrar annars, för att aldrig riskera att låsa
in dig) plus 80/443 för Caddy. Frågar om bekräftelse innan `ufw enable` aktiveras (kan hoppas
över med `--yes`).

### `41_harden_ssh.sh --user <namn>` — OPT-IN, inte en del av standardflödet

Stänger av lösenordsinloggning och root-inloggning över SSH. Detta är det enda steget i hela
bootstrap-sekvensen som på riktigt kan låsa dig ute — därför:

- **Vägrar köra om den inte hittar en riktig publik nyckel** i målanvändarens
  `authorized_keys`.
- **Kräver en uttrycklig bekräftelse** att du redan testat lösenordsfri inloggning i en
  SEPARAT session innan den ändrar något.
- **Rör aldrig SSH-porten.**
- **Säkerhetskopierar `sshd_config`** till en tidsstämplad fil innan den redigeras.
- **Validerar med `sshd -t` innan den laddar om** — om valideringen misslyckas återställs
  säkerhetskopian automatiskt och ingenting laddas om.
- **Startar aldrig om hela `sshd`**, bara `reload` (befintliga anslutningar bryts inte).
- **Startar aldrig om servern.**

### `50_enable_auto_updates.sh`

`unattended-upgrades` för säkerhetsuppdateringar. Aktiverar INTE automatiska omstarter — en
överraskande omstart av en server som kör produktionscontainrar är ett separat, medvetet beslut
(se `docs/VPS_OPERATIONS_RUNBOOK.md`s incidentpunkt för en väntande omstart efter en
kärnuppdatering), inte något det här skriptet bestämmer åt dig.

### `90_verify_installation.sh --user <namn>` — skrivskyddad

Sammanfattande, skrivskyddad kontroll av allt ovanstående: Docker/Compose installerat och
aktivt, driftanvändaren finns och är i rätt grupper, kataloger finns med rätt ägare/rättigheter,
`ufw` är aktiv med rätt regler, `unattended-upgrades` är konfigurerat, tillräckligt diskutrymme
kvar, och att inget redan lyssnar på 80/443 (Caddy är inte igång än — det är förväntat före
Steg 2-5 i `docs/STRATO_VPS_DEPLOY.md`).

## Vad som INTE verifieras här (medvetet)

Dessa skript sätter upp SERVERN, inte APPLIKATIONEN — GHCR-inloggning, hemlighetsfilens
faktiska innehåll, och den första `docker compose up` hör till Steg 2-5 i
`docs/STRATO_VPS_DEPLOY.md`, inte hit.

## CI-verifiering

`.github/workflows/ci.yml`s `vps-scripts-check`-jobb kör `shellcheck` mot varje skript här och
exekverar varje skripts `--dry-run`-väg (som är fri från sidoeffekter per konstruktion, se
ovan) för att fånga syntax-/logikfel innan de någonsin körs på riktigt. De riktiga (icke-
dry-run) vägarna testas INTE i CI, eftersom det inte finns någon riktig server att köra dem
mot ännu — se `docs/VPS_HANDOVER_CHECKPOINT.json` för exakt vad som återstår att verifiera på
den riktiga servern.
