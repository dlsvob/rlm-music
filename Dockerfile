# Multi-stage Dockerfile for rlm-music.
# Uses uv for fast dependency installation, then runs the FastAPI
# server via uvicorn on port 8080 (Cloud Run default).

FROM python:3.12-slim AS builder

# Install uv for fast package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies into the project venv
RUN uv sync --frozen --no-dev --no-install-project

# Copy the application code
COPY music/ music/

# Install the project itself
RUN uv sync --frozen --no-dev

# ── Runtime stage ──
FROM python:3.12-slim

WORKDIR /app

# Copy the entire venv and app from builder
COPY --from=builder /app /app

# Cloud Run sets PORT env var (defaults to 8080)
ENV PORT=8080

EXPOSE 8080

# Run the server using the venv's uvicorn
CMD ["/app/.venv/bin/uvicorn", "music.server.main:app", "--host", "0.0.0.0", "--port", "8080"]
