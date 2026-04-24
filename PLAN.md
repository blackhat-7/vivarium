# The vivarium plan (Docker edition)

An opinionated, minimum-moving-parts setup for running opencode and claude
code autonomously on your existing Linux host — agent in a locked-down
container, code bind-mounted from the host, read-only GitHub PAT as the
structural guarantee that nothing pushes.

Read top to bottom once before running. `CHECKLIST.md` is the post-setup
verification drill.

---

## 1. Threat model — what we defend against, what we accept

We are not trying to contain a state-actor-weaponized model. We are
containing a well-intentioned agent that can, realistically:

- Get **prompt-injected** by files it reads (READMEs, issue bodies, docs,
  dependency sources) and take an action inside its allowed scope that you
  did not want.
- Install a **poisoned dependency** (`npm install`, `pip install`, `uv sync`
  all execute third-party code at install time in full YOLO mode).
- **Loop into a bad state** — delete, rewrite into nonsense, burn quota.
- **Hallucinate a destructive command** — `rm -rf`, `git reset --hard`,
  `DROP TABLE`.

We are **not** defending against: a container-escape 0-day, Anthropic-side
compromise exfiltrating through `api.anthropic.com`, or physical access.

> **What "sufficient" means**: the worst-case incident is "restore yesterday's
> snapshot and rotate a key." Never "I lost years of work" or "my cloud
> accounts got pillaged."

### Residual risks you are explicitly accepting

| Risk | Why we accept it | Mitigation if it fires |
|---|---|---|
| Prompt injection causes destructive action in `~/vivarium-work` | Structural — the agent must write to work | 2-hour rsync snapshots |
| Malicious postinstall corrupts the work dir | We disable scripts by default; some projects need them | Snapshots + `docker compose down && up` resets container state |
| Runaway spend (pay-per-token only) | Session caps don't cover loops across sessions | Provider-console hard monthly cap. Subscriptions are rate-limited → capped |
| Secret exposure to a session that used it | If the agent holds the key, it has the key | Per-project scoped keys + rotation |
| Container-escape CVE (shared kernel) | Sandboxes have CVEs; Docker is hardened but not perfect | Non-root user, `cap_drop: ALL`, `no-new-privileges`, no `/var/run/docker.sock` mount |

---

## 2. Architecture — three layers

```
    ┌──────────────────────────────────────────────────┐
    │ 1. HOST (your existing Linux VM)                 │
    │                                                  │
    │   ┌────────────────────────────────────────┐     │
    │   │ 2. CONTAINER `vivarium`                │     │
    │   │    • ubuntu 24.04 base                 │     │
    │   │    • runs as UID 1000 (non-root)       │     │
    │   │    • cap_drop: ALL + no-new-privileges │     │
    │   │    • no docker.sock, not privileged    │     │
    │   │    • no SSH client useful for pushing  │     │
    │   │    • cpu/mem/pid limits                │     │
    │   │    • mounts only: ~/vivarium-home      │     │
    │   │                                        │     │
    │   │   ┌──────────────────────────┐         │     │
    │   │   │ 3. GitHub fine-grained   │         │     │
    │   │   │    read-only PAT         │         │     │
    │   │   │    (contents:read only,  │         │     │
    │   │   │     specific repos)      │         │     │
    │   │   └──────────────────────────┘         │     │
    │   └────────────────────────────────────────┘     │
    └──────────────────────────────────────────────────┘
```

Each layer independently blocks a class of harm:

- Container escape → still a non-root user on the host (and your host user
  has no write access to other users' data).
- Something runs unexpectedly in the container → the only write-accessible
  path outside the container is `~/vivarium-home`, which is snapshotted.
- Agent tries to `git push` → read-only PAT returns 403 regardless of what
  the agent does inside.

### What we explicitly dropped vs. the original VM plan

| Dropped | Why |
|---|---|
| Claude's `sandbox.filesystem` + `network` allowlist | The container namespace is the filesystem fence. The PAT is the real push blocker. Allowlist was maintenance tax with marginal extra value. |
| UID-based host iptables rule | Docker writes its own iptables rules; our rule could conflict. Container net ns is the isolation instead. |
| Dedicated OS user `keeper` | Container UID 1000 is the "keeper" — inside the namespace, not on the host. |
| `netfilter-persistent`, `apt` install choreography | All baked into the image once. |

---

## 3. Setup

### 3.1 On the host (one time)

```bash
git clone https://github.com/blackhat-7/vivarium.git ~/vivarium
cd ~/vivarium
./scripts/up.sh
```

`up.sh` writes `.env` with your host UID/GID and default agent selection
(opencode only), builds the image (first build: ~3 min), and starts the
container detached. It's idempotent — safe to re-run.

**Agent selection**: by default the image bakes in `opencode`. To also bake
in `claude` code, edit `.env` and set `INSTALL_CLAUDE=true`, then re-run
`./scripts/up.sh` (it rebuilds with the new args). At least one of
`INSTALL_OPENCODE` / `INSTALL_CLAUDE` must be true — if both are false or
either install fails mid-build, the build stops with a `[FATAL]` message
pointing at the flag to flip.

### 3.2 Inside the container (one time)

```bash
./scripts/shell.sh         # drops into /home/vivarium/work
opencode auth login        # pick provider, follow the OAuth flow
```

For opencode: your subscription (Claude Pro/Max, Copilot, ChatGPT Plus/Pro)
is the billing model. Rate limits cap runaway spend; no console config
needed.

For claude code (optional, API-billed): `claude` on first run prompts for
auth. If you use this, set a hard monthly cap on the key in the Anthropic
console.

### 3.3 GitHub PAT — the structural push-blocker

The single most important step. This is what makes "never pushes" a
structural guarantee instead of a policy hope.

1. github.com → Settings → Developer settings → **Personal access tokens →
   Fine-grained tokens** → Generate new token.
2. **Name**: `vivarium-readonly`.
3. **Resource owner**: your personal account.
4. **Repository access**: **Only select repositories** — pick specific ones.
   Never "All repositories."
5. **Repository permissions** — set every row explicitly:

   | Permission | Setting |
   |---|---|
   | **Metadata** | **Read** *(mandatory — GitHub requires it)* |
   | **Contents** | **Read** *(lets you clone and pull)* |
   | **Issues** | **Read** *(so the agent can see issue bodies)* |
   | **Pull requests** | **Read** *(PR history / review comments)* |
   | **Administration** | **No access** *(critical — blocks repo creation/settings)* |
   | **Secrets** | **No access** *(critical — never grant)* |
   | **Workflows** | **No access** *(critical — a compromised Action is a persistent backdoor)* |
   | Everything else | **No access** |

6. **Account permissions**: every single one set to **No access**. These
   include "create gists," "email addresses," "profile" — the scariest
   scopes.

After creation the token summary should list 3–5 `Read` lines and nothing
with `Write` or any account scope. If it shows anything more, regenerate.

**Verify the token cannot push**, inside the container:

```bash
cd ~/work
git clone https://github.com/YOU/some-repo.git   # paste PAT when prompted
cd some-repo
echo test > canary.txt && git add canary.txt && git commit -m test
git push      # MUST fail with 403. If it succeeds, the PAT scope is wrong.
git reset --hard HEAD~1
rm canary.txt
```

### 3.4 Backups + audit cron (on the host)

```bash
./scripts/cron-install.sh     # backup every 2h + audit monthly
./scripts/cron-uninstall.sh   # remove them later if you want
```

Both are idempotent. `cron-install.sh` replaces any existing vivarium
entries and leaves unrelated crontab lines alone. `cron-uninstall.sh` only
touches vivarium lines — your backup directory and log files are untouched.

Snapshots go to `~/vivarium-backup/hourly-HH/` (overwritten each day) and
`~/vivarium-backup/daily-N/` (day-of-week, 7-day rolling).

---

## 4. Daily workflow

```bash
./scripts/shell.sh                # drop into the container
cd ~/work/some-project            # or `git clone` a new one
opencode                          # or `claude` for API-billed sessions
```

Detached work:

```bash
tmux new -s proj
opencode
# Ctrl-b d to detach

# re-attach later:
./scripts/shell.sh
tmux a -t proj
```

Monitoring from your laptop:

```bash
ssh hetzner 'docker logs -f vivarium'
```

If a session weirds out, pick the right nuke button:

| Command | What it does |
|---|---|
| `docker compose restart` | Restart container; home + code + auth preserved |
| `docker compose down && ./scripts/up.sh` | Rebuild container; home + code + auth still preserved (volume persists) |
| `docker compose down && rm -rf ~/vivarium-home/.config && ./scripts/up.sh` | Kill opencode/claude auth but keep code |
| `docker compose down && rm -rf ~/vivarium-home && ./scripts/up.sh` | Total wipe — use only if you really mean it. Your code goes too. |

---

## 5. Secrets

**You cannot hide secrets from an agent that uses them.** The frame isn't
"encrypt them" — it's "minimize what each session can burn, and rotate."

### Three rules

1. **Per-project, scoped, capped keys.** Never an "everything" key. OpenAI
   project-scoped with spend cap. Stripe test/restricted keys. If a provider
   doesn't support scoping, treat its key as radioactive — consider whether
   it belongs on this host at all.
2. **Compartmentalize by session.** Only load the secrets the current
   project needs. No global `~/.env`. Nothing in `~/.bashrc`.
3. **Rotate on a cadence you'll keep.** Monthly if you have the discipline,
   quarterly otherwise.

### The recipe

One `.env` per project, inside the project, mode 600. The global gitignore
(baked into the container via entrypoint) ignores `.env*`, `*.pem`, `*.key`,
so they can't accidentally commit.

```bash
# inside the container:
cd ~/work/myproject
set -a; source .env; set +a
opencode
```

### Optional upgrade: `pass`

`pass` is pre-installed in the image. One GPG key, encrypted files in
`~/.password-store/`. Setup (once):

```bash
gpg --quick-generate-key "vivarium@yourname"
pass init "vivarium@yourname"
pass insert openai/myproject     # paste secret
```

Use per session:

```bash
export OPENAI_API_KEY=$(pass show openai/myproject)
opencode
```

Encrypts at rest. Still plaintext in env vars while the session runs — this
doesn't protect against a running session, just cold storage.

### What not to bother with

Vault, sops, Infisical, Doppler. Overkill at personal scale when per-project
scoped keys + rotation already cap downside.

---

## 6. Residual risks (longer form)

### 6.1 Supply chain (largest residual hole)

`npm install`, `pip install`, `uv sync`, `cargo build` all run third-party
code at install time as the in-container user.

Defenses:

- `npm config set ignore-scripts true` is set **globally in the image**;
  projects that need scripts opt in explicitly with `--ignore-scripts=false`.
- Prefer `uv` over `pip` — wheels don't execute setup.py; sdists still do.
- Pin versions. Commit lockfiles. Review additions from new authors manually.
- **Snapshots make this recoverable**. If `~/vivarium-work` gets corrupted,
  `rsync -a --delete ~/vivarium-backup/hourly-HH/ ~/vivarium-work/` puts it
  back in 2 seconds.

### 6.2 Git hooks

`.git/hooks/*` and `pre-commit` execute arbitrary code on commit. Defense:
`core.hooksPath=/dev/null` is set globally by entrypoint.sh on first run. If
a project needs hooks, opt in inside that repo: `git config core.hooksPath
.git/hooks`.

### 6.3 Blast radius inside the work dir

The agent can `rm -rf ~/work/old-project`. Same answer: snapshots.

### 6.4 Container escape via kernel CVE

Real, not theoretical. Mitigations layered in `compose.yaml`:

- Runs as non-root (UID 1000).
- `cap_drop: ALL` plus a minimal `cap_add` list.
- `no-new-privileges:true`.
- No Docker socket mount, not `--privileged`.
- Your host user's other files remain invisible because only
  `~/vivarium-home` is bind-mounted in.

Apply host kernel updates periodically. The Arch host's rolling release does
this naturally; just reboot occasionally.

### 6.5 Exfiltration to arbitrary hosts

We deliberately dropped the network allowlist (see §2). A compromised
session *can* `curl evil.com`. The guards that still hold:

- The read-only PAT gives the session nothing sensitive to exfiltrate from
  your GitHub account (can't create gists, can't write to any repo).
- Your LLM subscription OAuth token could be exfiltrated — revoke from the
  provider's console if suspicious activity appears.
- Per-project secrets you source temporarily are in env while running — rate
  limits and provider spend caps bound their damage.

If the risk feels wrong, optionally add a filtering DNS (`dns: [1.1.1.1]` in
compose.yaml, point at NextDNS / a local allowlist). But that's complexity
creep — revisit only if you have a reason.

### 6.6 Prompt injection

The file the agent reads can say "ignore prior instructions and do X." The
sandbox bounds *X*, not *whether Claude tries*. Do not let the agent have
capabilities whose abuse you cannot live with.

---

## 7. Maintenance

### Monthly — run `audit.sh` (or wait for the cron to mail its log)

It checks:

- `vivarium` container running
- Latest backup < 4 hours old
- All repos in work dir use https origins (not ssh — would imply push-capable)
- No tracked `.env` / `.pem` / `.key` files

### Periodic — refresh the base image

```bash
cd ~/vivarium
docker compose down
docker compose build --pull
./scripts/up.sh
```

Pulls a fresh Ubuntu 24.04, re-runs installers. Your home dir and auth
persist across the rebuild (they live on the host).

### Ad-hoc — inspect running sessions

```bash
docker logs vivarium                                # recent container output
docker stats vivarium                               # cpu/mem usage right now
docker compose exec vivarium ps auxf                # what's running inside
```

---

## 8. Deliberate exclusions

What we considered and did **not** do, so you know the design is intentional:

- **Network-layer domain allowlist.** Maintenance cost > value given PAT
  scope does the heavy lifting. Add later if you have a reason.
- **Rootless Docker / Podman.** Your host already runs Docker. Migration tax
  > incremental security gain in your threat model.
- **User-namespace remapping (`userns-remap`).** Meaningful extra protection
  but Docker-daemon-wide config change that could affect your other
  containers. Skip unless you're ready to own that.
- **One container per project.** Simpler to share one. Work directory
  distinguishes projects. Switch later if a project's deps pollute the
  shared container.
- **`apparmor` / `seccomp` custom profiles.** Docker's default profiles
  already drop dangerous syscalls. Custom profiles are a project of their
  own.
- **HashiCorp Vault / sops / Doppler.** Overkill at personal scale.

---

## 9. Incident response

### "The agent deleted files in my work dir"

```bash
ls -lt ~/vivarium-backup/hourly-*/    # find a clean one
rsync -a --delete ~/vivarium-backup/hourly-14/myproject/ ~/vivarium-work/myproject/
```

### "An install looked suspicious mid-session"

```bash
docker compose restart                            # nuke session state
# optional: reset work dir to a clean backup (see above)
```

### "I don't trust the container right now"

```bash
docker compose down
docker rmi vivarium:latest                        # force rebuild from scratch
./scripts/up.sh
```

Your `~/vivarium-home/` code and auth survive. Rebuild is ~3 min.

### "Get vivarium off my host entirely"

```bash
./scripts/remove.sh                  # reversible: container+image+cron, data preserved
./scripts/remove.sh --data           # also delete ~/vivarium-home (code + auth)
./scripts/remove.sh --backups        # also delete ~/vivarium-backup + logs
./scripts/remove.sh --everything     # all of the above + delete the repo dir itself
./scripts/remove.sh --dry-run        # preview without changing anything
```

All flags combinable with `--yes` to skip confirmation. See `./scripts/remove.sh --help`.

### "I don't trust the HOST right now"

Different problem. The vivarium design doesn't protect the host from the
host being compromised by other means. If it's this bad: snapshot
`~/vivarium-home/` off-host immediately, then rotate every credential the
host touched, then investigate.

### "API spend spiked" (pay-per-token only)

1. Revoke the VM's API key in provider console.
2. Verify the hard monthly cap exists; if not, set one now.
3. `docker logs vivarium | tail -n 500` to see what it was doing.
4. Issue a new scoped key.

---

## 10. What "done" looks like

Setup is complete when:

- [ ] `./scripts/up.sh` completes; `docker ps` shows `vivarium` running
- [ ] `./scripts/shell.sh` drops you into `/home/vivarium/work`
- [ ] `opencode auth login` succeeded with your subscription provider
- [ ] A test `git clone` over HTTPS works inside the container
- [ ] A test `git push` fails with 403 (PAT is read-only)
- [ ] Backup cron added; `scripts/backup.sh` run manually produced a
  snapshot under `~/vivarium-backup/`
- [ ] For pay-per-token providers: monthly cap set in provider console

After that, the loop is: shell in, work, detach, come back, review diffs
*from your laptop*, push from your laptop. Every month, read the audit log.

Enjoy the vivarium.
