FROM python:3.11-slim

WORKDIR /app

# System deps for building crypto libs + runtime `curl` for HEALTHCHECK.
# Purge gcc/libffi-dev after the Python deps are installed but keep curl —
# Alpine/slim images don't ship curl by default and HEALTHCHECK needs it.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir . && \
    apt-get purge -y gcc libffi-dev && apt-get autoremove -y

# Copy source code + scripts
COPY src/ src/
COPY scripts/ scripts/
COPY config.yaml .

# Data directory (mount as volume for persistence)
RUN mkdir -p data/history

# FIX-M5: drop root in the runtime container.  Creates a dedicated `bot`
# user with uid 1000 (stable for bind-mounted volume ownership) and
# chowns the app tree + data dir before switching.  Removes the
# attacker's easy escalation path if any of the Python deps get
# compromised upstream.
RUN useradd --uid 1000 --create-home --shell /bin/bash bot \
    && chown -R bot:bot /app
USER bot

EXPOSE 5001

# FIX-16: container healthcheck — hit the Flask /api/status endpoint.
# Review 🟡 #2 (2026-04-25): start-period=120s.  The 60s budget was too
# tight: backfilling 7 cities × 3 forecast days against Open-Meteo
# routinely takes 70–90s before the web server even starts serving.
# The orchestrator was marking the container unhealthy mid-init,
# triggering a restart loop on the first deploy.  120s leaves headroom
# for the slowest observed cold start.
#
# Review 🟡 #3 (2026-04-25): the orchestrator only RECORDS health; it
# does not auto-restart unhealthy containers.  Pair this with one of:
#   - watchtower (`com.centurylinklabs.watchtower.healthcheck=true`)
#   - autoheal sidecar (https://github.com/willfarrell/docker-autoheal)
#   - external Prometheus blackbox + alertmanager
# See docs/runbook/go_live_runbook.md "Health monitoring" section.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:5001/api/status || exit 1

CMD ["python", "-m", "src.main", "--paper", "-v"]
