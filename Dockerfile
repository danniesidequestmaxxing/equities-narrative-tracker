FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Install deps first (better layer caching).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[prod]"

# Default process is the always-on worker; the API is a separate command
# (see docker-compose.yml / Procfile).
CMD ["python", "-m", "narrative_tracker.worker"]
