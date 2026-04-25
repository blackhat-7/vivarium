# vivarium

> *a place where living things are kept*

A Docker-based enclosure for running **opencode** and **claude-code**
autonomously on a Linux host — reading freely across repos, writing locally,
never pushing to the cloud. Three layers of isolation: host, container, and a
read-only GitHub PAT. No in-app sandbox configs to maintain, no iptables
rules on the host, no dedicated VM required.

The agent runs as a non-root user inside a locked-down container. Your code
lives in a bind-mounted directory on the host (`~/vivarium-work/`). A
fine-grained read-only PAT is the structural reason nothing ever pushes. If a
session goes sideways, `docker compose restart` is a clean reset.

## Layout

```
vivarium/
├── README.md               you are here
├── PLAN.md                 threat model, architecture, setup, secrets, audits
├── CHECKLIST.md            pre-flight + post-setup verification
├── Dockerfile              the image (ubuntu 24.04 + opencode + claude + uv + node + git)
├── compose.yaml            runtime: mounts, user, caps, limits, restart policy
├── entrypoint.sh           bootstraps the user home on first run
├── .env.example            template for HOST_UID/HOST_GID
└── scripts/
    ├── up.sh               build image + start container
    ├── update.sh           pull latest code from origin and rebuild (preserves ~/vivarium-home)
    ├── shell.sh            drop into the container
    ├── remove.sh           tear down — container, image, cron; optional flags nuke data/backups/repo
    ├── cron-install.sh     add backup-every-2h + audit-monthly entries to your crontab
    ├── cron-uninstall.sh   remove them
    ├── backup.sh           host-side rsync of the work dir (cron'd every 2h)
    └── audit.sh            host-side monthly drift check
```

## Quick start

```bash
git clone https://github.com/blackhat-7/vivarium.git ~/vivarium
cd ~/vivarium
./scripts/up.sh              # first time: ~3 min to build
./scripts/shell.sh           # you are now in /home/vivarium/work inside the container

# inside the container, one-time:
opencode auth login          # pick provider, complete the OAuth flow
git clone https://github.com/YOU/some-repo.git   # paste read-only PAT
```

After that, daily life is `./scripts/shell.sh` → `cd some-project` → `opencode`.

## For agents working on this repo

See `AGENTS.md`. Red lines are non-negotiable.

## Why Docker (not a dedicated VM)

The original plan assumed a fresh dedicated Hetzner VM. On a VM that's
already running Docker + Tailscale + Nix + a personal account, adding
host-level iptables rules and a second unprivileged OS user risks conflicting
with the host's existing setup — and dilutes the "isolated sandbox user"
concept since you share an OS with Tailscale, Docker, etc.

The container approach gives you equivalent (or better) isolation for
realistic threats, trivial reset, and far less host pollution. See `PLAN.md`
§2 for the full layer comparison.
