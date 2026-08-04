FROM nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/classification-service/.venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3.12 \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/classification-service
COPY services/classification_service/pyproject.toml services/classification_service/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY services/classification_service/classification_service ./classification_service
RUN uv sync --locked --no-dev

USER 65532:65532
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "classification_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
