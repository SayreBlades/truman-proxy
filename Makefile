.PHONY: help build run prompt rpc shell clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build: ## Build the container image
	docker compose build

run: build ## Interactive CLI/TUI
	docker compose run --rm agent

prompt: build ## Single prompt: make prompt p="tell me a joke"
	docker compose run --rm agent pi -p "$(p)"

rpc: build ## RPC mode (JSONL on stdin/stdout)
	docker compose run --rm -T agent pi --mode rpc

shell: build ## Shell into the container (for debugging)
	docker compose run --rm agent bash

clean: ## Remove containers, volumes, and image
	docker compose down -v --rmi local
