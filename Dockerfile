# syntax=docker/dockerfile:1.6
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# system tools + node 20 in one apt pass.
# openssh-client is deliberately omitted: the read-only PAT is the structural
# push-blocker only over HTTPS; an SSH key the user (or a confused agent)
# drops in ~/.ssh would silently bypass it. PLAN.md §2 architecture box.
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
      ca-certificates curl wget git \
      tmux vim nano less \
      build-essential pkg-config \
      ripgrep fd-find jq sqlite3 \
      gnupg2 pass \
      python3 python3-pip python3-venv python-is-python3 \
      unzip xz-utils \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
 && npm config set ignore-scripts true -g

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

# skeleton that gets copied to /home/vivarium on first run.
# $HOME/.local/bin is *appended* to PATH (not prepended): user-local
# installs (pip --user, uvx, pipx, cargo) resolve, but a planted
# ~/.local/bin/git cannot shadow /usr/bin/git for future shells. See
# PLAN.md §6.7.
RUN mkdir -p /opt/vivarium/skel \
 && printf '%s\n' \
      'export PATH="$PATH:$HOME/.local/bin"' \
      'alias ll="ls -la"' \
      'alias g=git' \
      'alias gs="git status"' \
      'export EDITOR=vim' \
      '[ -d ~/work ] && cd ~/work' \
      > /opt/vivarium/skel/.bashrc

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
