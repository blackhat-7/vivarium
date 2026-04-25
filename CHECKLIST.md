# Pre-flight and post-setup checklist

Walk through once. When every box is checked, the vivarium is ready.

## Before running anything

- [ ] Host has Docker installed and you can run `docker ps` without sudo.
- [ ] You've read `PLAN.md` §1 (threat model) and §6 (residual risks).
- [ ] You accept the residual-risks table. If not, stop and rethink.

## After `./scripts/up.sh`

- [ ] `.env` was created with your host UID/GID: `cat .env`
- [ ] Image built: `docker images | grep vivarium`
- [ ] Container running: `docker ps | grep vivarium`
- [ ] `~/vivarium-home/` exists on the host
- [ ] `./scripts/shell.sh` drops you into a bash prompt at `/home/vivarium/work`

## Inside the container — one-time setup

- [ ] `opencode --version` prints a version
- [ ] `claude --version` prints a version (or errored cleanly during image build; optional)
- [ ] `opencode auth login <provider>` completed; auth stored under `~/.config/opencode/`
- [ ] `git config --global --get core.hooksPath` returns `/dev/null`
- [ ] `npm config get ignore-scripts` returns `true`
- [ ] If `INSTALL_BESTIARY=true`: `bestiary list` prints registered tools (one line per tool)
- [ ] If `INSTALL_BESTIARY=true`: `~/.config/opencode/opencode.json` has a `mcp.bestiary` entry (auto-wired by entrypoint on first start)
- [ ] If `INSTALL_BESTIARY=true`: `~/.claude.json` has a `mcpServers.bestiary` entry (auto-wired by entrypoint on first start)

## GitHub PAT verification (the critical one)

- [ ] Token generated as **fine-grained**, not classic
- [ ] Repository access: **only select repositories**, not "all repositories"
- [ ] Repository permissions — **Read only on**: Metadata, Contents, Issues, Pull requests
- [ ] Repository permissions — **No access on** (critical): Administration, Secrets, Workflows, Actions, Deployments, Environments, Variables, Webhooks, Pages, Dependabot, Security advisories, everything else
- [ ] **Account permissions**: every single one set to **No access**
- [ ] Token summary screen shows 3–5 `Read` lines and nothing with `Write` or any account scope

- [ ] Test clone works over HTTPS (inside the container):
  ```bash
  cd ~/work
  git clone https://github.com/YOU/SOMEREPO.git
  ```

- [ ] Test push fails with 403:
  ```bash
  cd ~/work/SOMEREPO
  echo vivarium-test > canary.txt
  git add canary.txt && git commit -m 'vivarium push test'
  git push       # MUST fail — expected 403
  git reset --hard HEAD~1
  rm -f canary.txt
  ```

## Spend caps (outside the VM — container can't verify these)

Pick the row that matches your provider:

### Subscription (Claude Pro/Max, Copilot, ChatGPT Plus/Pro) — default

- [ ] `opencode auth login <provider>` completed
- [ ] You know where to revoke if anything goes wrong (see PLAN.md §3)
- [ ] Rate limits are your spending cap — nothing else to do

### Pay-per-token API (Anthropic API direct, OpenRouter, OpenAI API, Gemini billed)

- [ ] Hard monthly cap set in the provider console
- [ ] The VM's API key is dedicated (not shared with your laptop)
- [ ] First session tested with a small budget limit before raising

## Backups (on the host)

- [ ] Cron entries installed: `crontab -l` shows `backup.sh` and `audit.sh`
- [ ] Initial backup run: `bash ~/vivarium/scripts/backup.sh`
- [ ] `~/vivarium-backup/hourly-HH/` exists and mirrors `~/vivarium-work/`

## Container hardening verification

- [ ] Non-root user inside: `docker exec vivarium id` → `uid=<your host uid>`, not 0
- [ ] `no-new-privileges` active: `docker inspect vivarium | grep NoNewPrivileges` → `true`
- [ ] Caps dropped: `docker inspect vivarium -f '{{.HostConfig.CapDrop}}'` → `[ALL]`
- [ ] No `/var/run/docker.sock` mounted: `docker inspect vivarium | grep docker.sock` → nothing
- [ ] Resource limits applied: `docker stats vivarium` shows memory cap

## Full-loop drill

- [ ] Start a tmux session inside, launch `opencode`, give it a tiny task, detach
- [ ] From your laptop, re-attach via `ssh hetzner -t 'cd vivarium && ./scripts/shell.sh'` then `tmux a`
- [ ] Session resumes cleanly

When every box is checked, you're done.
