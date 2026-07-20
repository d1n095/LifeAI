# scripts/vps/

Idempotent bootstrap scripts for a fresh Ubuntu Strato VPS. Run manually, in order, by Dennis
— nothing here is wired into CI to run against a real server. See
`docs/VPS_BOOTSTRAP.md` for the full explanation of each script and
`docs/STRATO_VPS_DEPLOY.md` for how this fits into the complete installation sequence.

## Order

```
sudo ./00_preflight.sh --domain <your-domain>      # read-only, safe to re-run any time
sudo ./10_install_docker.sh
sudo ./20_create_deploy_user.sh --user lifeai
# --- now, from YOUR OWN machine: ssh-copy-id lifeai@<server>, verify passwordless login ---
sudo ./30_setup_directories.sh --user lifeai
sudo ./40_configure_firewall.sh
sudo ./50_enable_auto_updates.sh
# --- optional, only after verifying key-based login works: ---
sudo ./41_harden_ssh.sh --user lifeai
sudo ./90_verify_installation.sh --user lifeai
```

## Every script supports

- `--dry-run` — prints every command it would run instead of running it. Every mutating
  action goes through `lib.sh`'s `run()` helper, so `--dry-run` is a real guarantee, not just
  documentation of intent.
- `--yes` / `-y` — skips interactive confirmations (`40_configure_firewall.sh`'s "enable ufw
  now?", `41_harden_ssh.sh`'s "have you verified key-based login?"). Use for a fully
  unattended run only after you've verified the dry-run output yourself.

## Safety guarantees (see docs/VPS_BOOTSTRAP.md for the reasoning)

- `41_harden_ssh.sh` refuses to disable password authentication unless it finds an actual
  public key already in the target user's `authorized_keys`, and requires you to confirm
  you've already tested passwordless login in a separate session.
- No script here ever changes the SSH port.
- No script here ever reboots the machine.
- `40_configure_firewall.sh` refuses to enable ufw if it detects sshd listening on a port
  different from the one it's about to allow.
- Every directory-creation step checks whether the directory already exists before touching
  ownership/permissions — re-running a script never silently overwrites something you set up
  by hand.

## CI verification

`.github/workflows/ci.yml`'s `vps-scripts-check` job runs `shellcheck` against every script
here and executes each script's `--dry-run` path (which is side-effect-free by construction)
to catch syntax/logic errors — see that job for exactly what's checked. The real,
non-dry-run paths are not (and cannot safely be) exercised in CI, since there is no real
server to run them against yet.
