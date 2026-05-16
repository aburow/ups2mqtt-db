#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    fail "Missing required environment variable: ${name}"
  fi
}

require_env "HAOS_HOST"
require_env "HAOS_SSH_USER"
require_env "HAOS_SSH_PORT"
require_env "HAOS_SSH_KEY_PATH"

PRE_RELEASE_SSH_CMD_VALUE="${PRE_RELEASE_SSH_CMD:-ssh}"
HAOS_SSH_TIMEOUT_VALUE="${HAOS_SSH_TIMEOUT:-15}"
PRE_RELEASE_ADDON_REPOSITORY_URL_VALUE="${PRE_RELEASE_ADDON_REPOSITORY_URL:-https://github.com/aburow/ups2mqtt-db}"
PRE_RELEASE_ADDON_SLUG_VALUE="${PRE_RELEASE_ADDON_SLUG:-}"
PRE_RELEASE_ADDON_NAME_HINT_VALUE="${PRE_RELEASE_ADDON_NAME_HINT:-ups2mqtt}"
PRE_RELEASE_ADDON_OPTIONS_JSON_VALUE="${PRE_RELEASE_ADDON_OPTIONS_JSON:-}"
PRE_RELEASE_ADDON_CONFIG_FILE_VALUE="${PRE_RELEASE_ADDON_CONFIG_FILE:-homeassistant-addon/ups2mqtt/config.yaml}"
PRE_RELEASE_MQTT_USERNAME_VALUE="${PRE_RELEASE_MQTT_USERNAME:-}"
PRE_RELEASE_MQTT_PASSWORD_VALUE="${PRE_RELEASE_MQTT_PASSWORD:-}"
PRE_RELEASE_TEST_DEVICE_ID_VALUE="${PRE_RELEASE_TEST_DEVICE_ID:-}"
PRE_RELEASE_TEST_DEVICE_HOST_VALUE="${PRE_RELEASE_TEST_DEVICE_HOST:-}"
PRE_RELEASE_TEST_DEVICE_SOURCE_VALUE="${PRE_RELEASE_TEST_DEVICE_SOURCE:-apc_modbus_smart}"
PRE_RELEASE_TEST_DEVICE_SNMP_COMMUNITY_VALUE="${PRE_RELEASE_TEST_DEVICE_SNMP_COMMUNITY:-public}"
PRE_RELEASE_LOG_TAIL_LINES_VALUE="${PRE_RELEASE_LOG_TAIL_LINES:-200}"
PRE_RELEASE_LOG_VERIFY_RETRIES_VALUE="${PRE_RELEASE_LOG_VERIFY_RETRIES:-20}"
PRE_RELEASE_LOG_VERIFY_DELAY_VALUE="${PRE_RELEASE_LOG_VERIFY_DELAY:-3}"
PRE_RELEASE_RESTART_VERIFY_RETRIES_VALUE="${PRE_RELEASE_RESTART_VERIFY_RETRIES:-20}"
PRE_RELEASE_RESTART_VERIFY_DELAY_VALUE="${PRE_RELEASE_RESTART_VERIFY_DELAY:-3}"
PRE_RELEASE_FORCE_DEBUG_VALUE="${PRE_RELEASE_FORCE_DEBUG:-true}"
HA_ENTITY_GROUP_CMD=""
HA_ENTITY_SINGULAR=""
HA_REPO_ENTITY=""
EXPECTED_DEVICE_EVIDENCE_REGEX=""

if ! [[ "${PRE_RELEASE_LOG_TAIL_LINES_VALUE}" =~ ^[0-9]+$ ]] || [ "${PRE_RELEASE_LOG_TAIL_LINES_VALUE}" -le 0 ]; then
  fail "PRE_RELEASE_LOG_TAIL_LINES must be a positive integer"
fi
if ! [[ "${PRE_RELEASE_LOG_VERIFY_RETRIES_VALUE}" =~ ^[0-9]+$ ]] || [ "${PRE_RELEASE_LOG_VERIFY_RETRIES_VALUE}" -le 0 ]; then
  fail "PRE_RELEASE_LOG_VERIFY_RETRIES must be a positive integer"
fi
if ! [[ "${PRE_RELEASE_LOG_VERIFY_DELAY_VALUE}" =~ ^[0-9]+$ ]] || [ "${PRE_RELEASE_LOG_VERIFY_DELAY_VALUE}" -le 0 ]; then
  fail "PRE_RELEASE_LOG_VERIFY_DELAY must be a positive integer"
fi
if ! [[ "${PRE_RELEASE_RESTART_VERIFY_RETRIES_VALUE}" =~ ^[0-9]+$ ]] || [ "${PRE_RELEASE_RESTART_VERIFY_RETRIES_VALUE}" -le 0 ]; then
  fail "PRE_RELEASE_RESTART_VERIFY_RETRIES must be a positive integer"
fi
if ! [[ "${PRE_RELEASE_RESTART_VERIFY_DELAY_VALUE}" =~ ^[0-9]+$ ]] || [ "${PRE_RELEASE_RESTART_VERIFY_DELAY_VALUE}" -le 0 ]; then
  fail "PRE_RELEASE_RESTART_VERIFY_DELAY must be a positive integer"
fi

ssh_exec() {
  local remote_cmd="$1"
  local q_remote
  printf -v q_remote '%q' "$remote_cmd"
  eval "${PRE_RELEASE_SSH_CMD_VALUE} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout='${HAOS_SSH_TIMEOUT_VALUE}' -i '${HAOS_SSH_KEY_PATH}' -p '${HAOS_SSH_PORT}' '${HAOS_SSH_USER}@${HAOS_HOST}' ${q_remote}"
}

ssh_capture() {
  local remote_cmd="$1"
  local q_remote
  printf -v q_remote '%q' "$remote_cmd"
  eval "${PRE_RELEASE_SSH_CMD_VALUE} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout='${HAOS_SSH_TIMEOUT_VALUE}' -i '${HAOS_SSH_KEY_PATH}' -p '${HAOS_SSH_PORT}' '${HAOS_SSH_USER}@${HAOS_HOST}' ${q_remote}"
}

redact_output() {
  sed -E \
    -e 's#(PVEAPIToken=)[^ ]+#\1REDACTED#g' \
    -e 's#(token|password|secret|mqtt_password|mqtt_username|ha_token|authorization)[=: ]+[^[:space:]]+#\1=REDACTED#ig' \
    -e 's#(/[^ ]*ssh[^ ]*key[^ ]*)#REDACTED_KEY_PATH#g' \
    -e 's#(https?://)[^/@]+:[^/@]+@#\1REDACTED:REDACTED@#g'
}

sanitize_json_output() {
  python3 -c '
import json
import sys

raw = sys.stdin.read()
if not raw:
    print("")
    raise SystemExit(0)

decoder = json.JSONDecoder()
for idx, ch in enumerate(raw):
    if ch not in "{[":
        continue
    try:
        obj, _ = decoder.raw_decode(raw[idx:])
        print(json.dumps(obj, separators=(",", ":")))
        raise SystemExit(0)
    except json.JSONDecodeError:
        continue

print("")
'
}

run_ha_cmd() {
  local label="$1"
  local remote_cmd="$2"
  local out rc

  log "check: ${label}"
  set +e
  out="$(ssh_capture "${remote_cmd}" 2>&1)"
  rc=$?
  set -e

  if [ ${rc} -ne 0 ]; then
    printf 'ERROR: %s failed (exit=%s)\n' "${label}" "${rc}" >&2
    printf 'ERROR: command: %s\n' "${remote_cmd}" >&2
    printf '%s\n' "${out}" | redact_output >&2
  fi
  RUN_HA_CMD_OUTPUT="${out}"
  return ${rc}
}

build_effective_options_json() {
  local default_json="$1"
  local base_json="$2"
  python3 - "$default_json" "$base_json" "${PRE_RELEASE_FORCE_DEBUG_VALUE}" \
    "${PRE_RELEASE_MQTT_USERNAME_VALUE}" "${PRE_RELEASE_MQTT_PASSWORD_VALUE}" \
    "${PRE_RELEASE_TEST_DEVICE_ID_VALUE}" "${PRE_RELEASE_TEST_DEVICE_HOST_VALUE}" \
    "${PRE_RELEASE_TEST_DEVICE_SOURCE_VALUE}" "${PRE_RELEASE_TEST_DEVICE_SNMP_COMMUNITY_VALUE}" <<'PY'
import json
import sys

import yaml

defaults = json.loads(sys.argv[1]) if sys.argv[1] else {}
base = json.loads(sys.argv[2]) if sys.argv[2] else {}
force_debug = str(sys.argv[3]).strip().lower() in {"1", "true", "yes", "on"}
mqtt_username = sys.argv[4]
mqtt_password = sys.argv[5]
test_device_id = sys.argv[6]
test_device_host = sys.argv[7]
test_device_source = sys.argv[8] or "apc_modbus_smart"
test_device_snmp_community = sys.argv[9] or "public"

if not isinstance(defaults, dict):
    defaults = {}
if not isinstance(base, dict):
    base = {}

merged = dict(defaults)
merged.update({key: value for key, value in base.items() if value is not None})

if force_debug:
    merged["log_level"] = "DEBUG"
if mqtt_username:
    merged["mqtt_username"] = mqtt_username
if mqtt_password:
    merged["mqtt_password"] = mqtt_password

config_text = merged.get("config", "")
if not isinstance(config_text, str):
    config_text = ""

parsed = yaml.safe_load(config_text) if config_text else {}
if not isinstance(parsed, dict):
    parsed = {}

if test_device_host:
    device_id = test_device_id or test_device_host.replace(".", "-")
    parsed["devices"] = [
        {
            "id": device_id,
            "source": test_device_source,
            "host": test_device_host,
            "snmp_community": test_device_snmp_community,
            "debug_logging": True,
            "discovery_enabled": True,
            "polling_enabled": True,
        }
    ]

devices = parsed.get("devices")
if isinstance(devices, list):
    for device in devices:
        if isinstance(device, dict) and force_debug:
            device["debug_logging"] = True

merged["config"] = yaml.safe_dump(parsed, sort_keys=False)
print(json.dumps(merged, separators=(",", ":")))
PY
}

load_default_options_json() {
  python3 - "${PRE_RELEASE_ADDON_CONFIG_FILE_VALUE}" <<'PY'
import json
import sys

import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    config = yaml.safe_load(handle) or {}

options = config.get("options", {})
if not isinstance(options, dict):
    options = {}

print(json.dumps(options, separators=(",", ":")))
PY
}

build_device_evidence_regex() {
  local options_json="$1"
  python3 - "$options_json" <<'PY'
import json
import re
import sys

import yaml

opts = json.loads(sys.argv[1]) if sys.argv[1] else {}
config_text = opts.get("config", "")
if not isinstance(config_text, str) or not config_text.strip():
    print("")
    raise SystemExit(0)

parsed = yaml.safe_load(config_text) or {}
if not isinstance(parsed, dict):
    print("")
    raise SystemExit(0)

devices = parsed.get("devices")
if not isinstance(devices, list):
    print("")
    raise SystemExit(0)

hints = []
for dev in devices:
    if not isinstance(dev, dict):
        continue
    for key in ("id", "host", "name"):
        value = dev.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value:
                hints.append(re.escape(value))

if not hints:
    print("")
else:
    unique = []
    seen = set()
    for hint in hints:
        if hint in seen:
            continue
        seen.add(hint)
        unique.append(hint)
    print("(" + "|".join(unique) + ")")
PY
}

fetch_options_via_api() {
  local slug="$1"
  run_ha_cmd "read ${HA_ENTITY_SINGULAR} options via api (${slug})" \
    "sh -lc ': \"\${SUPERVISOR_TOKEN:?}\"; command -v jq >/dev/null; curl -fsS -H \"Authorization: Bearer \${SUPERVISOR_TOKEN}\" \"http://supervisor/addons/${slug}/info\" | jq -c \".data.options\"'"
  RUN_HA_CMD_OUTPUT="$(printf '%s\n' "${RUN_HA_CMD_OUTPUT}" | sanitize_json_output)"
}

apply_options_via_api() {
  local slug="$1"
  local options_json="$2"
  local options_b64
  options_b64="$(printf '%s' "${options_json}" | base64 | tr -d '\n')"
  run_ha_cmd "apply ${HA_ENTITY_SINGULAR} options via api (${slug})" \
    "OPTIONS_B64='${options_b64}' sh -lc ': \"\${SUPERVISOR_TOKEN:?}\"; options_json=\$(printf %s \"\${OPTIONS_B64}\" | base64 -d); curl -fsS -X POST -H \"Authorization: Bearer \${SUPERVISOR_TOKEN}\" -H \"Content-Type: application/json\" -d \"{\\\"options\\\":\${options_json}}\" \"http://supervisor/addons/${slug}/options\" >/dev/null'"
}

ensure_mqtt_login() {
  if [ -z "${PRE_RELEASE_MQTT_USERNAME_VALUE}" ] || [ -z "${PRE_RELEASE_MQTT_PASSWORD_VALUE}" ]; then
    return 0
  fi

  local current_options_json next_options_json options_b64
  fetch_options_via_api "core_mosquitto" || fail "Unable to read Mosquitto options via Supervisor API"
  current_options_json="${RUN_HA_CMD_OUTPUT}"
  next_options_json="$(python3 - "${current_options_json}" "${PRE_RELEASE_MQTT_USERNAME_VALUE}" "${PRE_RELEASE_MQTT_PASSWORD_VALUE}" <<'PY'
import json
import sys

options = json.loads(sys.argv[1]) if sys.argv[1] else {}
if not isinstance(options, dict):
    options = {}

username = sys.argv[2]
password = sys.argv[3]
logins = options.get("logins")
if not isinstance(logins, list):
    logins = []

next_logins = [
    login
    for login in logins
    if not (isinstance(login, dict) and login.get("username") == username)
]
next_logins.append({"username": username, "password": password})
options["logins"] = next_logins
print(json.dumps(options, separators=(",", ":")))
PY
)"
  options_b64="$(printf '%s' "${next_options_json}" | base64 | tr -d '\n')"
  run_ha_cmd "configure Mosquitto pre-release login" \
    "OPTIONS_B64='${options_b64}' sh -lc ': \"\${SUPERVISOR_TOKEN:?}\"; options_json=\$(printf %s \"\${OPTIONS_B64}\" | base64 -d); curl -fsS -X POST -H \"Authorization: Bearer \${SUPERVISOR_TOKEN}\" -H \"Content-Type: application/json\" -d \"{\\\"options\\\":\${options_json}}\" \"http://supervisor/addons/core_mosquitto/options\" >/dev/null'" \
    || fail "Failed to configure Mosquitto pre-release login"
  run_ha_cmd "restart Mosquitto after pre-release login update" "ha ${HA_ENTITY_GROUP_CMD} restart core_mosquitto >/dev/null" \
    || fail "Failed to restart Mosquitto after pre-release login update"
}

ha_cli_choose_mode() {
  if run_ha_cmd "detect ha apps command" "ha apps list >/dev/null"; then
    HA_ENTITY_GROUP_CMD="apps"
    HA_ENTITY_SINGULAR="app"
    HA_REPO_ENTITY="app repositories"
    return 0
  fi
  if run_ha_cmd "detect ha addons command" "ha addons list >/dev/null"; then
    HA_ENTITY_GROUP_CMD="addons"
    HA_ENTITY_SINGULAR="addon"
    HA_REPO_ENTITY="addon repositories"
    return 0
  fi
  fail "Neither 'ha apps' nor 'ha addons' command set is usable"
}

confirm_haos_baseline() {
  run_ha_cmd "ha core info" "ha core info >/dev/null" || fail "HA core not reachable"
  run_ha_cmd "ha supervisor info" "ha supervisor info >/dev/null" || fail "HA supervisor not reachable"
}

confirm_mqtt_path() {
  if run_ha_cmd "mqtt path: ${HA_ENTITY_GROUP_CMD} list" "ha ${HA_ENTITY_GROUP_CMD} list"; then
    if printf '%s\n' "${RUN_HA_CMD_OUTPUT}" | grep -qi mosquitto; then
      return 0
    fi
  fi
  if run_ha_cmd "mqtt path: ha core logs grep" "ha core logs --raw | grep -qi mqtt"; then
    return 0
  fi
  fail "No MQTT path evidence found (mosquitto add-on or MQTT-related core logs)"
}

add_repository() {
  log "check: repository URL configured"
  if [ -z "${PRE_RELEASE_ADDON_REPOSITORY_URL_VALUE}" ]; then
    fail "PRE_RELEASE_ADDON_REPOSITORY_URL is empty"
  fi
  log "repository URL: ${PRE_RELEASE_ADDON_REPOSITORY_URL_VALUE}"

  run_ha_cmd "store repository: add" "ha store add '${PRE_RELEASE_ADDON_REPOSITORY_URL_VALUE}'" || true
  run_ha_cmd "store repository: reload" "ha store reload >/dev/null" || fail "Repository reload failed"
  run_ha_cmd "store repository: list" "ha store repositories" || fail "Repository list failed"
  if ! printf '%s\n' "${RUN_HA_CMD_OUTPUT}" | grep -F "${PRE_RELEASE_ADDON_REPOSITORY_URL_VALUE}" >/dev/null; then
    printf 'WARN: %s URL not visible after refresh: %s\n' "${HA_REPO_ENTITY}" "${PRE_RELEASE_ADDON_REPOSITORY_URL_VALUE}" >&2
    printf 'WARN: continuing; validating against Supervisor-visible installed repository/app slugs\n' >&2
    printf '%s\n' "${RUN_HA_CMD_OUTPUT}" | redact_output >&2
  fi
}

list_installable_addon_slugs() {
  run_ha_cmd "--raw-json store addons" "ha --raw-json store addons" || return 1
  python3 - "${RUN_HA_CMD_OUTPUT}" <<'PY'
import json
import sys

raw = sys.argv[1]
if not raw:
    raise SystemExit(1)

decoder = json.JSONDecoder()
obj = None
for idx, ch in enumerate(raw):
    if ch not in "{[":
        continue
    try:
        obj, _ = decoder.raw_decode(raw[idx:])
        break
    except json.JSONDecodeError:
        continue

if obj is None:
    raise SystemExit(1)

addons = []
if isinstance(obj, dict):
    data = obj.get("data")
    if isinstance(data, dict):
        addons = data.get("addons", [])
    elif isinstance(obj.get("addons"), list):
        addons = obj.get("addons", [])
elif isinstance(obj, list):
    addons = obj

for addon in addons:
    if isinstance(addon, dict):
        slug = addon.get("slug")
        if isinstance(slug, str) and slug.strip():
            print(slug.strip())
PY
}

addon_exists() {
  local slug="$1"
  set +e
  ssh_capture "ha ${HA_ENTITY_GROUP_CMD} info '${slug}' >/dev/null" >/dev/null 2>&1
  local rc=$?
  set -e
  return ${rc}
}

resolve_slug_from_list() {
  local list="$1"
  local candidate=""

  candidate="$(printf '%s\n' "${list}" | awk 'tolower($0)=="ups2mqtt"{print; exit}')"
  if [ -n "${candidate}" ]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  candidate="$(printf '%s\n' "${list}" | awk 'tolower($0) ~ /ups2mqtt/{print; exit}')"
  if [ -n "${candidate}" ]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  candidate="$(printf '%s\n' "${list}" | awk -v hint="$(printf '%s' "${PRE_RELEASE_ADDON_NAME_HINT_VALUE}" | tr '[:upper:]' '[:lower:]')" 'tolower($0) ~ hint {print; exit}')"
  if [ -n "${candidate}" ]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  return 1
}

resolve_addon_slug() {
  local addon_list
  if ! addon_list="$(list_installable_addon_slugs)"; then
    fail "Unable to get installable add-on metadata from HA store"
  fi

  if [ -z "${addon_list}" ]; then
    run_ha_cmd "--raw-json store addons (debug dump)" "ha --raw-json store addons" || true
    fail "Installable add-on metadata is empty after repository refresh"
  fi

  if [ -n "${PRE_RELEASE_ADDON_SLUG_VALUE}" ]; then
    if printf '%s\n' "${addon_list}" | grep -Fx "${PRE_RELEASE_ADDON_SLUG_VALUE}" >/dev/null; then
      printf '%s\n' "${PRE_RELEASE_ADDON_SLUG_VALUE}"
      return 0
    fi
    fail "PRE_RELEASE_ADDON_SLUG set to '${PRE_RELEASE_ADDON_SLUG_VALUE}' but it is not present in installable add-on metadata"
  fi

  local resolved_slug
  resolved_slug="$(resolve_slug_from_list "${addon_list}" || true)"
  if [ -z "${resolved_slug}" ]; then
    printf 'ERROR: slug resolution candidates:\n%s\n' "${addon_list}" | redact_output >&2
    fail "Unable to resolve Supervisor-visible ${HA_ENTITY_SINGULAR} slug from list; set PRE_RELEASE_ADDON_SLUG"
  fi
  printf '%s\n' "${resolved_slug}"
}

set_or_confirm_options() {
  local slug="$1"
  local default_options_json base_options_json effective_options_json
  default_options_json="$(load_default_options_json)"
  if [ -n "${PRE_RELEASE_ADDON_OPTIONS_JSON_VALUE}" ]; then
    base_options_json="${PRE_RELEASE_ADDON_OPTIONS_JSON_VALUE}"
  else
    fetch_options_via_api "${slug}" || fail "Unable to read current add-on options via Supervisor API"
    base_options_json="${RUN_HA_CMD_OUTPUT}"
  fi

  effective_options_json="$(build_effective_options_json "${default_options_json}" "${base_options_json}")"
  EXPECTED_DEVICE_EVIDENCE_REGEX="$(build_device_evidence_regex "${effective_options_json}")"

  apply_options_via_api "${slug}" "${effective_options_json}" || fail "Failed to apply add-on options via Supervisor API"
  fetch_options_via_api "${slug}" || fail "Unable to verify add-on options via Supervisor API"
}

wait_for_started() {
  local slug="$1"
  local i=1
  while [ "${i}" -le "${PRE_RELEASE_RESTART_VERIFY_RETRIES_VALUE}" ]; do
    if run_ha_cmd "${HA_ENTITY_SINGULAR} started state (${slug})" "ha ${HA_ENTITY_GROUP_CMD} info '${slug}' | grep -qi 'state: started'"; then
      return 0
    fi
    sleep "${PRE_RELEASE_RESTART_VERIFY_DELAY_VALUE}"
    i=$((i + 1))
  done
  return 1
}

collect_logs() {
  local slug="$1"
  if run_ha_cmd "${HA_ENTITY_SINGULAR} logs (${slug})" "timeout 20 ha ${HA_ENTITY_GROUP_CMD} logs '${slug}' 2>&1 | tail -n ${PRE_RELEASE_LOG_TAIL_LINES_VALUE}"; then
    printf '%s\n' "${RUN_HA_CMD_OUTPUT}"
    return 0
  fi
  printf '%s\n' "${RUN_HA_CMD_OUTPUT}"
  return 0
}

validate_logs() {
  local logs="$1"
  log "check: startup logs"
  local evidence_excerpt
  evidence_excerpt="$(printf '%s\n' "${logs}" | grep -Ei 'ups2mqtt|mqtt|homeassistant|discovery|publish|poll|apc-test|192\.168\.100\.7|options\.json|/data/options\.json|error|failed|exception|traceback' | tail -n 120 || true)"
  if printf '%s\n' "${logs}" | grep -qiE "traceback|fatal|exception"; then
    printf '%s\n' "${evidence_excerpt}" | redact_output >&2
    fail "Add-on logs contain fatal startup indicators"
  fi
  if printf '%s\n' "${logs}" | grep -qiE "apparmor.*denied|denied.*apparmor"; then
    printf '%s\n' "${evidence_excerpt}" | redact_output >&2
    fail "AppArmor denial detected in add-on logs"
  fi
  if ! printf '%s\n' "${logs}" | grep -qiE "discovery bridge published|homeassistant/.+/config|publishing state for|published [0-9]+ values"; then
    printf '%s\n' "${evidence_excerpt}" | redact_output >&2
    fail "No MQTT publish or Home Assistant discovery evidence found in add-on logs"
  fi
  if [ -n "${EXPECTED_DEVICE_EVIDENCE_REGEX}" ]; then
    if ! printf '%s\n' "${logs}" | grep -qiE "${EXPECTED_DEVICE_EVIDENCE_REGEX}"; then
      printf '%s\n' "${evidence_excerpt}" | redact_output >&2
      fail "No configured device polling evidence found in add-on logs"
    fi
  fi
}

wait_for_valid_logs() {
  local slug="$1"
  local logs=""
  local i=1
  while [ "${i}" -le "${PRE_RELEASE_LOG_VERIFY_RETRIES_VALUE}" ]; do
    logs="$(collect_logs "${slug}")"
    if printf '%s\n' "${logs}" | grep -qiE "traceback|fatal|exception"; then
      validate_logs "${logs}"
    fi
    if printf '%s\n' "${logs}" | grep -qiE "apparmor.*denied|denied.*apparmor"; then
      validate_logs "${logs}"
    fi
    if printf '%s\n' "${logs}" | grep -qiE "discovery bridge published|homeassistant/.+/config|publishing state for|published [0-9]+ values"; then
      if [ -z "${EXPECTED_DEVICE_EVIDENCE_REGEX}" ] \
        || printf '%s\n' "${logs}" | grep -qiE "${EXPECTED_DEVICE_EVIDENCE_REGEX}"; then
        validate_logs "${logs}"
        return 0
      fi
    fi
    sleep "${PRE_RELEASE_LOG_VERIFY_DELAY_VALUE}"
    i=$((i + 1))
  done
  validate_logs "${logs}"
}

diagnose_install_failure() {
  local slug="$1"
  local terms="local_ups2mqtt|ups2mqtt|install|build|image|docker|error|failed|invalid|schema|config"
  run_ha_cmd "supervisor logs (install diagnostics)" "ha supervisor logs -n 300 2>&1 | grep -Ei '${terms}' | tail -n 120" || true
  printf '%s\n' "${RUN_HA_CMD_OUTPUT}" | redact_output >&2
  run_ha_cmd "store addons (install diagnostics)" "ha store addons 2>&1 | grep -Ei '${terms}|slug:' | tail -n 160" || true
  printf '%s\n' "${RUN_HA_CMD_OUTPUT}" | redact_output >&2
  printf 'ERROR: install failed for slug: %s\n' "${slug}" >&2
}

main() {
  confirm_haos_baseline
  ha_cli_choose_mode
  confirm_mqtt_path
  ensure_mqtt_login
  add_repository

  local addon_slug
  addon_slug="$(resolve_addon_slug)"
  log "resolved add-on slug: ${addon_slug}"

  if ! run_ha_cmd "install ${HA_ENTITY_SINGULAR} (${addon_slug})" "ha ${HA_ENTITY_GROUP_CMD} install '${addon_slug}' >/dev/null"; then
    diagnose_install_failure "${addon_slug}"
    fail "Failed to install ${HA_ENTITY_SINGULAR}; stopping before post-install checks"
  fi
  run_ha_cmd "verify ${HA_ENTITY_SINGULAR} visible (${addon_slug})" "ha ${HA_ENTITY_GROUP_CMD} info '${addon_slug}' >/dev/null" || fail "${HA_ENTITY_SINGULAR} not visible after install attempt"

  set_or_confirm_options "${addon_slug}"

  run_ha_cmd "start ${HA_ENTITY_SINGULAR} (${addon_slug})" "ha ${HA_ENTITY_GROUP_CMD} start '${addon_slug}' >/dev/null" || fail "Failed to start ${HA_ENTITY_SINGULAR}"
  if ! wait_for_started "${addon_slug}"; then
    fail "Add-on did not reach started state"
  fi

  wait_for_valid_logs "${addon_slug}"

  run_ha_cmd "restart ${HA_ENTITY_SINGULAR} (${addon_slug})" "ha ${HA_ENTITY_GROUP_CMD} restart '${addon_slug}' >/dev/null" || fail "Failed to restart ${HA_ENTITY_SINGULAR}"
  if ! wait_for_started "${addon_slug}"; then
    fail "Add-on did not recover after restart"
  fi

  wait_for_valid_logs "${addon_slug}"

  log "pre-release HAOS add-on runtime validation: ok"
}

main "$@"
