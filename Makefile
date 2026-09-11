# Common tasks. `make help` lists them.
# Targets stay thin wrappers around real commands so nothing is hidden.

SERVICE := services/plc-sim
COMPOSE := docker compose
IP = $(shell docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(1))

.DEFAULT_GOAL := help
.PHONY: help setup lint fmt test check run up down logs ps verify build scan clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies
	cd $(SERVICE) && uv sync

lint: ## Run ruff
	cd $(SERVICE) && uv run ruff check .

fmt: ## Format the code
	cd $(SERVICE) && uv run ruff format .

test: ## Run the test suite
	cd $(SERVICE) && uv run pytest -q

check: lint test ## Everything CI runs locally

run: ## Run one chamber in the foreground
	cd $(SERVICE) && uv run python -m plc_sim.main

up: ## Start all chambers
	$(COMPOSE) up --build -d --wait --wait-timeout 40
	@$(COMPOSE) ps

down: ## Stop all chambers
	$(COMPOSE) down

logs: ## Follow container logs
	$(COMPOSE) logs -f --tail=100

ps: ## Show container status
	$(COMPOSE) ps

verify: ## End-to-end check against 127.0.0.1:5020
	cd $(SERVICE) && uv run python -m plc_sim.verify --host 127.0.0.1 --port 5020

build: ## Build the image
	docker build -t plc-sim:local $(SERVICE)

scan: ## Check nothing sensitive is tracked
	@echo "==> tracked binaries and credentials"
	@! git ls-files | grep -iE '\.(db|sqlite3?|pem|crt|key)$$' || (echo "FOUND" && exit 1)
	@echo "==> customer identifiers"
	@! git grep -rIn -iE 'mitsubishi|FX5[A-Z]|rubber_dryer_room' -- . ':!.github/workflows/ci.yml' ':!Makefile' ':!scripts/dev-check.sh' || (echo "FOUND" && exit 1)
	@command -v gitleaks >/dev/null && gitleaks detect --source . --no-banner || echo "(gitleaks not installed, skipped)"
	@echo "clean"

clean: ## Remove caches and build artefacts
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) \
		-exec rm -rf {} + 2>/dev/null || true

poll: ## Poll a chamber: make poll CH=chamber-03
	mbpoll -m tcp -a 1 -r 221 -c 12 -t 4 -l 1000 $(call IP,$(CH)) -p 5020
