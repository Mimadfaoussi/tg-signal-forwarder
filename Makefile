SHELL := /bin/bash

ENV_FILE := .env
COMPOSE := docker compose

.PHONY: help
help: ## Show this help message
	@echo "Usage: make <target>"
	@echo ""
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help

.PHONY: init
init: ## Copy .env.example to .env and create data/session/{listener,publisher} with mode 700
	@if [ ! -f $(ENV_FILE) ]; then \
		cp .env.example $(ENV_FILE); \
		echo "Created $(ENV_FILE) from .env.example — fill in the required values."; \
	else \
		echo "$(ENV_FILE) already exists, leaving it untouched."; \
	fi
	@mkdir -p data/session/listener data/session/publisher
	@chmod 700 data/session data/session/listener data/session/publisher
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit .env and fill in TG_API_ID, TG_API_HASH, TARGET_CHAT."
	@echo "  2. Run 'make build' to build the images."
	@echo "  3. Run 'make login' to authorize both Telegram user sessions (two codes)."
	@echo "  4. Fill in SOURCE_CHAT in .env using the dialog list printed by 'make login'."
	@echo "  5. Run 'make up' to start the stack."

.PHONY: build
build: ## Build all service images
	$(COMPOSE) build

.PHONY: login
login: ## Run login-listener then login-publisher (the operator gets two codes)
	$(MAKE) login-listener
	$(MAKE) login-publisher

.PHONY: login-listener
login-listener: ## Interactively log in the listener's Telegram user session
	$(COMPOSE) up -d redis
	$(COMPOSE) run --rm -it listener python -m listener.login

.PHONY: login-publisher
login-publisher: ## Interactively log in the publisher's session and check TARGET_CHAT
	$(COMPOSE) up -d redis postgres
	$(COMPOSE) run --rm -it publisher python -m publisher.login

.PHONY: check-target
check-target: ## Run only the publisher's target preflight check and exit
	$(COMPOSE) run --rm --no-deps publisher python -m publisher.sender

.PHONY: up
up: ## Start the stack in the background
	$(COMPOSE) up -d
	$(COMPOSE) ps

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: restart
restart: ## Restart the stack
	$(MAKE) down
	$(MAKE) up

.PHONY: logs
logs: ## Tail logs (optionally: make logs s=<service>)
	$(COMPOSE) logs -f --tail=100 $(s)

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

.PHONY: test
test: ## Run unit tests in a throwaway container (no Telegram credentials needed)
	docker build -f services/publisher/Dockerfile --target test -t tg-forwarder-test .
	docker run --rm -v $(CURDIR)/tests:/build/tests:ro tg-forwarder-test \
		/opt/venv/bin/python -m pytest tests/unit -v

.PHONY: test-integration
test-integration: ## Run integration tests against real redis/postgres, Telegram mocked
	docker build -f services/publisher/Dockerfile --target test -t tg-forwarder-test .
	$(COMPOSE) up -d redis postgres
	docker run --rm --network $$(basename $(CURDIR))_internal \
		-v $(CURDIR)/tests:/build/tests:ro \
		--env REDIS_URL=redis://redis:6379/0 \
		--env DATABASE_URL=postgresql://forwarder:change-me@postgres:5432/forwarder \
		tg-forwarder-test /opt/venv/bin/python -m pytest tests/integration -v \
		|| ($(COMPOSE) stop redis postgres && exit 1)
	$(COMPOSE) stop redis postgres

.PHONY: lint
lint: ## Run ruff check, ruff format --check and mypy --strict on shared/
	ruff check .
	ruff format --check .
	mypy shared/

.PHONY: fmt
fmt: ## Auto-format and auto-fix lint issues
	ruff format .
	ruff check --fix .

.PHONY: dry-run
dry-run: ## Start the stack with DRY_RUN=true
	DRY_RUN=true $(COMPOSE) up -d

.PHONY: db-shell
db-shell: ## Open a psql shell into postgres
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-forwarder} -d $${POSTGRES_DB:-forwarder}

.PHONY: redis-cli
redis-cli: ## Open a redis-cli shell into redis
	$(COMPOSE) exec redis redis-cli

.PHONY: stats
stats: ## Print signal counts by status and stream lengths
	@$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-forwarder} -d $${POSTGRES_DB:-forwarder} \
		-c "SELECT status, count(*) FROM signals GROUP BY status;"
	@$(COMPOSE) exec redis redis-cli XLEN signals.raw
	@$(COMPOSE) exec redis redis-cli XLEN signals.dead

.PHONY: replay-dead
replay-dead: ## Move every entry from signals.dead back to signals.raw
	@$(COMPOSE) exec redis redis-cli --no-raw eval \
		"local n = 0 while true do local e = redis.call('XRANGE', 'signals.dead', '-', '+', 'COUNT', 1) if #e == 0 then break end local id = e[1][1] local fields = e[1][2] redis.call('XADD', 'signals.raw', '*', unpack(fields)) redis.call('XDEL', 'signals.dead', id) n = n + 1 end return n" 0

.PHONY: backup
backup: ## Dump postgres and copy both session files (mode 600) into backups/
	@mkdir -p backups
	@stamp=$$(date +%Y%m%d-%H%M); \
	$(COMPOSE) exec -T postgres pg_dump -U $${POSTGRES_USER:-forwarder} $${POSTGRES_DB:-forwarder} | gzip > backups/forwarder-$$stamp.sql.gz; \
	cp data/session/listener/listener.session backups/listener-$$stamp.session 2>/dev/null || true; \
	cp data/session/publisher/publisher.session backups/publisher-$$stamp.session 2>/dev/null || true; \
	chmod 600 backups/listener-$$stamp.session backups/publisher-$$stamp.session 2>/dev/null || true; \
	echo "Backup written to backups/forwarder-$$stamp.sql.gz"

.PHONY: clean
clean: ## Stop the stack and remove volumes (asks for confirmation)
	@read -p "This will remove all containers and volumes. Type 'yes' to continue: " confirm; \
	if [ "$$confirm" = "yes" ]; then \
		$(COMPOSE) down -v; \
	else \
		echo "Aborted."; \
	fi
