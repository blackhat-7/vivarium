# vivarium implementation notes

Short reference for what the repo currently does. `README.md` is the user entry
point and safety contract.

## Architecture

- Docker image: `vivarium:latest`
- container name: `vivarium`
- base image: `ubuntu:24.04`
- final user: `vivarium`
- mounted home: `${VIVARIUM_HOME:-$HOME/vivarium-home}:/home/vivarium`
- work dir: `/home/vivarium/work`
- default host work dir: `~/vivarium-home/work`

## Runtime hardening

From `compose.yaml`:

- `cap_drop: [ALL]`
- `cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID]`
- `no-new-privileges:true`
- `mem_limit: 4g`
- `cpus: 2.0`
- `pids_limit: 512`
- no Docker socket mount
- no privileged mode

## Image contents

Base tools include git, curl, tmux, editors, build tools, ripgrep, fd, jq,
sqlite, Python, Node 24, npm, and uv.

Optional build flags:

- `INSTALL_OPENCODE=true` — install opencode
- `INSTALL_CLAUDE=false` — install claude code
- `INSTALL_PASEO=false` — install paseo
- `INSTALL_BESTIARY=false` — install bestiary from `BESTIARY_REF`
- `AI_HARNESSES_REF=main` — ai-harnesses ref to bake into the image
- `AI_HARNESSES_MCP=none` — `none`, `all`, or comma-list such as `github,bestiary`

At least one of `INSTALL_OPENCODE` or `INSTALL_CLAUDE` must be true.

## Script behavior

- `scripts/up.sh` creates/updates `.env`, creates the host work dir, builds,
  and starts the container.
- `scripts/shell.sh` starts the container if needed and opens bash inside it.
- `scripts/update.sh` refuses tracked local changes, fast-forwards from
  `origin/main`, resolves moving bestiary refs, then rebuilds.
- `scripts/backup.sh` rsyncs `VIVARIUM_HOME/work` into rotating backup slots.
- `scripts/audit.sh` checks container state, backup freshness, repo remotes,
  secret-like tracked files, risky git config, MCP drift, and `.ssh` files.
- `scripts/cron-install.sh` installs backup/audit cron entries.
- `scripts/cron-uninstall.sh` removes vivarium cron entries.
- `scripts/remove.sh` removes container/image/cron by default; `--data`,
  `--backups`, and `--everything` opt into destructive removal.

## Entrypoint behavior

On container start, `entrypoint.sh`:

- copies a skeleton `.bashrc` only if missing
- appends `$HOME/.local/bin` to PATH instead of prepending it
- reapplies safe global git config
- removes plaintext `~/.git-credentials`
- creates `~/work`
- reapplies baked yolo ai-harnesses configs every start
- starts paseo only when `PASEO_ENABLE=true` and `paseo` is installed
