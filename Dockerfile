FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PATH="/app/.venv/bin:$PATH"
WORKDIR /app
# uv is pinned to the version that wrote uv.lock.
RUN pip install --no-cache-dir uv==0.11.31 \
    && useradd --create-home --uid 10001 appuser \
    && chown appuser /app

# Dependencies get their own layer, keyed only on the lockfile, so a code change reuses it.
# Test and lint tools (the dev group) are not installed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY --chown=appuser README.md ./
COPY --chown=appuser src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache

# The API writes the ingestion report under eval/reports, so app files belong to appuser.
COPY --chown=appuser db ./db
COPY --chown=appuser config ./config
COPY --chown=appuser eval ./eval
RUN mkdir -p data/seed && chown -R appuser data
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
