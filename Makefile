# SPDX-FileCopyrightText: 2026 aburow
# SPDX-License-Identifier: GPL-3.0-only

ENV_FILE ?= .env
COMPOSE_FILE ?= standalone/docker-compose.yml
SERVICE ?= ups-unified

.PHONY: dev-up dev-restart dev-logs dev-down dev-ps dev-build db-cap-dump db-cap-prime

APP_DIR ?= ups2mqtt/rootfs/usr/src/app
DB_PATH ?= standalone/data/ups2mqtt.db
CAP_SNAPSHOT ?= $(APP_DIR)/capabilities/capability_snapshot.sql
DB_PATH_ABS := $(if $(filter /%,$(DB_PATH)),$(DB_PATH),$(CURDIR)/$(DB_PATH))
CAP_SNAPSHOT_ABS := $(if $(filter /%,$(CAP_SNAPSHOT)),$(CAP_SNAPSHOT),$(CURDIR)/$(CAP_SNAPSHOT))

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

db-cap-dump:
	cd $(APP_DIR) && python3 -m ups2mqtt.db_snapshot dump --db $(DB_PATH_ABS) --out $(CAP_SNAPSHOT_ABS)

db-cap-prime:
	cd $(APP_DIR) && python3 -m ups2mqtt.db_snapshot prime --db $(DB_PATH_ABS) --in $(CAP_SNAPSHOT_ABS)
