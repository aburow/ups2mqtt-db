# SPDX-FileCopyrightText: 2026 aburow
# SPDX-License-Identifier: GPL-3.0-only

ENV_FILE ?= .env
COMPOSE_FILE ?= standalone/docker-compose.yml
SERVICE ?= ups-unified

.PHONY: dev-up dev-restart dev-logs dev-down dev-ps dev-build

dev-up:
	DOCKER_BUILDKIT=0 docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) up -d --build

dev-build:
	DOCKER_BUILDKIT=0 docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) build

dev-restart:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) restart $(SERVICE)

dev-logs:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) logs -f $(SERVICE)

dev-down:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) down

dev-ps:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) ps
