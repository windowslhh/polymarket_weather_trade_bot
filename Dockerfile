FROM python:3.11-slim

WORKDIR /app

# System deps for building crypto libs
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir . && \
    apt-get purge -y gcc && apt-get autoremove -y

# Copy source code + scripts
COPY src/ src/
COPY scripts/ scripts/
COPY config.yaml .

# Data directory (mount as volume for persistence)
RUN mkdir -p data/history

EXPOSE 5001

CMD ["python", "-m", "src.main", "--paper", "-v"]
