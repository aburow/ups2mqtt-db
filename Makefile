# SPDX-FileCopyrightText: 2026 aburow
# SPDX-License-Identifier: GPL-3.0-only

ENV_FILE ?= .env
COMPOSE_FILE ?= standalone/docker-compose.yml
SERVICE ?= ups2mqtt
DOCKER_BUILDKIT ?= 1
COMPOSE_DOCKER_CLI_BUILD ?= 1
BUILDKIT_PROGRESS ?= auto
HA_TEST_COMPOSE ?= docker-compose.ha-test.yml
HA_TEST_PROJECT ?= ups2mqtt-ha-test
HA_TEST_ENV ?= .env.ha-test
PRE_RELEASE_ENV ?= ./.env.pre-release
PRE_RELEASE_SNAPSHOT ?= pre-release-$(shell git rev-parse --short HEAD 2>/dev/null || echo manual)
PRE_RELEASE_SSH_CMD ?= ssh
PRE_RELEASE_LOCAL_ADDON_SRC ?= homeassistant-addon/ups2mqtt
PRE_RELEASE_LOCAL_ADDON_ROOT ?= /addons/local
PRE_RELEASE_LOCAL_ADDON_DIR ?= ups2mqtt
PROXMOX_API_TIMEOUT ?= 20
HAOS_SSH_TIMEOUT ?= 15
PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES ?= 10
PRE_RELEASE_SNAPSHOT_VERIFY_DELAY ?= 2
PRE_RELEASE_ROLLBACK_VERIFY_RETRIES ?= 30
PRE_RELEASE_ROLLBACK_VERIFY_DELAY ?= 5
PRE_RELEASE_HAOS_READY_RETRIES ?= 30
PRE_RELEASE_HAOS_READY_DELAY ?= 5

.PHONY: build dev-up dev-up-direct dev-restart dev-logs dev-logs-direct dev-down dev-ps dev-build ha-test-start ha-test-stop ha-test-rebuild ha-test-logs ha-test-status ha-test-clean pre-release-preflight pre-release-snapshot pre-release-snapshot-list pre-release-haos-smoke pre-release-rollback pre-release-snapshot-delete pre-release-cycle pre-release-run pre-release-local-addon-sync db-cap-dump db-cap-prime proxy-hash-password proxy-set-password dev-lock dev-unlock runtime-sync runtime-check release-check git-commit-template git-push-template bump-version

APP_DIR ?= ups2mqtt/rootfs/usr/src/app
DB_PATH ?= standalone/data/ups2mqtt.db
DB_PATH_CONTAINER ?= /data/ups2mqtt.db
CAP_SNAPSHOT ?= $(APP_DIR)/capabilities/capability_snapshot.sql
DB_PATH_ABS := $(if $(filter /%,$(DB_PATH)),$(DB_PATH),$(CURDIR)/$(DB_PATH))
CAP_SNAPSHOT_ABS := $(if $(filter /%,$(CAP_SNAPSHOT)),$(CAP_SNAPSHOT),$(CURDIR)/$(CAP_SNAPSHOT))
HA_TEST_COMPOSE_CMD = docker compose --env-file $(HA_TEST_ENV) -p $(HA_TEST_PROJECT) -f $(HA_TEST_COMPOSE)

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

ha-test-start:
	$(HA_TEST_COMPOSE_CMD) up -d

ha-test-stop:
	$(HA_TEST_COMPOSE_CMD) stop

ha-test-rebuild:
	$(HA_TEST_COMPOSE_CMD) up -d --build --force-recreate

ha-test-logs:
	$(HA_TEST_COMPOSE_CMD) logs -f

ha-test-status:
	$(HA_TEST_COMPOSE_CMD) ps
	@echo "Home Assistant: http://localhost:$${HA_TEST_HTTP_PORT:-8123}"
	@echo "MQTT broker: localhost:$${HA_TEST_MQTT_PORT:-1883}"
	@echo "Capability: partial Home Assistant Container + MQTT test environment; no Supervisor or Add-on Store."

ha-test-clean:
	$(HA_TEST_COMPOSE_CMD) down -v --remove-orphans

pre-release-preflight:
	@set -eu; \
	test -f "$(PRE_RELEASE_ENV)" || { echo "Missing $(PRE_RELEASE_ENV)"; exit 1; }; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	for v in PROXMOX_API_URL PROXMOX_TOKEN_ID PROXMOX_TOKEN_SECRET PROXMOX_NODE PROXMOX_VM_ID HAOS_HOST HAOS_SSH_USER HAOS_SSH_PORT HAOS_SSH_KEY_PATH; do \
		eval "val=\$${$$v-}"; \
		if [ -z "$$val" ]; then \
			echo "Missing required variable: $$v"; \
			exit 1; \
		fi; \
	done; \
	for setting in \
		PROXMOX_API_TIMEOUT="$(PROXMOX_API_TIMEOUT)" \
		HAOS_SSH_TIMEOUT="$(HAOS_SSH_TIMEOUT)" \
		PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES="$(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES)" \
		PRE_RELEASE_SNAPSHOT_VERIFY_DELAY="$(PRE_RELEASE_SNAPSHOT_VERIFY_DELAY)" \
		PRE_RELEASE_ROLLBACK_VERIFY_RETRIES="$(PRE_RELEASE_ROLLBACK_VERIFY_RETRIES)" \
		PRE_RELEASE_ROLLBACK_VERIFY_DELAY="$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)" \
		PRE_RELEASE_HAOS_READY_RETRIES="$(PRE_RELEASE_HAOS_READY_RETRIES)" \
		PRE_RELEASE_HAOS_READY_DELAY="$(PRE_RELEASE_HAOS_READY_DELAY)"; do \
		name="$${setting%%=*}"; \
		val="$${setting#*=}"; \
		case "$$val" in \
			""|*[!0-9]*) \
				echo "Invalid required Make variable: $$name must be a positive integer, got '$$val'"; \
				exit 1; \
				;; \
		esac; \
		if [ "$$val" -le 0 ]; then \
			echo "Invalid required Make variable: $$name must be a positive integer, got '$$val'"; \
			exit 1; \
		fi; \
	done; \
	command -v curl >/dev/null || { echo "curl is required"; exit 1; }; \
	set -- $${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)}; \
	command -v "$$1" >/dev/null || { echo "$$1 is required"; exit 1; }; \
	echo "preflight: ok"

pre-release-snapshot: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	insecure_flag=""; \
	if [ "$${PROXMOX_TLS_INSECURE:-false}" = "true" ]; then insecure_flag="-k"; fi; \
	curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		-X POST \
		--data-urlencode "snapname=$(PRE_RELEASE_SNAPSHOT)" \
		--data-urlencode "description=ups2mqtt pre-release snapshot" \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot" >/dev/null; \
	verified=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES)" ]; do \
		snapshot_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot")"; \
		if printf '%s\n' "$$snapshot_json" | grep -F "\"name\":\"$(PRE_RELEASE_SNAPSHOT)\"" >/dev/null; then \
			verified=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_SNAPSHOT_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$verified -ne 1 ]; then \
		echo "snapshot verification failed: $(PRE_RELEASE_SNAPSHOT) not present after $(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES) attempts"; \
		exit 1; \
	fi; \
	snapshot_unlocked=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES)" ]; do \
		config_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/config")"; \
		if ! printf '%s\n' "$$config_json" | grep -E '"lock"[[:space:]]*:' >/dev/null; then \
			snapshot_unlocked=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_SNAPSHOT_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$snapshot_unlocked -ne 1 ]; then \
		echo "snapshot lock check failed: VM still locked after snapshot creation"; \
		exit 1; \
	fi; \
	echo "snapshot created and verified: $(PRE_RELEASE_SNAPSHOT)"

pre-release-snapshot-list: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	insecure_flag=""; \
	if [ "$${PROXMOX_TLS_INSECURE:-false}" = "true" ]; then insecure_flag="-k"; fi; \
	curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot"

pre-release-haos-smoke: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	$${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
		-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
		"$${HAOS_SSH_USER}@$${HAOS_HOST}" \
		'ha core info >/dev/null && ha supervisor info >/dev/null && echo "haos smoke: ok"'

pre-release-local-addon-sync: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	SRC="$(PRE_RELEASE_LOCAL_ADDON_SRC)"; \
	REMOTE_DIR="$(PRE_RELEASE_LOCAL_ADDON_ROOT)/$(PRE_RELEASE_LOCAL_ADDON_DIR)"; \
	test -d "$$SRC" || { echo "missing local add-on source dir: $$SRC"; exit 1; }; \
	test -f "$$SRC/config.yaml" || { echo "missing add-on config.yaml at $$SRC/config.yaml"; exit 1; }; \
	$${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
		-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
		"$${HAOS_SSH_USER}@$${HAOS_HOST}" "rm -rf \"$$REMOTE_DIR\" && mkdir -p \"$$REMOTE_DIR\""; \
	tar -C "$$SRC" -cf - . | \
		$${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
			-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
			"$${HAOS_SSH_USER}@$${HAOS_HOST}" "tar -xf - -C \"$$REMOTE_DIR\""; \
	$${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
		-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
		"$${HAOS_SSH_USER}@$${HAOS_HOST}" "test -f \"$$REMOTE_DIR/config.yaml\" && sed -i '/^image:[[:space:]]*/d' \"$$REMOTE_DIR/config.yaml\" && test -f \"$$REMOTE_DIR/config.yaml\" && ! grep -Eq '^image:[[:space:]]*' \"$$REMOTE_DIR/config.yaml\""; \
	$${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
		-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
		"$${HAOS_SSH_USER}@$${HAOS_HOST}" "test -f \"$$REMOTE_DIR/Dockerfile\" && echo \"local add-on synced for local build validation: $$REMOTE_DIR\""

pre-release-rollback: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	insecure_flag=""; \
	if [ "$${PROXMOX_TLS_INSECURE:-false}" = "true" ]; then insecure_flag="-k"; fi; \
	snapshot_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot")"; \
	if ! printf '%s\n' "$$snapshot_json" | grep -F "\"name\":\"$(PRE_RELEASE_SNAPSHOT)\"" >/dev/null; then \
		echo "rollback snapshot missing: $(PRE_RELEASE_SNAPSHOT)"; \
		exit 1; \
	fi; \
	curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		-X POST \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot/$(PRE_RELEASE_SNAPSHOT)/rollback" >/dev/null; \
	rollback_unlocked=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_ROLLBACK_VERIFY_RETRIES)" ]; do \
		config_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/config")"; \
		if ! printf '%s\n' "$$config_json" | grep -E '"lock"[[:space:]]*:' >/dev/null; then \
			rollback_unlocked=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$rollback_unlocked -ne 1 ]; then \
		echo "rollback lock check failed: VM still locked after rollback"; \
		exit 1; \
	fi; \
	running=0; \
	running_seen=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_ROLLBACK_VERIFY_RETRIES)" ]; do \
		status_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/status/current")"; \
		if printf '%s\n' "$$status_json" | grep -F '"status":"running"' >/dev/null; then \
			if [ $$running_seen -eq 1 ]; then \
				running=1; \
				break; \
			fi; \
			running_seen=1; \
			sleep "$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)"; \
			i=$$((i + 1)); \
			continue; \
		fi; \
		running_seen=0; \
		curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			-X POST \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/status/start" >/dev/null 2>&1 || true; \
		sleep "$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$running -ne 1 ]; then \
		echo "rollback VM status check failed: VM not running after rollback"; \
		exit 1; \
	fi; \
	ssh_ready=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_HAOS_READY_RETRIES)" ]; do \
		if $${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
			-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
			"$${HAOS_SSH_USER}@$${HAOS_HOST}" 'true' >/dev/null 2>&1; then \
			ssh_ready=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_HAOS_READY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$ssh_ready -ne 1 ]; then \
		echo "rollback SSH readiness failed: HAOS SSH unreachable after rollback"; \
		exit 1; \
	fi; \
	ha_ready=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_HAOS_READY_RETRIES)" ]; do \
		if $${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
			-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
			"$${HAOS_SSH_USER}@$${HAOS_HOST}" \
			'ha core info >/dev/null && ha supervisor info >/dev/null' >/dev/null 2>&1; then \
			ha_ready=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_HAOS_READY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$ha_ready -ne 1 ]; then \
		echo "rollback HAOS readiness failed: ha core/supervisor not ready"; \
		exit 1; \
	fi; \
	echo "rollback executed and verified: $(PRE_RELEASE_SNAPSHOT)"

pre-release-snapshot-delete: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	insecure_flag=""; \
	if [ "$${PROXMOX_TLS_INSECURE:-false}" = "true" ]; then insecure_flag="-k"; fi; \
	snapshot_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot")"; \
	if ! printf '%s\n' "$$snapshot_json" | grep -F "\"name\":\"$(PRE_RELEASE_SNAPSHOT)\"" >/dev/null; then \
		echo "snapshot already absent: $(PRE_RELEASE_SNAPSHOT)"; \
		exit 0; \
	fi; \
	curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		-X DELETE \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot/$(PRE_RELEASE_SNAPSHOT)" >/dev/null; \
	deleted=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES)" ]; do \
		snapshot_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot")"; \
		if ! printf '%s\n' "$$snapshot_json" | grep -F "\"name\":\"$(PRE_RELEASE_SNAPSHOT)\"" >/dev/null; then \
			deleted=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_SNAPSHOT_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$deleted -ne 1 ]; then \
		echo "snapshot deletion verification failed: $(PRE_RELEASE_SNAPSHOT) still present"; \
		exit 1; \
	fi; \
	echo "snapshot deleted and verified: $(PRE_RELEASE_SNAPSHOT)"

pre-release-cycle: pre-release-snapshot pre-release-haos-smoke
	@echo "pre-release cycle complete for snapshot $(PRE_RELEASE_SNAPSHOT)"

pre-release-run: pre-release-preflight
	@set -eu; \
	set -a; . "$(PRE_RELEASE_ENV)"; set +a; \
	insecure_flag=""; \
	if [ "$${PROXMOX_TLS_INSECURE:-false}" = "true" ]; then insecure_flag="-k"; fi; \
	rollback() { \
		snapshot_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot" || true)"; \
		if ! printf '%s\n' "$$snapshot_json" | grep -F "\"name\":\"$(PRE_RELEASE_SNAPSHOT)\"" >/dev/null; then \
			echo "rollback failed: snapshot missing $(PRE_RELEASE_SNAPSHOT)"; \
			return 1; \
		fi; \
		curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			-X POST \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot/$(PRE_RELEASE_SNAPSHOT)/rollback" >/dev/null || return 1; \
		rollback_unlocked=0; \
		i=1; \
		while [ $$i -le "$(PRE_RELEASE_ROLLBACK_VERIFY_RETRIES)" ]; do \
			config_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
				-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
				"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/config" || true)"; \
			if ! printf '%s\n' "$$config_json" | grep -E '"lock"[[:space:]]*:' >/dev/null; then \
				rollback_unlocked=1; \
				break; \
			fi; \
			sleep "$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)"; \
			i=$$((i + 1)); \
		done; \
		if [ $$rollback_unlocked -ne 1 ]; then \
			echo "rollback failed: VM still locked after rollback"; \
			return 1; \
		fi; \
		running=0; \
		running_seen=0; \
		i=1; \
		while [ $$i -le "$(PRE_RELEASE_ROLLBACK_VERIFY_RETRIES)" ]; do \
			status_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
				-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
				"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/status/current" || true)"; \
			if printf '%s\n' "$$status_json" | grep -F '"status":"running"' >/dev/null; then \
				if [ $$running_seen -eq 1 ]; then \
					running=1; \
					break; \
				fi; \
				running_seen=1; \
				sleep "$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)"; \
				i=$$((i + 1)); \
				continue; \
			fi; \
			running_seen=0; \
			curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
				-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
				-X POST \
				"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/status/start" >/dev/null 2>&1 || true; \
			sleep "$(PRE_RELEASE_ROLLBACK_VERIFY_DELAY)"; \
			i=$$((i + 1)); \
		done; \
		if [ $$running -ne 1 ]; then \
			echo "rollback failed: VM not running after rollback"; \
			return 1; \
		fi; \
		ssh_ready=0; \
		i=1; \
		while [ $$i -le "$(PRE_RELEASE_HAOS_READY_RETRIES)" ]; do \
			if $${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
				-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
				"$${HAOS_SSH_USER}@$${HAOS_HOST}" 'true' >/dev/null 2>&1; then \
				ssh_ready=1; \
				break; \
			fi; \
			sleep "$(PRE_RELEASE_HAOS_READY_DELAY)"; \
			i=$$((i + 1)); \
		done; \
		if [ $$ssh_ready -ne 1 ]; then \
			echo "rollback failed: HAOS SSH unreachable after rollback"; \
			return 1; \
		fi; \
		ha_ready=0; \
		i=1; \
		while [ $$i -le "$(PRE_RELEASE_HAOS_READY_RETRIES)" ]; do \
			if $${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
				-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
				"$${HAOS_SSH_USER}@$${HAOS_HOST}" \
				'ha core info >/dev/null && ha supervisor info >/dev/null' >/dev/null 2>&1; then \
				ha_ready=1; \
				break; \
			fi; \
			sleep "$(PRE_RELEASE_HAOS_READY_DELAY)"; \
			i=$$((i + 1)); \
		done; \
		if [ $$ha_ready -ne 1 ]; then \
			echo "rollback failed: ha core/supervisor not ready"; \
			return 1; \
		fi; \
		echo "rollback executed and verified: $(PRE_RELEASE_SNAPSHOT)"; \
	}; \
	trap 'if ! rollback; then echo "rollback verification failed"; exit 1; fi' EXIT INT TERM; \
	curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
		-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
		-X POST \
		--data-urlencode "snapname=$(PRE_RELEASE_SNAPSHOT)" \
		--data-urlencode "description=ups2mqtt pre-release snapshot" \
		"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot" >/dev/null; \
	verified=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES)" ]; do \
		snapshot_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/snapshot")"; \
		if printf '%s\n' "$$snapshot_json" | grep -F "\"name\":\"$(PRE_RELEASE_SNAPSHOT)\"" >/dev/null; then \
			verified=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_SNAPSHOT_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$verified -ne 1 ]; then \
		echo "snapshot verification failed: $(PRE_RELEASE_SNAPSHOT) not present after $(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES) attempts"; \
		exit 1; \
	fi; \
	snapshot_unlocked=0; \
	i=1; \
	while [ $$i -le "$(PRE_RELEASE_SNAPSHOT_VERIFY_RETRIES)" ]; do \
		config_json="$$(curl -fsS $$insecure_flag --max-time "$(PROXMOX_API_TIMEOUT)" \
			-H "Authorization: PVEAPIToken=$${PROXMOX_TOKEN_ID}=$${PROXMOX_TOKEN_SECRET}" \
			"$${PROXMOX_API_URL%/}/nodes/$${PROXMOX_NODE}/qemu/$${PROXMOX_VM_ID}/config")"; \
		if ! printf '%s\n' "$$config_json" | grep -E '"lock"[[:space:]]*:' >/dev/null; then \
			snapshot_unlocked=1; \
			break; \
		fi; \
		sleep "$(PRE_RELEASE_SNAPSHOT_VERIFY_DELAY)"; \
		i=$$((i + 1)); \
	done; \
	if [ $$snapshot_unlocked -ne 1 ]; then \
		echo "snapshot lock check failed: VM still locked after snapshot creation"; \
		exit 1; \
	fi; \
	echo "snapshot created and verified: $(PRE_RELEASE_SNAPSHOT)"; \
	$${PRE_RELEASE_SSH_CMD:-$(PRE_RELEASE_SSH_CMD)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout="$(HAOS_SSH_TIMEOUT)" \
		-i "$${HAOS_SSH_KEY_PATH}" -p "$${HAOS_SSH_PORT}" \
		"$${HAOS_SSH_USER}@$${HAOS_HOST}" \
		'ha core info >/dev/null && ha supervisor info >/dev/null && echo "haos smoke: ok"'; \
	if [ -n "$${PRE_RELEASE_TEST_CMD:-}" ]; then \
		echo "running PRE_RELEASE_TEST_CMD"; \
		sh -lc "$${PRE_RELEASE_TEST_CMD}"; \
	else \
		echo "PRE_RELEASE_TEST_CMD not set; smoke-only run complete"; \
	fi

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

bump-version:
	@if [ -z "$(VERSION)" ]; then \
		VERSION=$$(grep '^APP=' version | cut -d'=' -f2); \
		echo "Using version from version file: $$VERSION"; \
	else \
		VERSION="$(VERSION)"; \
		echo "Setting new version: $$VERSION"; \
		sed -i "s/^APP=.*/APP=$$VERSION/" version; \
	fi; \
	sed -i "s/^version:.*/version: \"$$VERSION\"/" homeassistant-addon/ups2mqtt/config.yaml; \
	sed -i "s/^APP_VERSION = .*/APP_VERSION = \"$$VERSION\"/" ups2mqtt/rootfs/usr/src/app/ups2mqtt/versions.py; \
	sed -i "s/^APP_VERSION = .*/APP_VERSION = \"$$VERSION\"/" homeassistant-addon/ups2mqtt/app/ups2mqtt/versions.py; \
	echo "Updated version to $$VERSION in:"; \
	echo "  - version"; \
	echo "  - homeassistant-addon/ups2mqtt/config.yaml"; \
	echo "  - ups2mqtt/rootfs/usr/src/app/ups2mqtt/versions.py"; \
	echo "  - homeassistant-addon/ups2mqtt/app/ups2mqtt/versions.py"

git-commit-template:
	@VERSION=$$(grep '^APP=' version | cut -d'=' -f2); \
	echo ""; \
	echo "# Current version: $$VERSION"; \
	echo ""; \
	echo "# Current git status:"; \
	echo ""; \
	git status --short; \
	echo ""; \
	echo "# Git add and commit commands (copy/paste and modify as needed):"; \
	echo ""; \
	if git status --short | grep -q .; then \
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
	fi; \
	echo ""

git-push-template:
	@echo ""
	@echo "# Git push commands (copy/paste and modify as needed):"
	@echo ""
	@BRANCH=$$(git branch --show-current); \
	VERSION=$$(grep '^APP=' version | cut -d'=' -f2); \
	echo "# Current branch: $$BRANCH"; \
	echo "# Current version: $$VERSION"; \
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
	echo "# Create and push a version tag:"; \
	echo "git tag -a v$$VERSION -m \"Release v$$VERSION\""; \
	echo "git push origin v$$VERSION"; \
	echo ""; \
	echo "# Preview auto-generated release notes BEFORE creating release:"; \
	echo "gh api repos/aburow/ups2mqtt-db/releases/generate-notes -f tag_name=v$$VERSION"; \
	echo ""; \
	echo "# Create GitHub release (this makes it visible to HA):"; \
	echo "gh release create v$$VERSION --title \"v$$VERSION\" --notes \"<release notes here>\""; \
	echo ""; \
	echo "# Or create release with auto-generated notes:"; \
	echo "# gh release create v$$VERSION --title \"v$$VERSION\" --generate-notes"; \
	echo ""; \
	echo "# Or push all tags:"; \
	echo "# git push --tags"; \
	echo ""
