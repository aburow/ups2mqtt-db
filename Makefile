# SPDX-FileCopyrightText: 2026 aburow
# SPDX-License-Identifier: GPL-3.0-only

ENV_FILE ?= .env
COMPOSE_FILE ?= standalone/docker-compose.yml
SERVICE ?= ups2mqtt
DOCKER_BUILDKIT ?= 1
COMPOSE_DOCKER_CLI_BUILD ?= 1
BUILDKIT_PROGRESS ?= auto

.PHONY: build dev-up dev-up-direct dev-restart dev-logs dev-logs-direct dev-down dev-ps dev-build db-cap-dump db-cap-prime proxy-hash-password proxy-set-password dev-lock dev-unlock runtime-sync runtime-check release-check git-commit-template git-push-template

APP_DIR ?= ups2mqtt/rootfs/usr/src/app
DB_PATH ?= standalone/data/ups2mqtt.db
DB_PATH_CONTAINER ?= /data/ups2mqtt.db
CAP_SNAPSHOT ?= $(APP_DIR)/capabilities/capability_snapshot.sql
DB_PATH_ABS := $(if $(filter /%,$(DB_PATH)),$(DB_PATH),$(CURDIR)/$(DB_PATH))
CAP_SNAPSHOT_ABS := $(if $(filter /%,$(CAP_SNAPSHOT)),$(CAP_SNAPSHOT),$(CURDIR)/$(CAP_SNAPSHOT))

build: dev-build

dev-up:
	DOCKER_BUILDKIT=$(DOCKER_BUILDKIT) COMPOSE_DOCKER_CLI_BUILD=$(COMPOSE_DOCKER_CLI_BUILD) BUILDKIT_PROGRESS=$(BUILDKIT_PROGRESS) docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) up -d --build

dev-up-direct:
	@echo "Starting dual debug mode: direct HTTP on http://$${UPS2MQTT_WEB_BIND:-0.0.0.0}:$${UPS2MQTT_WEB_PORT:-8099}/ and proxied HTTPS on :$${UPS2MQTT_PROXY_HTTPS_PORT:-8443}"
	DOCKER_BUILDKIT=$(DOCKER_BUILDKIT) COMPOSE_DOCKER_CLI_BUILD=$(COMPOSE_DOCKER_CLI_BUILD) BUILDKIT_PROGRESS=$(BUILDKIT_PROGRESS) UPS2MQTT_WEB_BIND=$${UPS2MQTT_WEB_BIND:-0.0.0.0} docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) up -d --build $(SERVICE) caddy

dev-build:
	DOCKER_BUILDKIT=$(DOCKER_BUILDKIT) COMPOSE_DOCKER_CLI_BUILD=$(COMPOSE_DOCKER_CLI_BUILD) BUILDKIT_PROGRESS=$(BUILDKIT_PROGRESS) docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) build

dev-restart:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) restart $(SERVICE)

dev-logs:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) logs -f $(SERVICE)

dev-logs-direct:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) logs -f $(SERVICE)

dev-down:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) down

dev-ps:
	docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE) ps

db-cap-dump:
	cd $(APP_DIR) && python3 -m ups2mqtt.db_snapshot dump --db $(DB_PATH_ABS) --out $(CAP_SNAPSHOT_ABS)

db-cap-prime:
	cd $(APP_DIR) && python3 -m ups2mqtt.db_snapshot prime --db $(DB_PATH_ABS) --in $(CAP_SNAPSHOT_ABS)

proxy-hash-password:
	@if [ -z "$(PASSWORD)" ]; then \
		echo "Usage: make proxy-hash-password PASSWORD='your-new-password'"; \
		exit 1; \
	fi
	@docker run --rm caddy:2-alpine caddy hash-password --plaintext "$(PASSWORD)"

proxy-set-password:
	@if [ -z "$(PASSWORD)" ]; then \
		echo "Usage: make proxy-set-password PASSWORD='your-new-password'"; \
		exit 1; \
	fi
	@if [ ! -f "$(ENV_FILE)" ]; then \
		echo "Missing env file: $(ENV_FILE)"; \
		exit 1; \
	fi
	@set -eu; \
	HASH="$$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$(PASSWORD)")"; \
	ESCAPED_HASH="$$(printf '%s\n' "$$HASH" | sed 's/[$$]/&&/g')"; \
	TMP_FILE="$$(mktemp)"; \
	if rg -q '^UPS2MQTT_PROXY_PASSWORD_HASH=' "$(ENV_FILE)"; then \
		sed "s#^UPS2MQTT_PROXY_PASSWORD_HASH=.*#UPS2MQTT_PROXY_PASSWORD_HASH=$$ESCAPED_HASH#" "$(ENV_FILE)" > "$$TMP_FILE"; \
	else \
		cat "$(ENV_FILE)" > "$$TMP_FILE"; \
		printf '\nUPS2MQTT_PROXY_PASSWORD_HASH=%s\n' "$$ESCAPED_HASH" >> "$$TMP_FILE"; \
	fi; \
	mv "$$TMP_FILE" "$(ENV_FILE)"; \
	docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" up -d --no-deps --force-recreate caddy >/dev/null; \
	echo "Updated UPS2MQTT_PROXY_PASSWORD_HASH in $(ENV_FILE) and recreated caddy."

dev-lock:
	@set -eu; \
	if [ -f "$(DB_PATH_ABS)" ] && [ -w "$(DB_PATH_ABS)" ]; then \
		if python3 -c "import sqlite3; db='$(DB_PATH_ABS)'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"UPDATE profiles SET is_protected = 1 WHERE lower(name) LIKE '%[default]%' AND is_protected != 1\"); changed=cur.rowcount; conn.commit(); conn.close(); print(f'Locked {changed} profile(s) matching [default] in {db}')"; then \
			exit 0; \
		fi; \
	fi; \
	if [ -n "$$(docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" ps -q "$(SERVICE)")" ]; then \
		if docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" exec -T "$(SERVICE)" python3 -c "import sqlite3; db='$(DB_PATH_CONTAINER)'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"UPDATE profiles SET is_protected = 1 WHERE lower(name) LIKE '%[default]%' AND is_protected != 1\"); changed=cur.rowcount; conn.commit(); conn.close(); print(f'Locked {changed} profile(s) matching [default] in {db}')"; then \
			exit 0; \
		fi; \
	fi; \
	docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" run --rm --no-deps "$(SERVICE)" python3 -c "import sqlite3; db='$(DB_PATH_CONTAINER)'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"UPDATE profiles SET is_protected = 1 WHERE lower(name) LIKE '%[default]%' AND is_protected != 1\"); changed=cur.rowcount; conn.commit(); conn.close(); print(f'Locked {changed} profile(s) matching [default] in {db}')"

dev-unlock:
	@set -eu; \
	if [ -f "$(DB_PATH_ABS)" ] && [ -w "$(DB_PATH_ABS)" ]; then \
		if python3 -c "import sqlite3; db='$(DB_PATH_ABS)'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"UPDATE profiles SET is_protected = 0 WHERE lower(name) LIKE '%[default]%' AND is_protected != 0\"); changed=cur.rowcount; conn.commit(); conn.close(); print(f'Unlocked {changed} profile(s) matching [default] in {db}')"; then \
			exit 0; \
		fi; \
	fi; \
	if [ -n "$$(docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" ps -q "$(SERVICE)")" ]; then \
		if docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" exec -T "$(SERVICE)" python3 -c "import sqlite3; db='$(DB_PATH_CONTAINER)'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"UPDATE profiles SET is_protected = 0 WHERE lower(name) LIKE '%[default]%' AND is_protected != 0\"); changed=cur.rowcount; conn.commit(); conn.close(); print(f'Unlocked {changed} profile(s) matching [default] in {db}')"; then \
			exit 0; \
		fi; \
	fi; \
	docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" run --rm --no-deps "$(SERVICE)" python3 -c "import sqlite3; db='$(DB_PATH_CONTAINER)'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"UPDATE profiles SET is_protected = 0 WHERE lower(name) LIKE '%[default]%' AND is_protected != 0\"); changed=cur.rowcount; conn.commit(); conn.close(); print(f'Unlocked {changed} profile(s) matching [default] in {db}')"

runtime-sync:
	./scripts/sync_runtime_tree.sh

runtime-check:
	./scripts/check_runtime_tree_sync.sh

release-check: runtime-check
	@echo "Release checks passed"

git-commit-template:
	@echo ""
	@echo "# Current git status:"
	@echo ""
	@git status --short
	@echo ""
	@echo "# Git add and commit commands (copy/paste and modify as needed):"
	@echo ""
	@if git status --short | grep -q .; then \
		echo "# Add individual files:"; \
		git status --short | awk '{print "git add " $$NF}'; \
		echo ""; \
		echo "# Or add all files:"; \
		echo "git add -A"; \
		echo ""; \
		echo "# Then commit:"; \
		echo "git commit -m \"<your commit message here>\""; \
	else \
		echo "# No changes detected"; \
	fi
	@echo ""

git-push-template:
	@echo ""
	@echo "# Git push commands (copy/paste and modify as needed):"
	@echo ""
	@BRANCH=$$(git branch --show-current); \
	VERSION=$$(grep '^version:' homeassistant-addon/ups2mqtt/config.yaml | awk '{print $$2}' | tr -d '"'); \
	echo "# Current branch: $$BRANCH"; \
	echo "# Current version in config.yaml: $$VERSION"; \
	echo ""; \
	echo "# Check current branch and remote status"; \
	echo "git status"; \
	echo "git branch -vv"; \
	echo ""; \
	echo "# Push to remote (regular push)"; \
	echo "git push"; \
	echo ""; \
	echo "# Push and set upstream for new branch"; \
	echo "git push -u origin $$BRANCH"; \
	echo ""; \
	echo "# Push with force (use with caution!)"; \
	echo "# git push --force-with-lease"; \
	echo ""; \
	echo "# ===== PUBLISH FOR HOME ASSISTANT ====="; \
	echo ""; \
	echo "# Create and push a version tag (HA will detect this):"; \
	echo "git tag -a v$$VERSION -m \"Release v$$VERSION\""; \
	echo "git push origin v$$VERSION"; \
	echo ""; \
	echo "# Or push all tags:"; \
	echo "# git push --tags"; \
	echo ""
