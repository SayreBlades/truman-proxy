.PHONY: help build build-gateway build-agent publish \
       clean

IMAGE_PREFIX := ghcr.io/sayreblades
GATEWAY_IMAGE := $(IMAGE_PREFIX)/truman-gateway
AGENT_IMAGE := $(IMAGE_PREFIX)/truman-agent
VERSION := latest

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Image Build ──────────────────────────────────────────────────

build: build-gateway build-agent ## Build all container images

build-gateway: ## Build gateway image
	docker build -t $(GATEWAY_IMAGE):$(VERSION) images/gateway/

build-agent: ## Build agent image
	docker build -t $(AGENT_IMAGE):$(VERSION) images/agent/

# ── Publish ──────────────────────────────────────────────────────

publish: build ## Build and push images to ghcr.io
	docker push $(GATEWAY_IMAGE):$(VERSION)
	docker push $(AGENT_IMAGE):$(VERSION)

# ── Utilities ────────────────────────────────────────────────────

clean: ## Remove locally-built truman images
	docker rmi $(GATEWAY_IMAGE):$(VERSION) 2>/dev/null || true
	docker rmi $(AGENT_IMAGE):$(VERSION) 2>/dev/null || true
	@echo "✅ Cleaned truman images"
