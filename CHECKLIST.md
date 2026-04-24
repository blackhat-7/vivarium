# Pre-flight and post-setup checklist

Walk through this once. When every box is checked, the vivarium is ready.

## Before running any script

- [ ] You have a fresh Hetzner VM (any size, Ubuntu 22.04+ or Debian 12+).
- [ ] You can SSH in as root.
- [ ] You have read `PLAN.md` §1 (threat model) and §6 (residual risks).
- [ ] You accept the residual-risks table. If not, stop.

## After `scripts/setup-root.sh`

- [ ] User `keeper` exists: `id keeper`
- [ ] `keeper` has no sudo: `sudo -u keeper sudo whoami` fails
- [ ] Dev packages installed: `which bubblewrap socat tmux rg fd jq sqlite3 ffmpeg`
- [ ] iptables rule exists: `iptables -L OUTPUT -v | grep owner`
- [ ] iptables rule survives reboot: `systemctl is-enabled netfilter-persistent`

## After `scripts/setup-user.sh` (as `keeper`)

- [ ] `claude --version` prints a version
- [ ] `opencode --version` prints a version
- [ ] `~/.claude/settings.json` exists and has `"enabled": true` under `sandbox`
- [ ] `~/.config/opencode/opencode.json` exists
- [ ] `git config --global --get core.hooksPath` → `/dev/null`
- [ ] `git config --global --get core.excludesfile` → `/home/keeper/.gitignore_global`
- [ ] `npm config get ignore-scripts` → `true`
- [ ] `~/work` and `~/.secrets` exist; `~/.secrets` is mode 700
- [ ] Cron installed: `crontab -l` shows backup.sh and audit.sh entries

## GitHub PAT verification (the critical one)

- [ ] Token generated as **fine-grained**, not classic
- [ ] Token expiration is set (not "no expiration")
- [ ] Token scope: **only select repositories**, not all
- [ ] Token permissions: only **contents: read** and **metadata: read**
- [ ] Test clone over HTTPS works
- [ ] Test push fails with 403:

  ```bash
  cd ~/work/SOMEREPO
  echo vivarium-test > canary.txt
  git add canary.txt && git commit -m 'vivarium push test'
  git push       # MUST fail — expected 403
  git reset --hard HEAD~1
  rm -f canary.txt
  ```

## Network fence verification

- [ ] SSH out from `keeper` is blocked:

  ```bash
  ssh -o ConnectTimeout=5 git@github.com    # MUST fail
  ```

- [ ] HTTPS to github.com works:

  ```bash
  curl -sI https://github.com | head -1     # MUST return 200/301
  ```

- [ ] Arbitrary HTTPS is blocked by Claude's sandbox (test inside a Claude
  session by asking it to `curl https://example.com` — must be denied).

## Budget caps (done in provider consoles — VM can't verify these)

- [ ] Anthropic: hard monthly cap set on the VM's API key
- [ ] OpenRouter / OpenAI / other opencode providers: same
- [ ] VM's API keys are dedicated to the VM, not shared with your laptop

## Backup verification

- [ ] Do a dummy write in `~/work` and force a backup: `bash ~/vivarium/scripts/backup.sh`
- [ ] `ls ~/backup/` shows a directory named `work-HH`
- [ ] `ls ~/backup/work-HH/` contains your dummy write

## Tmux / re-attach drill

- [ ] Start a session: `tmux new -s demo`
- [ ] Inside it: `claude --dangerously-skip-permissions`, send it a tiny task
- [ ] Detach: `Ctrl-b d`
- [ ] From your laptop (new SSH): `ssh keeper@vm -t tmux a -t demo`
- [ ] You see the running session

When every box is checked, you're done. Put a calendar reminder:

- [ ] **90 days from now**: rotate GitHub PAT
- [ ] **First of every month**: read `~/vivarium/audit.log` output
- [ ] **Every 2 weeks**: `ssh keeper@vm 'sudo apt update && sudo apt upgrade'` (yes this needs sudo — add a narrow sudoers rule for `apt` only, or ssh as root for this one task)
