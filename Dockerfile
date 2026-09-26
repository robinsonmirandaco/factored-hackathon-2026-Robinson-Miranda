FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install --no-cache-dir uv \
    && useradd --create-home --uid 10001 appuser \
    && chown appuser /app

# Dependencies get their own layer, keyed only on pyproject.toml, so a code change reuses it
# instead of reinstalling about 2 GB of libraries. Test and lint tools are not installed.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY --chown=appuser README.md ./
COPY --chown=appuser src ./src
RUN uv pip install --system --no-cache --no-deps .

# The API writes the ingestion report under eval/reports, so app files belong to appuser.
COPY --chown=appuser config ./config
COPY --chown=appuser eval ./eval
RUN mkdir -p data/seed && chown -R appuser data
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
