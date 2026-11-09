# HiHydroSoil v2.0 extracted GeoTIFFs -> Icechunk Zarr.
# Local by default. ACCOUNT=chill explicitly targets Source Cooperative.
STORE        ?= ./hihydrosoil_store_local/v0.1.0.icechunk
ACCOUNT      ?=
SOURCE       ?= data
WORK_DIR     ?= work
PROPERTIES   ?=
BBOX         ?=
WORKERS      ?= 4
COMMIT_EVERY ?= 8
SAMPLES      ?= 8
SEED         ?= 0
CREDS_FILE   ?=
NO_CHECKSUMS ?=
OVERWRITE    ?=
REBUILD      ?=
STRUCTURE_ONLY ?=
GC_HOURS     ?= 24
SUFFIX       ?=

CLI = uv run hihydrosoil
ifeq ($(ACCOUNT),)
STORE_FLAGS = --store "$(STORE)"
else
STORE_FLAGS = --source-coop-account "$(ACCOUNT)"
endif
CREDS_FLAG = $(if $(CREDS_FILE),--credentials-file "$(CREDS_FILE)")
STORE_FLAGS += $(CREDS_FLAG)
PROPERTIES_FLAG = $(if $(PROPERTIES),--properties "$(PROPERTIES)")
BBOX_FLAG = $(if $(BBOX),--bbox "$(BBOX)")

.DEFAULT_GOAL := help
.PHONY: help setup test lint show-config inspect-source init-store materialize overviews status validate release \
	info garbage-collect publish-readme upload-audit clean-local-store clean-work clean-remote-store

help: ## Show commands and current settings
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'
	@echo "STORE=$(STORE) ACCOUNT=$(ACCOUNT) SOURCE=$(SOURCE) WORK_DIR=$(WORK_DIR)"
	@echo "PROPERTIES=$(PROPERTIES) BBOX=$(BBOX) WORKERS=$(WORKERS) COMMIT_EVERY=$(COMMIT_EVERY)"

setup: ## Install the locked environment
	uv sync --locked

test: ## Run synthetic tests on all logical CPU cores, without source data or network
	uv run pytest -q -n logical

lint: ## Check code and formatting with Ruff
	uv run ruff check --no-cache src tests
	uv run ruff format --check --no-cache src tests

show-config: ## Print the frozen structural specification
	$(CLI) show-config

inspect-source: ## Inspect extracted TIFFs; inventory and fingerprint available layers
	$(CLI) inspect-source --source "$(SOURCE)" --work-dir "$(WORK_DIR)" $(PROPERTIES_FLAG) $(if $(NO_CHECKSUMS),--no-checksums)

init-store: ## Create or additively extend native arrays and overview metadata
	$(CLI) init-store $(STORE_FLAGS) $(PROPERTIES_FLAG)

materialize: ## Fill native arrays in shard-aligned, resumable batches
	$(CLI) materialize $(STORE_FLAGS) --source "$(SOURCE)" --work-dir "$(WORK_DIR)" $(PROPERTIES_FLAG) $(BBOX_FLAG) \
		--workers $(WORKERS) --commit-every $(COMMIT_EVERY) $(if $(OVERWRITE),--overwrite) $(if $(NO_CHECKSUMS),--no-checksums)

overviews: ## Build checkpointed mean/mode overviews from materialized native data
	$(CLI) overviews $(STORE_FLAGS) $(PROPERTIES_FLAG) --workers $(WORKERS) \
		--commit-every $(COMMIT_EVERY) $(if $(REBUILD),--rebuild)

status: ## Show native and overview completion/progress
	$(CLI) status $(STORE_FLAGS)

validate: ## Verify structure, source pixels and independent overview reductions
	$(CLI) validate $(STORE_FLAGS) $(PROPERTIES_FLAG) --work-dir "$(WORK_DIR)" \
		--samples $(SAMPLES) --seed $(SEED) --workers $(WORKERS) $(if $(STRUCTURE_ONLY),--structure-only)

release: ## Tag a complete store validated at its current snapshot
	$(CLI) release $(STORE_FLAGS) --work-dir "$(WORK_DIR)" $(if $(SUFFIX),--suffix="$(SUFFIX)")

info: ## Show the array tree and immutable tags
	$(CLI) info $(STORE_FLAGS)

garbage-collect: ## Reclaim unreferenced objects; run only with no active writers
	$(CLI) garbage-collect $(STORE_FLAGS) --older-than-hours $(GC_HOURS)

publish-readme: ## Upload the product landing-page README
	$(CLI) publish-readme --source-coop-account "$(or $(ACCOUNT),chill)" $(CREDS_FLAG)

upload-audit: ## Upload manifests and reports to audit/{source_version}/
	$(CLI) upload-audit --source-coop-account "$(or $(ACCOUNT),chill)" $(CREDS_FLAG) --work-dir "$(WORK_DIR)"

clean-local-store: ## Remove the local store, leaving downloaded sources untouched
	@test -n "$(STORE)" && test "$(STORE)" != / && test "$(STORE)" != .
	rm -rf -- "$(STORE)"

clean-work: ## Remove pipeline manifests and reports
	@test -n "$(WORK_DIR)" && test "$(WORK_DIR)" != / && test "$(WORK_DIR)" != .
	rm -rf -- "$(WORK_DIR)"

clean-remote-store: ## DESTRUCTIVE remote-store removal; confirms twice in an interactive terminal
	$(CLI) clean-remote-store --source-coop-account "$(or $(ACCOUNT),chill)" $(CREDS_FLAG) --workers $(WORKERS)
