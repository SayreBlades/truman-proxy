.PHONY: help build run prompt rpc shell shell-gateway logs clean sync-token

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build: ## Build all container images
	docker compose build gateway
	docker compose build agent

run: build ## Interactive CLI/TUI
	docker compose run --rm agent

prompt: build ## Single prompt: make prompt p="tell me a joke"
	docker compose run --rm agent pi -p "$(p)"

rpc: build ## RPC mode (JSONL on stdin/stdout)
	docker compose run --rm -T agent pi --mode rpc

shell: build ## Shell into the agent container (for debugging)
	docker compose run --rm agent bash

shell-gateway: build ## Shell into the gateway container (for debugging)
	docker compose run --rm gateway bash

logs: ## Show gateway logs
	docker compose logs -f gateway

sync-token: ## Sync Anthropic OAuth refresh token from host pi into .env
	@AUTH_FILE="$$HOME/.pi/agent/auth.json"; \
	if [ ! -f "$$AUTH_FILE" ]; then \
		echo "Error: $$AUTH_FILE not found. Run 'pi' and '/login' first." >&2; exit 1; \
	fi; \
	REFRESH=$$(python3 -c "import json; print(json.load(open('$$AUTH_FILE'))['anthropic']['refresh'])" 2>/dev/null); \
	if [ -z "$$REFRESH" ]; then \
		echo "Error: No Anthropic OAuth credentials in $$AUTH_FILE. Run 'pi' and '/login' first." >&2; exit 1; \
	fi; \
	if [ -f .env ] && grep -q '^ANTHROPIC_REFRESH_TOKEN=' .env; then \
		sed -i '' "s|^ANTHROPIC_REFRESH_TOKEN=.*|ANTHROPIC_REFRESH_TOKEN=$$REFRESH|" .env; \
	elif [ -f .env ]; then \
		echo "ANTHROPIC_REFRESH_TOKEN=$$REFRESH" >> .env; \
	else \
		echo "ANTHROPIC_REFRESH_TOKEN=$$REFRESH" > .env; \
	fi; \
	echo "✓ Synced ANTHROPIC_REFRESH_TOKEN into .env"

clean: ## Remove containers, volumes, and images
	docker compose down -v --rmi local
