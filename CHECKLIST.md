# Pre-flight and post-setup checklist

Walk through once. When every box is checked, the vivarium is ready.

## Before running anything

- [ ] Host has Docker installed and you can run `docker ps` without sudo.
- [ ] You will run `./scripts/up.sh` as your normal non-root host user, not with `sudo`.
- [ ] You've read `PLAN.md` §1 (threat model) and §6 (residual risks).
- [ ] You accept the residual-risks table. If not, stop and rethink.

## After `./scripts/up.sh`

- [ ] `.env` was created with your host UID/GID: `cat .env`
- [ ] Image built: `docker images | grep vivarium`
- [ ] Container running: `docker ps | grep vivarium`
- [ ] `~/vivarium-home/` exists on the host
- [ ] `./scripts/shell.sh` drops you into a bash prompt at `/home/vivarium/work`

## Inside the container — one-time setup

- [ ] If `INSTALL_OPENCODE=true`: `opencode --version` prints a version
- [ ] If `INSTALL_CLAUDE=true`: `claude --version` prints a version
- [ ] If using opencode: `opencode auth login <provider>` completed; auth stored under `~/.config/opencode/`
- [ ] `git config --global --get core.hooksPath` returns `/dev/null`
- [ ] `git config --global --get credential.helper` returns `cache --timeout=86400`
- [ ] `npm config get ignore-scripts` returns `true`
- [ ] If `INSTALL_BESTIARY=true`: `bestiary list` prints registered tools (one line per tool)
- [ ] If `INSTALL_BESTIARY=true`: `~/.config/opencode/opencode.json` has a `mcp.bestiary` entry (auto-wired by entrypoint on container start when missing)
- [ ] If `INSTALL_BESTIARY=true`: `~/.claude.json` has a `mcpServers.bestiary` entry (auto-wired by entrypoint on container start when missing)
- [ ] If `INSTALL_PASEO=true`: `paseo --version` prints a version

## Paseo remote-access (only if `INSTALL_PASEO=true` and `PASEO_ENABLE=true`)

- [ ] `docker compose logs vivarium` shows `[entrypoint] PASEO_ENABLE=true — starting paseo daemon on 0.0.0.0:6767` and a QR code below it
- [ ] `PASEO_BIND_ADDR` in `.env` is set to this host's Tailscale IPv4 (`tailscale ip -4`) so the host port binds tailnet-only
- [ ] `nc -z <your-tailscale-ip> 6767` from another tailnet device succeeds
- [ ] Phone + Tailscale: install the paseo app, scan the QR from `docker compose logs vivarium`, daemon shows up under "Connected daemons"
- [ ] From the phone, start a Claude Code / opencode / codex session — agent commands run inside the vivarium container, not on the phone

## GitHub PAT verification (the critical one)

- [ ] Token generated as **fine-grained**, not classic
- [ ] Repository access: **only select repositories**, not "all repositories"
- [ ] Repository permissions — **Read only on**: Metadata and Contents; optionally Issues and Pull requests
- [ ] Repository permissions — **No access on** (critical): Administration, Secrets, Workflows, Actions, Deployments, Environments, Variables, Webhooks, Pages, Dependabot, Security advisories, everything else
- [ ] **Account permissions**: every single one set to **No access**
- [ ] Token summary screen shows only the selected `Read` lines (Metadata + Contents, optionally Issues and Pull requests) and nothing with `Write` or any account scope

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

## Spend caps (outside the container — vivarium can't verify these)

Pick the row that matches your provider:

### Subscription (Claude Pro/Max, Copilot, ChatGPT Plus/Pro) — default opencode path

- [ ] If using opencode: `opencode auth login <provider>` completed
- [ ] You know where to revoke if anything goes wrong (see PLAN.md §3)
- [ ] Rate limits are your spending cap — nothing else to do

### Pay-per-token API (Anthropic API direct, OpenRouter, OpenAI API, Gemini billed)

- [ ] Hard monthly cap set in the provider console
- [ ] The provider API key used from vivarium is dedicated (not shared with your laptop)
- [ ] First session tested with a small budget limit before raising

## Backups (on the host)

- [ ] Cron entries installed: `crontab -l` shows `backup.sh` and `audit.sh`
- [ ] Initial backup run: `bash ~/vivarium/scripts/backup.sh`
- [ ] `~/vivarium-backup/hourly-HH/` exists and contains a snapshot of `~/vivarium-home/work/` by default, minus backup excludes

## Container hardening verification

- [ ] Non-root user inside: `docker exec vivarium id` → `uid=<your host uid>`, not 0
- [ ] `no-new-privileges` active: `docker inspect vivarium | grep NoNewPrivileges` → `true`
- [ ] Caps dropped: `docker inspect vivarium -f '{{.HostConfig.CapDrop}}'` → `[ALL]`
- [ ] No `/var/run/docker.sock` mounted: `docker inspect vivarium | grep docker.sock` → nothing
- [ ] Resource limits applied: `docker stats vivarium` shows memory cap
- [ ] No SSH client: `docker exec vivarium bash -c 'command -v ssh'` returns empty/nonzero

## Full-loop drill

- [ ] Start a tmux session inside, launch the installed agent CLI (`opencode` by default), give it a tiny task, detach
- [ ] From your laptop, re-attach to the host and run `cd vivarium && ./scripts/shell.sh`, then `tmux a`
- [ ] Session resumes cleanly

When every box is checked, you're done.
