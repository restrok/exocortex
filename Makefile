COMPOSE := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; else echo "docker-compose"; fi)
ENV_FILE ?= .env

.PHONY: bootstrap start up down logs doctor test lint quality quality-live

bootstrap:
	@test -f $(ENV_FILE) || cp .env.example $(ENV_FILE)
	@mkdir -p brain/Vault brain/Sources/Sanitized brain/Sources/Review brain/neo4j

start:
	./scripts/start.sh

up: bootstrap
	$(COMPOSE) --env-file $(ENV_FILE) up --build -d

down:
	$(COMPOSE) --env-file $(ENV_FILE) down

logs:
	$(COMPOSE) --env-file $(ENV_FILE) logs -f

doctor:
	$(COMPOSE) --env-file $(ENV_FILE) exec brain brainctl doctor

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/python -m ruff check src tests

quality:
	.venv/bin/python -m exocortex.evaluation --enforce-gates

quality-live:
	.venv/bin/python -m exocortex.evaluation --live --enforce-gates
