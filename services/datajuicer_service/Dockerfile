FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/vendor/data-juicer:/app"

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

RUN groupadd --system datajuicer && useradd --system --gid datajuicer --home /app datajuicer \
    && mkdir -p /app /var/run/datajuicer /var/lib/datajuicer /data/textprocessor/global-deduplication \
    && chown -R datajuicer:datajuicer /app /var/run/datajuicer /var/lib/datajuicer /data/textprocessor

WORKDIR /app
COPY services/datajuicer_service/pyproject.toml services/datajuicer_service/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project

COPY --chown=datajuicer:datajuicer services/datajuicer_service/datajuicer_service ./datajuicer_service
COPY --chown=datajuicer:datajuicer services/datajuicer_service/migrations ./migrations
COPY --chown=datajuicer:datajuicer services/datajuicer_service/scripts ./scripts
COPY --chown=datajuicer:datajuicer services/datajuicer_service/alembic.ini ./alembic.ini
COPY --chown=datajuicer:datajuicer services/datajuicer_service/vendor ./vendor
RUN printf '%s\n' '7061da6ad06287aa0305eda162429b34361a56a3' > /app/vendor/data-juicer/.textprocessor-commit
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen

USER datajuicer
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 CMD ["python", "-m", "datajuicer_service.healthcheck"]
CMD ["python", "-m", "datajuicer_service.process_manager"]
