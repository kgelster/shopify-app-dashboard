FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Deps first (cache layer), then source, so a code-only change skips uv sync.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Drop root. Nothing here writes to disk: settings come from the environment,
# the GA4 service-account key is held in memory, and all state is in Postgres.
RUN useradd --system --uid 10001 dashboard
USER dashboard

# HOST 0.0.0.0 so Fly's proxy can reach the server; port matches fly.toml internal_port.
EXPOSE 8080
CMD ["sh", "-c", "python -m partner_dashboard.migrate && uvicorn partner_dashboard.web:app --host 0.0.0.0 --port 8080"]
