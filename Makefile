.PHONY: help build run prompt rpc shell shell-gateway logs clean

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

clean: ## Remove containers, volumes, and images
	docker compose down -v --rmi local
