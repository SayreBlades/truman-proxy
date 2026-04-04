#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# truman.sh — Truman devcontainer setup & lifecycle manager
#
# Usage:
#   .devcontainer/truman.sh init       Interactive setup wizard
#   .devcontainer/truman.sh start      Validate config + devcontainer up
#   .devcontainer/truman.sh run-pi     devcontainer exec ... pi (args forwarded)
#   .devcontainer/truman.sh stop       Stop containers
#   .devcontainer/truman.sh status     Show configuration & container status
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GATEWAY_YAML="$SCRIPT_DIR/gateway.yaml"
ENV_AGENT="$SCRIPT_DIR/.env.agent"
DOCKER_ENV="$SCRIPT_DIR/.env"

# ── Colors & output helpers ──────────────────────────────────────────

if [ -t 1 ]; then
    BOLD='\033[1m'
    DIM='\033[2m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    RESET='\033[0m'
else
    BOLD='' DIM='' GREEN='' YELLOW='' RED='' CYAN='' RESET=''
fi

info()  { echo -e "${GREEN}✅${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠️${RESET}  $*"; }
err()   { echo -e "${RED}❌${RESET} $*"; }
step()  { echo -e "\n${BOLD}${CYAN}$*${RESET}"; }

# Prompt yes/no. $1=prompt, $2=default (Y or N)
prompt_yn() {
    local prompt="$1" default="${2:-Y}"
    local yn_hint
    if [[ "$default" == "Y" ]]; then yn_hint="[Y/n]"; else yn_hint="[y/N]"; fi
    while true; do
        read -r -p "$(echo -e "  ${prompt} ${DIM}${yn_hint}${RESET}: ")" answer
        answer="${answer:-$default}"
        case "$answer" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) echo "  Please answer y or n." ;;
        esac
    done
}

# Prompt for a value. $1=prompt, $2=variable name, $3=default (optional)
prompt_value() {
    local prompt="$1" varname="$2" default="${3:-}"
    local hint=""
    if [ -n "$default" ]; then hint=" ${DIM}[${default}]${RESET}"; fi
    read -r -p "$(echo -e "  ${prompt}${hint}: ")" value
    value="${value:-$default}"
    eval "$varname=\"\$value\""
}

# ── Auth.json discovery ──────────────────────────────────────────────

PI_AUTH_JSON_PATH=""

PI_AUTH_SEARCH_PATHS=(
    "$HOME/.pi/agent/auth.json"
)

find_pi_auth() {
    for path in "${PI_AUTH_SEARCH_PATHS[@]}"; do
        if [ -f "$path" ]; then
            PI_AUTH_JSON_PATH="$path"
            return 0
        fi
    done
    return 1
}

# Check if a key exists in auth.json with an access token
# $1=key name (e.g., "anthropic")
auth_json_has_key() {
    local key="$1"
    [ -n "$PI_AUTH_JSON_PATH" ] && python3 -c "
import json, sys
auth = json.load(open('$PI_AUTH_JSON_PATH'))
entry = auth.get('$key', {})
if entry.get('access'):
    sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

# Get token expiry info from auth.json
# $1=key name. Prints remaining hours or "expired"
auth_json_expiry() {
    local key="$1"
    python3 -c "
import json, time
auth = json.load(open('$PI_AUTH_JSON_PATH'))
entry = auth.get('$key', {})
expires_ms = entry.get('expires', 0)
if not expires_ms:
    print('unknown')
else:
    remaining = (expires_ms / 1000) - time.time()
    if remaining <= 0:
        print('expired')
    else:
        print(f'{remaining/3600:.1f}h remaining')
" 2>/dev/null || echo "unknown"
}

# ── Provider: Anthropic ──────────────────────────────────────────────

ANTHROPIC_CONFIGURED=0
ANTHROPIC_MODE=""           # "shared" or "standalone"
ANTHROPIC_REFRESH_TOKEN=""

provider_anthropic_detect() {
    auth_json_has_key "anthropic"
}

provider_anthropic_prompt() {
    step "[Anthropic API]"

    if provider_anthropic_detect; then
        local expiry
        expiry=$(auth_json_expiry "anthropic")
        echo -e "  ${GREEN}✓${RESET} Found Anthropic credentials in $PI_AUTH_JSON_PATH ($expiry)"
    fi

    if ! prompt_yn "Set up Anthropic?" "Y"; then
        echo -e "  ${DIM}Skipped${RESET}"
        return
    fi

    if [ -n "$PI_AUTH_JSON_PATH" ] && provider_anthropic_detect; then
        if prompt_yn "Share token file with host pi (recommended)?" "Y"; then
            ANTHROPIC_MODE="shared"
            ANTHROPIC_CONFIGURED=1
            info "Anthropic configured (shared token from $PI_AUTH_JSON_PATH)"
            return
        fi
    fi

    # Manual refresh token entry
    echo ""
    echo -e "  ${DIM}Get a refresh token: run 'pi', then '/login' → Anthropic${RESET}"
    prompt_value "Refresh token (sk-ant-ort01-...)" ANTHROPIC_REFRESH_TOKEN
    if [ -z "$ANTHROPIC_REFRESH_TOKEN" ]; then
        warn "No refresh token provided — skipping Anthropic"
        return
    fi
    ANTHROPIC_MODE="standalone"
    ANTHROPIC_CONFIGURED=1
    info "Anthropic configured (standalone refresh token)"
}

provider_anthropic_yaml() {
    [ "$ANTHROPIC_CONFIGURED" -eq 0 ] && return

    cat << 'YAML_HEADER'
api.anthropic.com:
  type: oauth
  provider: anthropic
  strip_headers:
    - authorization
    - x-api-key
  inject_headers:
    X-Api-Key: "$ACCESS_TOKEN"
YAML_HEADER

    if [ "$ANTHROPIC_MODE" = "shared" ]; then
        cat << 'YAML_SHARED'
  token_file: "/host-auth/auth.json"
  token_file_key: "anthropic"
  refresh_token: ""
YAML_SHARED
    else
        echo "  refresh_token: \"$ANTHROPIC_REFRESH_TOKEN\""
    fi

    cat << 'YAML_AGENT'
  agent_env:
    ANTHROPIC_OAUTH_TOKEN: "sk-ant-oat01-DUMMY000000000000000000000000000000000000000000000000000000000000000000-0000000000"
YAML_AGENT
}

provider_anthropic_needs_auth_mount() {
    [ "$ANTHROPIC_CONFIGURED" -eq 1 ] && [ "$ANTHROPIC_MODE" = "shared" ]
}

# ── Provider: Brave Search ───────────────────────────────────────────

BRAVE_CONFIGURED=0
BRAVE_API_KEY=""

provider_brave_detect() {
    return 1  # No auto-discovery for Brave
}

provider_brave_prompt() {
    step "[Brave Search]"

    if ! prompt_yn "Set up Brave Search?" "N"; then
        echo -e "  ${DIM}Skipped${RESET}"
        return
    fi

    prompt_value "Brave API key (BSAp-...)" BRAVE_API_KEY
    if [ -z "$BRAVE_API_KEY" ]; then
        warn "No API key provided — skipping Brave Search"
        return
    fi
    BRAVE_CONFIGURED=1
    info "Brave Search configured"
}

provider_brave_yaml() {
    [ "$BRAVE_CONFIGURED" -eq 0 ] && return

    cat << EOF
api.search.brave.com:
  type: apikey
  strip_headers:
    - x-subscription-token
  inject_headers:
    X-Subscription-Token: "\$API_KEY"
  api_key: "$BRAVE_API_KEY"
  agent_env:
    BRAVE_API_KEY: "BSAdummy0000000000000000000000"
EOF
}

# ── Provider: GitHub ─────────────────────────────────────────────────

GITHUB_CONFIGURED=0
GITHUB_TOKEN=""

provider_github_detect() {
    command -v gh &>/dev/null && gh auth token &>/dev/null
}

provider_github_prompt() {
    step "[GitHub]"

    local detected_token=""
    if provider_github_detect; then
        detected_token=$(gh auth token 2>/dev/null)
        echo -e "  ${GREEN}✓${RESET} Found gh CLI token"
    fi

    if ! prompt_yn "Set up GitHub?" "N"; then
        echo -e "  ${DIM}Skipped${RESET}"
        return
    fi

    if [ -n "$detected_token" ]; then
        if prompt_yn "Use token from gh CLI?" "Y"; then
            GITHUB_TOKEN="$detected_token"
            GITHUB_CONFIGURED=1
            info "GitHub configured (from gh CLI)"
            return
        fi
    fi

    prompt_value "GitHub personal access token (ghp_... or gho_...)" GITHUB_TOKEN
    if [ -z "$GITHUB_TOKEN" ]; then
        warn "No token provided — skipping GitHub"
        return
    fi
    GITHUB_CONFIGURED=1
    info "GitHub configured"
}

provider_github_yaml() {
    [ "$GITHUB_CONFIGURED" -eq 0 ] && return

    cat << EOF
api.github.com:
  type: apikey
  strip_headers:
    - authorization
  inject_headers:
    Authorization: "token \$API_KEY"
  api_key: "$GITHUB_TOKEN"
  agent_env:
    GH_TOKEN: "ghp_DUMMY0000000000000000000000000000000000"

github.com:
  type: apikey
  strip_headers:
    - authorization
  inject_headers:
    Authorization: "Basic \$BASIC_AUTH"
  api_key: "$GITHUB_TOKEN"
EOF
}

# ── Provider list ────────────────────────────────────────────────────

PROVIDERS=(anthropic brave github)

# ── File generation ──────────────────────────────────────────────────

generate_gateway_yaml() {
    {
        echo "# Generated by truman.sh init — $(date)"
        echo "# Contains secrets — DO NOT COMMIT."
        echo ""

        local first=1
        for provider in "${PROVIDERS[@]}"; do
            local output
            output=$("provider_${provider}_yaml")
            if [ -n "$output" ]; then
                if [ "$first" -eq 0 ]; then echo ""; fi
                echo "$output"
                first=0
            fi
        done
    } > "$GATEWAY_YAML"
}

generate_env_agent() {
    {
        echo "# Auto-generated from gateway.yaml by truman.sh"
        echo "# Dummy values for the agent container — do not edit manually."

        local in_agent_env=0
        local seen=""

        while IFS= read -r line; do
            if [[ "$line" =~ ^[[:space:]]+agent_env:[[:space:]]*$ ]]; then
                in_agent_env=1
                continue
            fi
            if [ "$in_agent_env" -eq 1 ]; then
                if [[ "$line" =~ ^[[:space:]]{4,}[A-Z_]+: ]]; then
                    local stripped
                    stripped=$(echo "$line" | sed 's/^[[:space:]]*//')
                    local var_name="${stripped%%:*}"
                    local var_value="${stripped#*:}"
                    var_value=$(echo "$var_value" | sed 's/^[[:space:]]*//; s/^"//; s/"[[:space:]]*$//')
                    if [[ "$seen" != *"|${var_name}|"* ]]; then
                        echo "${var_name}=${var_value}"
                        seen="${seen}|${var_name}|"
                    fi
                else
                    in_agent_env=0
                fi
            fi
        done < "$GATEWAY_YAML"
    } > "$ENV_AGENT"
}

generate_docker_env() {
    {
        echo "# Generated by truman.sh init — do not edit"
        echo "# Docker Compose variable substitution"
        if [ -n "$PI_AUTH_JSON_PATH" ] && needs_auth_mount; then
            echo "PI_AUTH_JSON=$PI_AUTH_JSON_PATH"
        fi
    } > "$DOCKER_ENV"
}

needs_auth_mount() {
    for provider in "${PROVIDERS[@]}"; do
        if type "provider_${provider}_needs_auth_mount" &>/dev/null; then
            if "provider_${provider}_needs_auth_mount"; then
                return 0
            fi
        fi
    done
    return 1
}

# ── Gitignore helper ─────────────────────────────────────────────────

check_gitignore() {
    local gitignore="$PROJECT_ROOT/.gitignore"
    local entries=(".devcontainer/gateway.yaml" ".devcontainer/.env" ".devcontainer/.env.agent")
    local missing=()

    for entry in "${entries[@]}"; do
        if [ ! -f "$gitignore" ] || ! grep -qF "$entry" "$gitignore" 2>/dev/null; then
            missing+=("$entry")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        echo ""
        step "Gitignore"
        echo -e "  These generated files should not be committed:"
        for entry in "${missing[@]}"; do
            echo -e "    ${DIM}${entry}${RESET}"
        done
        if prompt_yn "Add them to .gitignore?" "Y"; then
            [ ! -f "$gitignore" ] && touch "$gitignore"
            echo "" >> "$gitignore"
            echo "# Truman — generated files (contain secrets or host-specific paths)" >> "$gitignore"
            for entry in "${missing[@]}"; do
                echo "$entry" >> "$gitignore"
            done
            info "Updated .gitignore"
        else
            echo ""
            echo -e "  ${DIM}Add manually:${RESET}"
            for entry in "${missing[@]}"; do
                echo "    echo '${entry}' >> .gitignore"
            done
        fi
    fi
}

# ── Validation ───────────────────────────────────────────────────────

validate_config() {
    local ok=1

    if [ ! -f "$GATEWAY_YAML" ]; then
        err "Missing $GATEWAY_YAML"
        ok=0
    elif [ ! -s "$GATEWAY_YAML" ]; then
        err "$GATEWAY_YAML is empty"
        ok=0
    fi

    if [ ! -f "$ENV_AGENT" ]; then
        err "Missing $ENV_AGENT"
        ok=0
    fi

    # Check PI_AUTH_JSON if gateway.yaml references token_file
    if [ -f "$GATEWAY_YAML" ] && grep -q 'token_file:' "$GATEWAY_YAML"; then
        if [ ! -f "$DOCKER_ENV" ]; then
            err "Missing $DOCKER_ENV (gateway.yaml uses token_file)"
            ok=0
        elif grep -q 'PI_AUTH_JSON=' "$DOCKER_ENV"; then
            local auth_path
            auth_path=$(grep 'PI_AUTH_JSON=' "$DOCKER_ENV" | cut -d= -f2)
            if [ ! -f "$auth_path" ]; then
                err "PI_AUTH_JSON=$auth_path does not exist"
                echo -e "    ${DIM}Run 'pi' and '/login' to create it${RESET}"
                ok=0
            fi
        fi
    fi

    if [ "$ok" -eq 0 ]; then
        echo ""
        echo -e "  Run ${BOLD}.devcontainer/truman.sh init${RESET} to set up."
        return 1
    fi
    return 0
}

# ── Subcommands ──────────────────────────────────────────────────────

cmd_init() {
    echo ""
    echo -e "${BOLD}🔧 Truman Setup${RESET}"
    echo ""

    # Discover pi auth.json
    echo -e "Searching for pi credentials..."
    if find_pi_auth; then
        echo -e "  ${GREEN}✓${RESET} Found: $PI_AUTH_JSON_PATH"
    else
        echo -e "  ${DIM}Not found in known locations${RESET}"
        echo -e "  ${DIM}(OAuth providers will need manual token entry)${RESET}"
    fi

    # Walk through providers
    local count=0
    local total=${#PROVIDERS[@]}
    for provider in "${PROVIDERS[@]}"; do
        count=$((count + 1))
        echo ""
        echo -e "${DIM}[$count/$total]${RESET}"
        "provider_${provider}_prompt"
    done

    # Check at least one provider configured
    local any_configured=0
    for provider in "${PROVIDERS[@]}"; do
        local var_name
        var_name=$(echo "${provider}" | tr '[:lower:]' '[:upper:]')_CONFIGURED
        if [ "${!var_name}" -eq 1 ]; then
            any_configured=1
            break
        fi
    done

    if [ "$any_configured" -eq 0 ]; then
        echo ""
        warn "No providers configured. The gateway will blind-tunnel all traffic."
        if ! prompt_yn "Continue anyway?" "N"; then
            echo "Aborted."
            exit 1
        fi
    fi

    # Generate files
    echo ""
    step "Generating configuration"

    generate_gateway_yaml
    echo -e "  ${DIM}→ $GATEWAY_YAML${RESET}"

    generate_env_agent
    local var_count
    var_count=$(grep -c '=' "$ENV_AGENT" 2>/dev/null || echo "0")
    echo -e "  ${DIM}→ $ENV_AGENT ($var_count env vars)${RESET}"

    generate_docker_env
    echo -e "  ${DIM}→ $DOCKER_ENV${RESET}"

    info "Configuration generated"

    # Gitignore check
    check_gitignore

    # Summary
    echo ""
    step "Ready!"
    echo -e "  Start:     ${BOLD}.devcontainer/truman.sh start${RESET}"
    echo -e "  Run pi:    ${BOLD}.devcontainer/truman.sh run-pi${RESET}"
    echo -e "  Status:    ${BOLD}.devcontainer/truman.sh status${RESET}"
    echo ""
}

cmd_validate() {
    # Non-interactive validation gate (for devcontainer.json initializeCommand)
    if ! validate_config; then
        exit 1
    fi
    info "Truman configuration OK"
}

cmd_start() {
    if ! validate_config; then
        exit 1
    fi
    info "Starting devcontainer..."
    devcontainer up --workspace-folder "$PROJECT_ROOT"
}

cmd_run_pi() {
    if ! validate_config; then
        exit 1
    fi
    devcontainer exec --workspace-folder "$PROJECT_ROOT" pi "$@"
}

cmd_stop() {
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" down
}

cmd_status() {
    echo ""
    echo -e "${BOLD}Truman Status${RESET}"
    echo -e "${DIM}─────────────${RESET}"

    # Config files
    echo ""
    echo -e "${BOLD}Configuration${RESET}"
    for f in "$GATEWAY_YAML" "$ENV_AGENT" "$DOCKER_ENV"; do
        local name
        name=$(basename "$f")
        if [ -f "$f" ] && [ -s "$f" ]; then
            echo -e "  $name  ${GREEN}✅${RESET}"
        elif [ -f "$f" ]; then
            echo -e "  $name  ${YELLOW}⚠️  empty${RESET}"
        else
            echo -e "  $name  ${RED}❌ missing${RESET}"
        fi
    done

    # Providers from gateway.yaml
    if [ -f "$GATEWAY_YAML" ]; then
        echo ""
        echo -e "${BOLD}Providers${RESET}"

        # Parse hostnames and types from gateway.yaml
        local current_host="" current_type=""
        while IFS= read -r line; do
            # Top-level key (hostname)
            if [[ "$line" =~ ^[a-zA-Z] ]] && [[ "$line" == *: ]]; then
                # Print previous host if any
                if [ -n "$current_host" ]; then
                    _print_provider_status "$current_host" "$current_type"
                fi
                current_host="${line%:}"
                current_type=""
            fi
            if [[ "$line" =~ ^[[:space:]]+type:[[:space:]]+(.*) ]]; then
                current_type="${BASH_REMATCH[1]}"
            fi
        done < "$GATEWAY_YAML"
        # Print last host
        if [ -n "$current_host" ]; then
            _print_provider_status "$current_host" "$current_type"
        fi

        # Token file status
        if grep -q 'token_file:' "$GATEWAY_YAML" 2>/dev/null; then
            echo ""
            echo -e "${BOLD}Shared Token File${RESET}"
            if [ -f "$DOCKER_ENV" ] && grep -q 'PI_AUTH_JSON=' "$DOCKER_ENV"; then
                local auth_path
                auth_path=$(grep 'PI_AUTH_JSON=' "$DOCKER_ENV" | cut -d= -f2)
                if [ -f "$auth_path" ]; then
                    echo -e "  $auth_path  ${GREEN}✅${RESET}"
                    # Show expiry for each token_file_key
                    local key
                    key=$(grep 'token_file_key:' "$GATEWAY_YAML" | head -1 | sed 's/.*token_file_key:[[:space:]]*//' | tr -d '"')
                    if [ -n "$key" ]; then
                        local expiry
                        PI_AUTH_JSON_PATH="$auth_path"
                        expiry=$(auth_json_expiry "$key")
                        echo -e "  ${DIM}$key: $expiry${RESET}"
                    fi
                else
                    echo -e "  $auth_path  ${RED}❌ not found${RESET}"
                fi
            else
                echo -e "  ${RED}❌ PI_AUTH_JSON not set in .env${RESET}"
            fi
        fi
    fi

    # Container status
    echo ""
    echo -e "${BOLD}Containers${RESET}"
    local compose_file="$SCRIPT_DIR/docker-compose.yml"
    if [ -f "$compose_file" ]; then
        local services
        services=$(docker compose -f "$compose_file" ps --format '{{.Service}}\t{{.State}}\t{{.Health}}' 2>/dev/null)
        if [ -n "$services" ]; then
            while IFS=$'\t' read -r svc state health; do
                local status_icon
                if [[ "$state" == *"running"* ]]; then
                    if [[ "$health" == "healthy" ]]; then
                        status_icon="${GREEN}● healthy${RESET}"
                    elif [[ "$health" == "unhealthy" ]]; then
                        status_icon="${RED}● unhealthy${RESET}"
                    else
                        status_icon="${GREEN}● running${RESET}"
                    fi
                else
                    status_icon="${DIM}○ $state${RESET}"
                fi
                echo -e "  $svc  $status_icon"
            done <<< "$services"
        else
            echo -e "  ${DIM}Not running${RESET}"
        fi
    fi
    echo ""
}

_print_provider_status() {
    local host="$1" type="$2"
    echo -e "  $host  ${DIM}($type)${RESET}  ${GREEN}✅${RESET}"
}

cmd_help() {
    echo ""
    echo -e "${BOLD}truman.sh${RESET} — Truman devcontainer setup & lifecycle manager"
    echo ""
    echo -e "${BOLD}Usage:${RESET}"
    echo "  .devcontainer/truman.sh <command> [args]"
    echo ""
    echo -e "${BOLD}Commands:${RESET}"
    echo "  init       Interactive setup wizard (creates gateway.yaml, .env.agent)"
    echo "  start      Validate config and start the devcontainer"
    echo "  run-pi     Run pi inside the container (args forwarded)"
    echo "  stop       Stop all containers"
    echo "  status     Show configuration and container status"
    echo "  validate   Check configuration (non-interactive)"
    echo "  help       Show this help"
    echo ""
    echo -e "${BOLD}Quick start:${RESET}"
    echo "  .devcontainer/truman.sh init"
    echo "  .devcontainer/truman.sh start"
    echo "  .devcontainer/truman.sh run-pi"
    echo ""
}

# ── Main dispatch ────────────────────────────────────────────────────

main() {
    local cmd="${1:-help}"
    shift || true

    case "$cmd" in
        init)     cmd_init ;;
        validate) cmd_validate ;;
        start)    cmd_start ;;
        run-pi)   cmd_run_pi "$@" ;;
        stop)     cmd_stop ;;
        status)   cmd_status ;;
        help|-h|--help) cmd_help ;;
        *)
            err "Unknown command: $cmd"
            cmd_help
            exit 1
            ;;
    esac
}

main "$@"
