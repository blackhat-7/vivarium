# vivarium

A Docker-based sandbox for running coding agents on a Linux host with a bounded
blast radius. Agents can work freely inside `~/vivarium-home/work`; the host is
protected by Docker isolation, a non-root container user, backups, and a
read-only GitHub PAT.

## Quick start

```bash
git clone https://github.com/blackhat-7/vivarium.git ~/vivarium
cd ~/vivarium
./scripts/up.sh
./scripts/shell.sh
```

Inside the container:

```bash
opencode auth login        # default agent
cd ~/work
git clone https://github.com/YOU/repo.git
```

AI harness configs are baked in yolo mode. MCPs are off by default; enable only
what you need in `.env` before `./scripts/up.sh`:

```env
AI_HARNESSES_MCP=github,bestiary   # or: none / all
GITHUB_MCP_TOKEN=github_pat_...    # optional, read-only token
```

Use a **fine-grained, repo-scoped, read-only GitHub PAT** for clones. Pushes
with that token should fail.

## Safety invariants

Do not weaken these:

- container runs as non-root: final Dockerfile stage is `USER vivarium`
- no `/var/run/docker.sock` mount
- no `--privileged`
- `cap_drop: ALL` stays enabled
- GitHub PAT is fine-grained, selected-repo, read-only only
- optional installs fail fast with `[FATAL]`
- scripts preserve user state and only delete data behind explicit flags

## Current commands

```bash
./scripts/up.sh [profile|env-file]              # create env, build image, start container
./scripts/shell.sh [profile|env-file]           # enter container
./scripts/profile-create.sh <profile>           # create profiles/<profile>.env
./scripts/update.sh [profile|env-file]          # fast-forward repo and rebuild
./scripts/backup.sh [profile|env-file]          # snapshot profile work dir
./scripts/audit.sh [profile|env-file]           # host-side drift/safety check
./scripts/cron-install.sh [profile|env-file]    # install backup/audit cron entries
./scripts/cron-uninstall.sh [profile|env-file]  # remove cron entries
./scripts/remove.sh [profile|env-file]          # remove container; optional data deletion
```

No argument uses `.env`. A profile name uses `profiles/<name>.env`; a path can
point at an env file owned by another repo, e.g. `../server-stack/work.env`.
For parallel Paseo profiles, set different `PASEO_PORT` values.

## Files

- `Dockerfile` — Ubuntu image, non-root user, optional agent installs
- `compose.yaml` — runtime hardening, mount, limits, restart policy
- `entrypoint.sh` — home bootstrap and startup safety config
- `scripts/build-ai-harnesses.sh` — build-time yolo ai-harnesses profile
- `.env.example` — default config template
- `profiles/*.env` — ignored per-profile config files
- `DOCS.md` — concise implementation notes

## Planned additions

- [ ] `vivarium doctor` — prove safety invariants with one command
- [ ] `vivarium panic` — stop bad sessions, clear volatile creds, print recovery steps
- [ ] `vivarium snapshots` — list available backups
- [ ] `vivarium restore` — safely restore a repo or work dir from backup
- [ ] audit timeline — summarize changed repos, secret-like files, MCP drift, and git config drift
- [ ] red-team demo — show common escape/persistence attempts failing
- [ ] red-line tests — automated checks for Dockerfile/compose/security invariants
- [ ] optional Go host CLI if the command surface grows; keep Docker/compose/entrypoint simple
