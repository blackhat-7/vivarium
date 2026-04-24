FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# system tools — everything the agent is likely to reach for
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git openssh-client \
      tmux vim nano less \
      build-essential pkg-config \
      ripgrep fd-find jq sqlite3 \
      gnupg2 pass \
      python3 python3-pip python3-venv python-is-python3 \
      unzip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd

# node 20 via nodesource (keeps npm current)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set ignore-scripts true -g

# uv — fast python package manager, system-wide
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/

# opencode — primary agent for subscription-based use
RUN curl -fsSL https://opencode.ai/install | bash \
    && ( cp /root/.opencode/bin/opencode /usr/local/bin/opencode 2>/dev/null \
      || cp /root/.local/bin/opencode    /usr/local/bin/opencode 2>/dev/null \
      || cp "$(find /root -name opencode -type f -executable 2>/dev/null | head -1)" /usr/local/bin/opencode ) \
    && chmod +x /usr/local/bin/opencode \
    && opencode --version

# claude code — optional secondary agent for API-key sessions
RUN ( curl -fsSL https://code.claude.com/install.sh | bash \
    && ( cp /root/.local/bin/claude /usr/local/bin/claude 2>/dev/null \
      || cp "$(find /root -name claude -type f -executable 2>/dev/null | head -1)" /usr/local/bin/claude ) \
    && chmod +x /usr/local/bin/claude \
    && claude --version ) || echo "[dockerfile] claude code install failed — optional, continuing"

# skeleton that gets copied to /home/vivarium on first run
RUN mkdir -p /opt/vivarium/skel \
 && printf '%s\n' \
      'export PATH="$HOME/.local/bin:$PATH"' \
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
