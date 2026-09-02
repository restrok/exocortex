FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml /app/
COPY src /app/src
COPY evals /app/evals

RUN pip install --no-cache-dir .

CMD ["brainctl", "--help"]
