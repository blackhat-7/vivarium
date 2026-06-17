# syntax=docker/dockerfile:1.6
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# system tools + language toolchains + node 24 in one apt pass.
# openssh-client is deliberately omitted: the read-only PAT is the structural
# push-blocker only over HTTPS; an SSH key the user (or a confused agent)
# drops in ~/.ssh would silently bypass it.
#
# BuildKit cache mounts keep the .deb downloads on the host between rebuilds,
# so a busted layer here re-runs apt but skips the ~150 MB network fetch.
# `rm -f .../docker-clean` disables Ubuntu's default post-install cache wipe
# so the cache mount actually retains the .debs. The mounts live outside
# the image, so nothing is added to the runtime layer.
#
# INSTALL_* and BESTIARY_REF args are declared just before the steps that
# use them, so changing them does NOT invalidate this expensive layer.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git gh \
      tmux vim nano less \
      build-essential pkg-config \
      ripgrep fd-find jq sqlite3 \
      gnupg2 pass \
      golang-go rustc cargo \
      python3 python3-pip python3-venv python-is-python3 \
      unzip xz-utils \
 && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
 && npm config set ignore-scripts true -g

# gh wrapper — use the same ephemeral GitHub HTTPS credential that
# scripts/git-auth.sh places in git's credential cache. This avoids a separate
# `gh auth login` token stored in ~/.config/gh/hosts.yml.
RUN <<'EOF'
cat > /usr/local/bin/gh <<'GH_WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${GH_TOKEN:-}" && -z "${GITHUB_TOKEN:-}" ]]; then
  credential="$(
    printf 'protocol=https\nhost=github.com\n\n' \
      | GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c credential.helper='cache --timeout=86400' credential fill 2>/dev/null \
      || true
  )"
  token=""
  while IFS= read -r line; do
    case "$line" in
      password=*) token="${line#password=}"; break ;;
    esac
  done <<< "$credential"
  if [[ -n "$token" ]]; then
    export GH_TOKEN="$token"
  fi
  unset credential token line
fi

exec /usr/bin/gh "$@"
GH_WRAPPER
chmod +x /usr/local/bin/gh
EOF

# uv — fast python package manager, system-wide
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/

# opencode — install iff INSTALL_OPENCODE=true, fail hard on error.
# (validation that at least one agent CLI is selected lives in scripts/up.sh
# so it doesn't bust the heavy apt layer.)
ARG INSTALL_OPENCODE=true
RUN if [ "$INSTALL_OPENCODE" = "true" ]; then \
      ( curl -fsSL https://opencode.ai/install | bash \
        && ( cp /root/.opencode/bin/opencode /usr/local/bin/opencode 2>/dev/null \
          || cp /root/.local/bin/opencode    /usr/local/bin/opencode 2>/dev/null \
          || cp "$(find /root -name opencode -type f -executable 2>/dev/null | head -1)" /usr/local/bin/opencode ) \
        && chmod +x /usr/local/bin/opencode \
        && opencode --version ) \
      || ( echo "[FATAL] opencode install failed. set INSTALL_OPENCODE=false in .env to skip." >&2 && exit 1 ); \
    else \
      echo "[skip] INSTALL_OPENCODE=false — skipping opencode" ; \
    fi

# claude code — install iff INSTALL_CLAUDE=true via npm, fail hard on error
ARG INSTALL_CLAUDE=false
RUN if [ "$INSTALL_CLAUDE" = "true" ]; then \
      ( npm install -g --ignore-scripts=false @anthropic-ai/claude-code \
        && claude --version ) \
      || ( echo "[FATAL] claude-code install failed. set INSTALL_CLAUDE=false in .env to skip." >&2 && exit 1 ); \
    else \
      echo "[skip] INSTALL_CLAUDE=false — skipping claude code" ; \
    fi

# paseo — install iff INSTALL_PASEO=true via npm, fail hard on error.
# Paseo is a multi-agent (claude/codex/opencode) daemon that pairs with
# desktop/mobile/web/CLI clients via QR code. When enabled at runtime
# (PASEO_ENABLE=true), the entrypoint launches `paseo daemon start
# --foreground --no-relay` instead of sleep infinity. Pairing keys
# persist under $HOME/.paseo (bind-mounted from the host).
ARG INSTALL_PASEO=false
RUN if [ "$INSTALL_PASEO" = "true" ]; then \
      ( npm install -g --ignore-scripts=false @getpaseo/cli \
        && paseo --version ) \
      || ( echo "[FATAL] paseo install failed. set INSTALL_PASEO=false in .env to skip." >&2 && exit 1 ); \
    else \
      echo "[skip] INSTALL_PASEO=false — skipping paseo" ; \
    fi

# bestiary — install iff INSTALL_BESTIARY=true into a system venv at
# /opt/bestiary so the non-root vivarium user can execute it via the
# /usr/local/bin/bestiary symlink. fail hard on error.
ARG INSTALL_BESTIARY=false
ARG BESTIARY_REF=main
RUN if [ "$INSTALL_BESTIARY" = "true" ]; then \
      ( uv venv --python 3.12 /opt/bestiary \
        && uv pip install --python /opt/bestiary/bin/python \
             "git+https://github.com/blackhat-7/bestiary.git@${BESTIARY_REF}" \
        && ln -s /opt/bestiary/bin/bestiary /usr/local/bin/bestiary \
        && bestiary list ) \
      || ( echo "[FATAL] bestiary install failed. set INSTALL_BESTIARY=false in .env to skip." >&2 && exit 1 ); \
    else \
      echo "[skip] INSTALL_BESTIARY=false — skipping bestiary" ; \
    fi

# Nix is the slowest setup step; keep it in its own cached layer so changing
# AI_HARNESSES_REF/MCP does not reinstall it.
COPY scripts/build-ai-harnesses.sh /usr/local/bin/build-ai-harnesses.sh
RUN chmod +x /usr/local/bin/build-ai-harnesses.sh \
 && /usr/local/bin/build-ai-harnesses.sh --ensure-nix

ARG TARGETARCH
ARG AI_HARNESSES_REF=main
ARG AI_HARNESSES_MCP=none
RUN /usr/local/bin/build-ai-harnesses.sh "$AI_HARNESSES_REF" "$AI_HARNESSES_MCP" \
 && printf '%s\n' '#!/bin/sh' 'exec "$HOME/.npm-global/bin/pi" "$@"' > /usr/local/bin/pi \
 && chmod +x /usr/local/bin/pi

# skeleton that gets copied to /home/vivarium on first run.
# $HOME/.local/bin is *appended* to PATH (not prepended): user-local
# installs (pip --user, uvx, pipx, cargo) resolve, but a planted
# ~/.local/bin/git cannot shadow /usr/bin/git for future shells.
RUN mkdir -p /opt/vivarium/skel \
 && printf '%s\n' \
      'export PATH="$PATH:$HOME/.local/bin:$HOME/.npm-global/bin"' \
      'alias ll="ls -la"' \
      'alias g=git' \
      'alias gs="git status"' \
      'export EDITOR=vim' \
      '[ -d ~/work ] && cd ~/work' \
      > /opt/vivarium/skel/.bashrc
COPY skel/AGENTS.md /opt/vivarium/skel/AGENTS.md

# non-root user — UID/GID overridden at build time to match host
ARG UID=1000
ARG GID=1000
RUN ( getent group ${GID} || groupadd -g ${GID} vivarium ) \
 && ( getent passwd ${UID} \
        || useradd -m -u ${UID} -g ${GID} -s /bin/bash vivarium ) \
 && if [ "$(id -un ${UID})" != "vivarium" ]; then \
      usermod -l vivarium "$(id -un ${UID})" \
      && usermod -d /home/vivarium -m vivarium \
      && groupmod -n vivarium "$(getent group ${GID} | cut -d: -f1)" ; \
    fi \
 && mkdir -p /home/vivarium \
 && chown -R ${UID}:${GID} /home/vivarium

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER vivarium
WORKDIR /home/vivarium

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
