# syntax=docker/dockerfile:1
#
# One image, two services (section 2). ECS overrides the command for the
# worker; the default runs the API.
#
# What is in it: runtime dependencies only, the 122KB GAT checkpoint, the seed
# CSVs, the embedding model, and the built UI, which the API serves. What is deliberately not: the training stack
# (see pyproject's train group), the 475MB CSV, and the 1.84GB graph cache,
# which the worker fetches from S3 on the way up.

FROM node:22-bookworm-slim AS ui

WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/index.html ui/vite.config.js ./
COPY ui/src ./src
RUN npm run build


FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# --no-default-groups drops the train group: xgboost, shap, tensorboard, pypdf
# and scikit-learn, and with shap the 143MB of numba and llvmlite it drags, and
# with xgboost the 216MB nvidia-nccl it drags. torch itself resolves from the
# CPU index on linux -- Fargate has no GPU (section 8).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-default-groups

# Bake the embedding model. Node 3 embeds a query on every case, so without
# this the first investigation of a fresh task stalls on a 1.1GB download --
# and the worker needs egress to huggingface.co, which makes the NAT Gateway
# mandatory rather than a choice (section 9).
ENV HF_HOME=/opt/hf
RUN /app/.venv/bin/python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('Qwen/Qwen3-Embedding-0.6B')" \
 && find /opt/hf -name '*.lock' -delete


# Debian 13, not 12. ECR's scan of a bookworm build found 4 critical and 15
# high CVEs, every one in a Debian package (perl, openssl, util-linux, zlib) and
# none in the app -- and apt-get upgrade changed nothing, because bookworm had
# no fixed versions to upgrade to. The builder moves with it so the venv is
# built against the same libc it runs on.
FROM python:3.12-slim-trixie AS runtime

# AML_ENV is not "local", so every AML_* variable must come from the task
# definition; a missing one fails the container on the way up rather than one
# case at a time (config.py). HF_HUB_OFFLINE makes a missing baked model an
# error here instead of a silent download in front of an interviewer.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    AML_ENV=aws

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/hf /opt/hf

COPY config.py db.py artifacts.py ./
COPY agent ./agent
COPY api ./api
COPY ml ./ml
COPY rag ./rag
COPY scripts ./scripts
COPY artifacts/model-HI-Small_Trans.pt ./artifacts/
COPY data/seed ./data/seed
COPY --from=ui /ui/dist ./ui/dist

# The worker downloads the graph here, so it has to be writable by the run user.
RUN useradd --create-home --uid 10001 aml \
 && mkdir -p /app/data \
 && chown -R aml:aml /app/data
USER aml

EXPOSE 8000

# api:    the default below
# worker: command override ["arq", "api.worker.WorkerSettings"]
# seed:   one-shot, ["python", "-m", "scripts.load_seed"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
