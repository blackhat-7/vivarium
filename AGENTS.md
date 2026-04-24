# AGENTS.md

Guidance for agents working on this repo. Read before editing anything.

## What vivarium is

A Docker-based sandbox that lets agents (opencode, claude code) run
autonomously on a user's existing Linux host with a bounded blast radius.
The worst-case incident is "restore yesterday's snapshot and rotate a key."
Never catastrophic loss.

`PLAN.md` is the spec. Read §1–§2 (threat model + architecture) before
touching the Dockerfile, compose.yaml, or any script that talks to docker,
cron, or secrets.

## Red lines — do not cross

Five guarantees are load-bearing. Weakening any of them is worse than
useless; the entire design falls apart.

1. **Non-root user in the container.** The Dockerfile ends with
   `USER vivarium`. No `--privileged`. Never `USER root` at the final stage.
2. **No docker socket mount.** Never add `/var/run/docker.sock` to
   `compose.yaml` for any reason. That's host-root equivalency through a
   back door.
3. **`cap_drop: ALL` is sacred.** The current minimal `cap_add` list is the
   ceiling. Adding a capability (`SYS_ADMIN`, `NET_ADMIN`, etc.) requires
   an explicit threat-model update in `PLAN.md` in the same commit.
4. **GitHub PAT is read-only, fine-grained, repo-scoped.** Documentation
   MUST steer users to fine-grained tokens with only `Contents: read` +
   `Metadata: read` (optionally `Issues: read`, `Pull requests: read`).
   Never classic PATs. Never `Administration`, `Secrets`, `Workflows`, or
   any Account-level permission.
5. **Fail-fast on installs.** Opt-in installs (opencode, claude, any future
   tool) that fail MUST stop the build with `[FATAL]` naming the flag to
   flip. Never swallow errors on a security-relevant path.

If a request seems to require relaxing any of the above, stop and say so
explicitly. Do not quietly comply.

## Design principles

- **Complexity is the enemy.** Before adding a file, a flag, a layer, or a
  dependency, check whether the three existing layers (host, container,
  PAT) already cover the threat. If yes, don't add it.
- **Preserve user state.** Scripts that touch `crontab`, `.env`,
  `~/vivarium-home`, or anything else user-owned MUST:
  - modify only vivarium-owned entries (grep for `vivarium/scripts` etc.);
  - be idempotent — re-running produces the same state, never duplicates;
  - never delete user data without an explicit opt-in flag
    (`remove.sh --data` is the pattern).
- **One source of truth per concern.** `PLAN.md` for spec, `.env` for
  runtime config, `compose.yaml` for invocation. Do not duplicate settings
  across files.
- **Bash, not frameworks.** No build system, no Python/Go orchestration,
  no Makefiles, no CI tooling. Scripts are short bash files with
  `set -euo pipefail`.
- **Stay inside the threat model.** Adding network allowances, relaxing
  caps, or broadening filesystem mounts without a `PLAN.md` justification
  is a regression, even if it "works."

## Workflow for a change

1. Read the relevant `PLAN.md` section.
2. If the change touches a red line, flag it first — do not write code.
3. Minimal diff. Do not refactor adjacent code "while you're there."
4. Test on a real host before pushing. This repo's workflow assumes an
   `ssh hetzner`-style reachable machine; do not invent local test
   harnesses.
5. If `PLAN.md` or `CHECKLIST.md` diverge from behavior after your change,
   update them in the same commit.

## File index

| File | Purpose |
|---|---|
| `PLAN.md` | Spec — threat model, architecture, setup, secrets, residuals, incident response. Source of truth. |
| `README.md` | Index + quick-start. Keep short. |
| `CHECKLIST.md` | Post-setup verification; stays in sync with PLAN.md. |
| `Dockerfile` | Image — non-root user, optional agents, fail-fast installs. |
| `compose.yaml` | Runtime hardening — caps, limits, mounts. |
| `entrypoint.sh` | Bootstraps `~/vivarium-home` from skeleton on first run. |
| `.env.example` | Template. Keep in sync with what `scripts/up.sh` upserts. |
| `scripts/` | Bash only; each script idempotent, preserves unrelated user state. |

## Commit style

Short imperative subject, lowercase. Body explains *why*, not *what*.
Reference `PLAN.md` sections when security-relevant. No emojis. No trailing
"summary of what I did" paragraph. No AI co-author line unless the user
explicitly asks for it.
