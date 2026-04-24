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

EXPOSE 5001

# FIX-16: container healthcheck — hit the Flask /api/status endpoint.
# start-period=60s gives the bot time to do its FIX-05 reconciler pass,
# historical distribution build, and first forecast pull before the
# first probe.  interval=30s, retries=3 means we wait ~3 min before the
# orchestrator (docker compose / k8s) marks the container unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5001/api/status || exit 1

CMD ["python", "-m", "src.main", "--paper", "-v"]
