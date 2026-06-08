# AGENTS.md

Guidance for agents working on vivarium.

## What this is

Vivarium is a Docker-based sandbox for running coding agents on a Linux host
with bounded blast radius. Worst case should be: restore a backup, rotate a key,
move on.

Read this file and `README.md` before touching Docker, compose, cron, secrets,
or destructive scripts.

## Red lines

Do not weaken these:

1. Final Dockerfile stage stays `USER vivarium`; no final root user.
2. Never add `/var/run/docker.sock` to `compose.yaml`.
3. Never use `--privileged`.
4. Keep `cap_drop: ALL`; do not add stronger caps without updating the threat
   model.
5. GitHub PAT docs must require fine-grained, repo-scoped, read-only tokens:
   `Metadata: read` + `Contents: read`, optionally `Issues: read` and
   `Pull requests: read`. No classic PATs, write/admin/secrets/workflows/account
   scopes.
6. Optional installs must fail fast with `[FATAL]` and name the flag to disable.

If a request requires weakening a red line, stop and say so.

## Editing rules

- Keep diffs small and boring.
- Preserve user state.
- Scripts touching `.env`, crontab, backups, or `~/vivarium-home` must be
  idempotent and only touch vivarium-owned entries.
- Never delete user data without an explicit destructive flag.
- Keep docs short and implementation-accurate.
- If behavior changes, update `README.md` or `DOCS.md` as needed.

## File index

- `README.md` — quick start, safety rules, commands, roadmap checklist
- `DOCS.md` — concise implementation notes
- `Dockerfile` — image and optional installs
- `compose.yaml` — runtime hardening and mounts
- `entrypoint.sh` — container startup config
- `scripts/` — host-side orchestration
