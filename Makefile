# Radio Broadcast Analysis -- developer and operator entry points.
#
# `make help` lists everything. Targets are thin wrappers around the scripts and
# tools that do the real work, so nothing here is the only place a command
# exists -- CI and the deployment host run the same scripts directly.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE      ?= docker compose
COMPOSE_DEV  := $(COMPOSE) -f compose.yaml -f compose.dev.yaml
COMPOSE_PROD := $(COMPOSE) -f compose.yaml -f compose.prod.yaml
PYTHON       ?= python
MODEL_ROOT   ?= ./var/models

# Development points env_file at the committed placeholder values so a fresh
# clone runs without touching /etc or holding any credential.
export RADIO_ENV_DIR ?= ./deploy/dev

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# --- quality -----------------------------------------------------------------

.PHONY: test
test: ## Run the whole test suite
	$(PYTHON) -m pytest -q

.PHONY: test-unit
test-unit: ## Run unit tests only
	$(PYTHON) -m pytest tests/unit -q

.PHONY: test-integration
test-integration: ## Run end-to-end pipeline tests
	$(PYTHON) -m pytest tests/integration -q

.PHONY: test-load
test-load: ## Run the 1,000-station synthetic load test
	$(PYTHON) -m pytest tests/load -q -s -m load

.PHONY: lint
lint: ## Lint with ruff
	$(PYTHON) -m ruff check app tests tools scripts

.PHONY: check
check: lint test compose-check ## Everything CI runs

# --- compose -----------------------------------------------------------------

.PHONY: compose-check
compose-check: ## Validate Compose files and audit the security posture
	bash scripts/compose-check.sh prod
	bash scripts/compose-check.sh dev

.PHONY: build
build: ## Build all images for this host's architecture
	$(COMPOSE_DEV) --profile core --profile pipeline build

.PHONY: build-multiarch
build-multiarch: ## Build linux/amd64 + linux/arm64 images (needs buildx)
	docker buildx build --platform linux/amd64,linux/arm64 \
		-f docker/api.Dockerfile -t $${RADIO_API_IMAGE:-radio-api:local} .
	docker buildx build --platform linux/amd64,linux/arm64 \
		-f docker/pipeline.Dockerfile -t $${RADIO_PIPELINE_IMAGE:-radio-pipeline:local} .
	docker buildx build --platform linux/amd64,linux/arm64 \
		-f docker/llm.Dockerfile -t $${RADIO_LLM_IMAGE:-radio-llm:local} .

.PHONY: dev-up
dev-up: dev-dirs ## Start the dev stack (no models, no AWS, fake ASR)
	$(COMPOSE_DEV) --profile core --profile pipeline up

.PHONY: dev-down
dev-down: ## Stop the dev stack
	$(COMPOSE_DEV) --profile core --profile pipeline --profile llm down

.PHONY: dev-logs
dev-logs: ## Tail dev stack logs
	$(COMPOSE_DEV) --profile core --profile pipeline logs -f

.PHONY: dev-dirs
dev-dirs: ## Create the local ./var directories the dev stack mounts
	mkdir -p var/database var/spool var/models var/evidence var/logs var/backups

.PHONY: prod-config
prod-config: ## Print the fully resolved production configuration
	$(COMPOSE_PROD) --profile core --profile pipeline --profile llm config

.PHONY: prod-up
prod-up: ## Start the production stack (expects /etc/radio-broadcast-analysis)
	RADIO_ENV_DIR=/etc/radio-broadcast-analysis \
		$(COMPOSE_PROD) --profile core --profile pipeline up -d

.PHONY: prod-down
prod-down: ## Stop the production stack
	RADIO_ENV_DIR=/etc/radio-broadcast-analysis \
		$(COMPOSE_PROD) --profile core --profile pipeline --profile llm down

.PHONY: ps
ps: ## Show container status
	$(COMPOSE_PROD) --profile core --profile pipeline --profile llm ps

# --- models ------------------------------------------------------------------

.PHONY: models
models: ## Download the pinned models (~1.1 GB; explicit, never automatic)
	$(PYTHON) scripts/download-models.py --root $(MODEL_ROOT)

.PHONY: models-asr
models-asr: ## Download only the ASR model
	$(PYTHON) scripts/download-models.py --root $(MODEL_ROOT) --role asr --role vad

.PHONY: models-verify
models-verify: ## Verify local models against models.lock.json
	$(PYTHON) scripts/verify-models.py --root $(MODEL_ROOT)

.PHONY: models-plan
models-plan: ## Show what would be downloaded, fetching nothing
	$(PYTHON) scripts/download-models.py --root $(MODEL_ROOT) --dry-run

# --- operations --------------------------------------------------------------

.PHONY: backup
backup: ## Take a consistent SQLite backup
	bash scripts/backup-sqlite.sh

.PHONY: smoke
smoke: ## Run the smoke test against a running stack
	bash scripts/smoke-test.sh

.PHONY: container-smoke
container-smoke: ## Build and smoke-test the API container in isolation
	bash scripts/container-smoke-test.sh

.PHONY: deploy-dry-run
deploy-dry-run: ## Validate a deployment without building or starting anything
	@test -n "$(COMMIT)" || { echo "usage: make deploy-dry-run COMMIT=<40-hex-sha>"; exit 64; }
	bash scripts/deploy-compose.sh --commit "$(COMMIT)" --stage $(or $(STAGE),api) --dry-run

.PHONY: rollback-dry-run
rollback-dry-run: ## Validate a rollback without changing containers
	bash scripts/rollback-compose.sh --previous --dry-run

.PHONY: migrate-check
migrate-check: ## Report the schema version of a local database
	$(PYTHON) -m app.cli.migrate_database --check-only

.PHONY: secret-scan
secret-scan: ## Fail if a credential looks committed
	bash scripts/secret-scan.sh

.PHONY: clean
clean: ## Remove local caches and build artefacts (keeps ./var)
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache prod-config.tmp.yaml
