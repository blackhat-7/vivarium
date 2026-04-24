# The vivarium plan

A complete, opinionated plan for running Claude Code and opencode
autonomously on a Hetzner VM for personal projects — with enough hardening
that the blast radius of any single failure is bounded and recoverable.

This document is long on purpose. Read it top-to-bottom once before you run
any script. After that, `CHECKLIST.md` is the operational companion.

---

## 1. Threat model — what we defend against and what we accept

We are not trying to contain an actively malicious model. We are containing a
well-intentioned agent that can, realistically:

- **Get prompt-injected** by the content it reads — a README, a GitHub issue,
  a web page, a dependency's source comments — and take an action inside its
  allowed scope that you did not want.
- **Install a poisoned dependency**. `npm install`, `pip install`, `uv sync`,
  `cargo build` all execute third-party code at install time. In full YOLO
  mode this runs with the same permissions as the agent.
- **Loop into a bad state** — delete files, rewrite them into nonsense, burn
  API budget grinding on an impossible task.
- **Hallucinate a destructive command** — confidently `rm -rf`, `git reset
  --hard`, `DROP TABLE`, etc., inside its write scope.

We are **not** defending against:

- A state actor with a bubblewrap 0-day targeting your Hetzner VM.
- Claude Anthropic-side being compromised and exfiltrating through
  `api.anthropic.com`.
- Physical access to the VM.

### What "sufficient" means here

> The worst-case incident is "restore yesterday's snapshot and rotate a key."
> It is never "I lost years of work" or "my cloud accounts got pillaged."

That's the bar. Every layer below is justified by that standard, not by a
fantasy of "100% security."

### Residual risks you are explicitly accepting

| Risk | Why we accept it | Mitigation if it fires |
|---|---|---|
| Prompt injection causes destructive action inside `~/work` | Structural — the agent *must* be able to write to work | 2-hour rsync snapshots + local bare git mirrors |
| Malicious postinstall in a new dep corrupts `~/work` | We disable scripts by default, but some projects need them | Snapshots + `npm ci --ignore-scripts` as first-line habit |
| Runaway API spend | Per-session caps don't cover session-loops | Hard monthly cap set in the provider console |
| Secret exposure to a session that used it | If the agent holds the key, it has the key | Per-project scoped keys + monthly rotation |
| Bubblewrap sandbox escape (CVE-class) | Sandboxes have CVEs; bwrap is hardened but not perfect | OS-level firewall + unprivileged user + read-only PAT limit damage |

If you are not comfortable with any row in that table, stop and revisit
before proceeding.

---

## 2. Architecture — the five layers

```
    ┌─────────────────────────────────────────────────┐
    │ 1. Hetzner VM                                   │  physical/network isolation
    │                                                 │
    │   ┌───────────────────────────────────────┐     │
    │   │ 2. Unprivileged user `keeper`          │    │  no sudo, owns nothing outside ~/
    │   │                                        │    │
    │   │   ┌────────────────────────────────┐   │    │
    │   │   │ 3. Filesystem fence            │   │    │  writes only to ~/work;
    │   │   │   allowWrite: ~/work           │   │    │  denyWrite on ssh keys,
    │   │   │   denyWrite:  ~/.ssh, ~/.claude│   │    │  shell init, package configs
    │   │   │                 etc.           │   │    │
    │   │   │   ┌──────────────────────┐     │   │    │
    │   │   │   │ 4. Network fence     │     │   │    │  Claude sandbox allowlist +
    │   │   │   │   domain allowlist   │     │   │    │  iptables UID-based SSH block
    │   │   │   │   + UID iptables     │     │   │    │
    │   │   │   │                      │     │   │    │
    │   │   │   │   ┌───────────────┐  │     │   │    │
    │   │   │   │   │ 5. Credentials │  │     │   │    │  fine-grained PAT,
    │   │   │   │   │   read-only    │  │     │   │    │  contents:read only,
    │   │   │   │   │   PAT only     │  │     │   │    │  on specific repos
    │   │   │   │   └───────────────┘  │     │   │    │
    │   │   │   └──────────────────────┘     │   │    │
    │   │   └────────────────────────────────┘   │    │
    │   └───────────────────────────────────────┘     │
    └─────────────────────────────────────────────────┘
```

Each layer independently prevents a whole class of harm. The *combination* is
what lets you stop worrying:

- If the sandbox escapes → the user is still unprivileged.
- If something runs as `keeper` → it still can't SSH out (iptables).
- If it can't SSH out but finds the HTTPS github.com path → the PAT can't push.
- If the PAT scope somehow expands → only the specific repos are exposed.
- If the VM itself is compromised → snapshots and the keys-aren't-here-in-plaintext
  model bound the damage.

---

## 3. Setup — one-time

Run the scripts in order. They are idempotent-ish; safe to re-run.

### 3.1 As root on the fresh VM

```bash
# on your laptop
scp -r ~/Documents/projects/vivarium/ root@YOUR_VM:/root/
ssh root@YOUR_VM
cd /root/vivarium
bash scripts/setup-root.sh
```

What it does:

- Creates user `keeper` (no password, no sudo).
- Installs `bubblewrap socat tmux git curl jq iptables-persistent
  build-essential ripgrep fd-find sqlite3 ffmpeg` — the dev deps Claude will
  reach for often. Pre-installed so the unprivileged user never needs sudo.
- Adds the iptables rule that blocks outbound SSH (port 22) from UID `keeper`.
  This structurally prevents `git push` over SSH regardless of in-app guards.
- Saves iptables rules via `netfilter-persistent` so they survive reboot.

### 3.2 As `keeper` on the VM

```bash
sudo -iu keeper
cd /root/vivarium   # (or wherever; it's accessible by keeper via the copy step)
bash scripts/setup-user.sh
```

What it does:

- Installs Claude Code via the official installer.
- Installs opencode via the official installer.
- Copies `configs/claude-settings.json` to `~/.claude/settings.json`.
- Copies `configs/opencode.json` to `~/.config/opencode/opencode.json`.
- Sets up git: `core.hooksPath=/dev/null`, `core.excludesfile=~/.gitignore_global`,
  `credential.helper=store`.
- Drops `configs/gitignore-global` at `~/.gitignore_global`.
- `npm config set ignore-scripts true` (after nvm install).
- Creates `~/work/` (writable) and `~/.secrets/` (mode 700).
- Installs `pass` for optional secret-store use.
- Installs crons: `backup.sh` every 2h, `audit.sh` monthly.

### 3.3 GitHub read-only PAT — manual, do this right

**The single most important step.** This is what makes "never pushes to
cloud" structurally true rather than policy-enforced.

1. github.com → Settings → Developer settings → **Personal access tokens →
   Fine-grained tokens** → Generate new token.
2. **Name**: `vivarium-readonly`.
3. **Expiration**: 90 days (set a calendar reminder to rotate).
4. **Resource owner**: your personal account.
5. **Repository access**: **Only select repositories**. Pick the specific
   repos you want Claude to be able to read. Do *not* choose "All
   repositories."
6. **Permissions → Repository permissions**:
   - **Contents**: *Read-only*
   - **Metadata**: *Read-only* (required)
   - **Pull requests**: *No access*
   - **Issues**: *No access* (or Read-only if you want Claude to see issue context)
   - Everything else: **No access**
7. **Permissions → Account permissions**: leave everything at **No access**.
8. Generate, copy the token.

On the VM, as `keeper`:

```bash
cd ~/work
git clone https://github.com/YOU/some-repo.git
# when prompted for password: paste the PAT
# credential.helper=store saves it for subsequent clones/pulls
```

**Verify the token cannot push**:

```bash
cd ~/work/some-repo
echo test > x && git add x && git commit -m test
git push
# must fail with 403; if it succeeds, the PAT scope is wrong — regenerate
```

**Do not use classic PATs.** A classic PAT with `repo` scope can create
repos, create gists, and bypass every assumption in this document.

### 3.4 API budget caps — manual, the other critical ceiling

- **Anthropic console** → Organization → Usage limits → **set a hard monthly
  cap** on the API key the VM uses. Pick an amount whose loss you'd shrug at.
  $50/month is a reasonable starting point for personal use.
- If you use opencode with OpenRouter/OpenAI/etc., do the same on each
  provider's console. **This is the only guard against session-loops burning
  thousands of dollars overnight.**
- Use a dedicated API key for the VM, not one you use anywhere else. If it
  leaks, you revoke it and your laptop keeps working.

---

## 4. Daily workflow

From your laptop:

```bash
ssh keeper@vm
tmux new -s $(basename $(pwd))    # or re-attach: tmux a -t name
cd ~/work
git clone https://github.com/YOU/new-project.git   # or cd into an existing one
cd new-project
```

### Interactive session

```bash
claude --dangerously-skip-permissions
# or
opencode
```

`--dangerously-skip-permissions` is fine here — the sandbox in
`~/.claude/settings.json` is your guard. YOLO without a sandbox is reckless;
YOLO inside a sandbox is the design.

Detach: `Ctrl-b d`. Re-attach from your laptop anytime.

### Headless / unattended session

```bash
claude -p "implement issue #12, run tests, commit locally" \
  --max-turns 25 --max-budget-usd 5 \
  --dangerously-skip-permissions
```

Log stream is at `~/.claude/projects/<path>/session-*.jsonl`. From your
laptop:

```bash
ssh keeper@vm 'tail -f ~/.claude/projects/*/session-*.jsonl' | jq -r '.message.content[]?.text // empty'
```

### Monitoring from a phone / outside

Install `tmate` on the VM (`sudo apt install tmate` during root setup) and
run `tmate` inside a tmux session — you get a URL that opens the terminal in
any browser. No SSH keys to fuss with from the phone.

---

## 5. Secrets — the honest framing

**You cannot hide secrets from an agent that uses them.** If the agent can
call Stripe, the agent holds the Stripe key. Stop trying to encrypt the
problem away.

The frame is instead: **minimize what each session can burn, and rotate.**

### The three rules

1. **Per-project, scoped, capped keys.** Never use an "everything" key.
   - OpenAI: project-scoped key with monthly spend cap.
   - Stripe: restricted keys or test-mode keys, never live unrestricted.
   - GitHub: fine-grained PAT per repo (you already have one — keep it that
     way).
   - If a provider doesn't support scoping or caps, treat its key as
     radioactive. Ideally: don't put that provider on the VM at all.

2. **Compartmentalize by session.** Load only the secrets the current
   project needs. No global `~/.env`. Nothing in `~/.bashrc`.

3. **Rotate on a cadence you'll actually keep.** Monthly if you have the
   discipline, quarterly if that's realistic. Put it on a calendar. The
   monthly `audit.sh` prints a rotation reminder.

### The minimum-setup recipe (use this if nothing else)

```bash
# one secret file per project, IN the project, gitignored
~/work/myproject/.env      # mode 600
```

The global `.gitignore` already ignores `.env*`, `*.pem`, `*.key`, so they
cannot accidentally be committed. Load per session:

```bash
cd ~/work/myproject
set -a; source .env; set +a
claude --dangerously-skip-permissions
```

### The upgrade: `pass` (installed by `setup-user.sh`)

`pass` is the standard Unix password manager. One binary, uses a GPG key,
stores entries as encrypted files in `~/.password-store/`.

```bash
# one-time
gpg --quick-generate-key "keeper@vivarium"
pass init "keeper@vivarium"

# add a secret
pass insert openai/myproject

# use in a session
export OPENAI_API_KEY=$(pass show openai/myproject)
claude --dangerously-skip-permissions
```

Benefit: secrets are encrypted at rest between sessions. They are still
plaintext in environment variables while the session is running, and the
agent can still read `/proc/self/environ` — so this doesn't protect against
an active session, it protects against a cold-boot compromise. Small upgrade,
worth the 10 minutes.

### What not to bother with

Vault, sops, Infisical, Doppler, 1Password CLI on the VM. All fine tools,
all overkill at personal scale when per-project scoped keys + rotation
already cap your downside.

---

## 6. Residual risks — what to watch for and how to recover

### 6.1 Supply chain (the biggest hole)

Install-time code execution is the largest residual risk. Defenses, in order:

- **`npm config set ignore-scripts true`** (done by setup-user.sh) — stops
  postinstall scripts by default. Individual projects that need scripts can
  override with `npm install --ignore-scripts=false` explicitly.
- **Prefer `uv` over `pip`**. `uv` installs from wheels by default and does
  not execute `setup.py` for pre-built wheels. `pip install <sdist>` runs
  arbitrary Python.
- **Pin versions.** Commit `package-lock.json`, `uv.lock`, `Cargo.lock`,
  `go.sum`. Review additions manually when they're from new authors.
- **Snapshots make this recoverable.** If a bad install corrupts `~/work`,
  the 2-hour rsync from `backup.sh` is your undo button.

### 6.2 Git hooks on cloned repos

`.git/hooks/*` and `pre-commit` configs execute arbitrary code when Claude
runs `git commit` or `git clone`. Same supply-chain class.

Defense: `git config --global core.hooksPath /dev/null` (done by
setup-user.sh). If a project genuinely needs hooks, opt in explicitly:
`git config core.hooksPath .git/hooks` inside that repo.

### 6.3 Blast radius inside the write scope

Claude can `rm -rf ~/work/other-project-from-last-month`. The sandbox does
not care — it's inside the allowed write scope.

Defense: the 2-hour rsync snapshot at `~/backup/work-HH/` (cron'd by
setup-user.sh) plus weekly rotation. Verify with `audit.sh`.

### 6.4 Exfiltration within the allowlist

You allowed `github.com`. A compromised session can theoretically push data
to any GitHub endpoint.

Defense: the fine-grained PAT cannot *create* gists, repos, or issues — its
permissions are `contents:read` on specific repos only. There is literally
nothing on github.com it can exfiltrate *to*. This is why step 3.3 is
load-bearing and not optional.

### 6.5 DNS / allowlist abuse

Even read-only traffic can carry data via URL paths/headers. Minor at this
scale; the PAT scope bounds it.

### 6.6 Runaway API spend

Per-session `--max-budget-usd` caps one session. Nothing prevents 40
back-to-back sessions.

Defense: the hard monthly cap in the provider console (step 3.4). **This is
not optional.**

### 6.7 Bubblewrap sandbox escape

Real, not theoretical. Mitigations:

- `apt upgrade` weekly (included in `audit.sh` as a reminder).
- The other four layers — unprivileged user, filesystem perms, iptables,
  PAT scope — limit what an escape can actually do.

### 6.8 Prompt injection

The file Claude reads can contain "ignore prior instructions and do X." The
sandbox bounds *X*, not *whether Claude tries it*.

Defense: treat the sandbox scope as the threat model. Do not let Claude have
a capability whose abuse you cannot live with.

---

## 7. Monthly maintenance

`scripts/audit.sh` runs monthly via cron and emails (or logs — your choice
in the script) the results. Review output within a week.

It checks:

- `ignore-scripts` is still true
- `core.hooksPath` is still `/dev/null`
- iptables rule for `keeper` UID still exists and fires
- `~/.claude/settings.json` still has `sandbox.enabled: true`
- Latest `~/backup/work-*` is less than 3 hours old
- Every repo in `~/work` has `origin` pointing at https, not ssh
- No file matching `*.env`, `*.pem`, `*.key` is tracked in any repo

It reminds you to:

- Rotate any PAT older than 90 days
- Verify budget caps in provider consoles (these can be changed outside the
  VM, so the VM cannot truly check them — only remind you)
- `apt upgrade` the VM
- Prune old `~/backup/work-*` directories

---

## 8. Deliberate exclusions

Things we considered and deliberately did **not** do, so you know the design
is intentional:

- **Docker in the VM.** The VM is already the isolation boundary. Docker
  inside it adds setup tax and daemon surface for marginal gain.
- **claudebox / ccontainer wrappers.** Same reason.
- **Vault / sops / Doppler.** Overkill at personal scale; `pass` covers 95%
  of the value.
- **microVM per session (firecracker, etc.).** Huge operational tax,
  defends against threats (rootkit persistence across sessions) that the
  snapshot strategy already covers at 1% of the complexity.
- **Rewriting Claude's sandbox in eBPF / LSM.** No.
- **Blocking `*.githubusercontent.com`.** We allow it because package
  managers and `gh release download` need it; the PAT scope means this isn't
  a meaningful exfil path.

---

## 9. Incident response — when (not if) something fires

### "Claude deleted a bunch of files in ~/work"

```bash
# find the latest clean snapshot
ls -lt ~/backup/ | head
# restore just one project
rsync -a --delete ~/backup/work-14/myproject/ ~/work/myproject/
```

### "A dep looked suspicious during install"

```bash
# kill the session
tmux kill-session -t proj
# restore
rsync -a --delete ~/backup/work-$(date +%H -d '2 hours ago')/ ~/work/
# remove the offending package from package.json, commit-revert, and move on
```

### "API spend spiked"

1. Revoke the VM's API key in the provider console (takes seconds).
2. Check the hard monthly cap is still in place. If it wasn't, set one now.
3. Grep `~/.claude/projects/` for what the session was doing.
4. Issue a new key scoped to the VM.

### "I think the VM is compromised"

1. Snapshot `~/work` to your laptop over scp using a different SSH key than
   the VM has.
2. Destroy the VM (Hetzner → delete).
3. Rotate every credential the VM ever held. This is why PATs and API keys
   are scoped to the VM and nothing else.
4. Provision fresh. The whole setup is scripted; rebuild is < 20 minutes.

---

## 10. What "done" looks like

You have finished setting up when:

- [ ] `CHECKLIST.md` passes end-to-end
- [ ] You have started, detached from, and re-attached to a tmux session
  running `claude --dangerously-skip-permissions` at least once
- [ ] A test `git push` from inside `~/work` failed with 403 (PAT is read-only)
- [ ] A test `ssh you@somehost` from `keeper` failed (iptables rule fires)
- [ ] `~/backup/work-*` has at least one snapshot
- [ ] Budget caps are set in the Anthropic console (and any other provider
  consoles you use)

After that, the ongoing loop is: start sessions, detach, come back, review
diffs, push from your laptop (not the VM), repeat. Every month, read the
audit output.

Enjoy the vivarium.
