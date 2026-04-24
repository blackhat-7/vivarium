# vivarium

> *a place where living things are kept*

A carefully-fenced Hetzner VM where Claude Code and opencode run autonomously
on personal projects — reading freely across repos, writing locally, never
pushing to the cloud. The walls are made of five layers (VM, user, filesystem,
network, credentials); escape of any single layer does not compromise the
others.

This is **not** a sandbox that claims to contain a malicious model. It is an
enclosure sized to the realistic threat model: a well-intentioned agent that
may be tricked, loop into a bad state, install something poisoned, or
hallucinate destructive commands. The goal is that the worst-case incident is
"restore yesterday's snapshot and rotate a key," not "catastrophic loss."

## Layout

```
vivarium/
├── README.md              you are here
├── PLAN.md                the whole plan — threat model, architecture, setup, daily ops, secrets, audits
├── CHECKLIST.md           pre-flight verification — run through this before first autopilot session
├── configs/
│   ├── claude-settings.json    ~/.claude/settings.json on the VM
│   ├── opencode.json           ~/.config/opencode/opencode.json on the VM
│   └── gitignore-global        ~/.gitignore_global on the VM
└── scripts/
    ├── setup-root.sh           one-time, as root: user, packages, firewall
    ├── setup-user.sh           one-time, as `keeper`: claude/opencode install, git config
    ├── backup.sh               cron every 2h: snapshot ~/work to ~/backup
    └── audit.sh                cron monthly: drift check — PATs, ignore-scripts, deny rules, budget caps
```

## Quick start

1. Read `PLAN.md` top to bottom once. Do not skip the threat model section.
2. Provision the Hetzner VM (any size; 4GB RAM is plenty).
3. Copy this repo to the VM: `scp -r vivarium root@vm:/root/`
4. Run `scripts/setup-root.sh` as root.
5. `su - keeper`, run `scripts/setup-user.sh`.
6. Walk through `CHECKLIST.md`.
7. Start working.

## Why "vivarium"

A vivarium has glass walls (sandbox boundaries), a curated climate (allowlist),
observable specimens (agent sessions you can `tmux attach` to), regulated
feeding (budget caps), and a keeper who checks on it (you, via the monthly
audit). Every piece of this project maps to one of those.

The user that runs the agents is named `keeper` for the same reason.
