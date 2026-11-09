FROM python:3.12-slim

ARG APP_UID=1001
ARG APP_GID=1001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid ${APP_GID} exocortex \
    && useradd --system --uid ${APP_UID} --gid exocortex --create-home --home-dir /home/exocortex exocortex \
    && mkdir -p /app /data/brain /sources/codex \
    && chown -R exocortex:exocortex /app /data/brain /sources/codex

WORKDIR /app

COPY --chown=exocortex:exocortex pyproject.toml /app/
COPY --chown=exocortex:exocortex src /app/src
COPY --chown=exocortex:exocortex evals /app/evals

RUN pip install --no-cache-dir .

USER exocortex

CMD ["brainctl", "--help"]
