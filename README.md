# vivarium

> *a place where living things are kept*

A Docker-based enclosure for running **opencode** and **claude-code**
autonomously on a Linux host — reading freely across repos, writing locally,
and relying on a correctly configured fine-grained read-only GitHub PAT so
HTTPS pushes fail. Three layers of isolation: host, container, and PAT. No
in-app sandbox configs to maintain, no iptables rules on the host, no dedicated
VM required.

Run setup as your normal non-root host user, not with `sudo`; the container
user is built from that UID/GID. Your code lives in a bind-mounted directory
on the host (`~/vivarium-home/work/` by default). A fine-grained read-only PAT
is the structural reason HTTPS pushes using that PAT fail. If a session goes
sideways, `docker compose restart` restarts the container.

## Layout

```
vivarium/
├── README.md               you are here
├── DOCS.md                 implementation-accurate feature docs
├── PLAN.md                 threat model, architecture, setup, secrets, audits
├── CHECKLIST.md            pre-flight + post-setup verification
├── Dockerfile              the image (ubuntu 24.04 + optional agents + uv + node + git)
├── compose.yaml            runtime: mounts, user, caps, limits, restart policy
├── entrypoint.sh           bootstraps the user home on first run
├── .env.example            template for UID/GID, install flags, and bestiary config
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

# inside the container, one-time if using the default opencode install:
opencode auth login          # pick provider, complete the OAuth flow
git clone https://github.com/YOU/some-repo.git   # paste read-only PAT
```

After that, daily life is `./scripts/shell.sh` → `cd some-project` → run the installed agent CLI (`opencode` by default; `claude` if enabled).

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
