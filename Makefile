# The platform, driven from a product repo:  make up PRODUCT=../my-product
#
# PRODUCT is a PATH, not a name. This Makefile contains no product identifier,
# which is the property that makes "a second product can use this unchanged" a
# fact rather than an aspiration.
SHELL := /bin/bash
PRODUCT ?= ./product
SOURCES ?= ../contoso-sources
# NAMED FOR THIS CELL, not for the platform this Makefile was copied from. It
# arrived here as `airflow-fabric`, which would have been the Fabric Airflow 3
# platform's project name too -- two stacks sharing one project name do not
# collide loudly, they ADOPT each other's containers, and `make down` in one
# repository stops the other's.
PROJECT ?= airflow-databricks
export PRODUCT_ABS := $(abspath $(PRODUCT))
export SOURCES_ABS := $(abspath $(SOURCES))
export PRODUCT_NAME := $(notdir $(PRODUCT_ABS))
include versions.env
export

FRAGMENT := .sources.generated.yml
# Where Airflow 3's simple auth manager writes the credential it generates.
PASSWORD_FILE := /opt/airflow/simple_auth_manager_passwords.json.generated
COMPOSE := PRODUCT=$(PRODUCT_ABS) PRODUCT_NAME=$(PRODUCT_NAME) SOURCES=$(SOURCES_ABS) PWD=$(CURDIR) \
           docker compose -p $(PROJECT) -f docker-compose.yml -f $(FRAGMENT)

.PHONY: help up down logs connections creds doctor sources trigger unpause prepare token verify test lint witness
help: ## This list
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t20

up: doctor sources prepare ## Build the worker from the product's pyproject.toml and start the stack
	@echo "platform: product = $(PRODUCT_ABS)"
	@echo "platform: sources = $(SOURCES_ABS)"
	$(COMPOSE) up --build -d
	@$(MAKE) --no-print-directory token
	@echo "platform: Airflow on http://localhost:$${AIRFLOW_PORT:-18082}"

token: ## Put the workspace credential where the product can read it
# A PRODUCT DOES NOT REACH INTO A PLATFORM -- it is handed a credential. The
# emulator mints a workspace PAT at start-up; this copies it into the product
# directory, which is the only place a task can read it from without knowing
# what started the workspace.
#
# AFTER `up`, not before: the file does not exist until the databricks container
# is healthy. Run as part of `up` for that reason, and separately available for
# a stack that was already running.
#
# On a real workspace there is nothing to copy -- DATABRICKS_TOKEN is exported
# and the product never looks for this file.
	@mkdir -p "$(PRODUCT_ABS)/data"
	@$(COMPOSE) cp databricks:/data/admin.pat "$(PRODUCT_ABS)/data/admin.pat" >/dev/null
	@test -s "$(PRODUCT_ABS)/data/admin.pat" || { \
	  echo "the emulator produced no workspace token -- every task would fail"; \
	  echo "authenticating, which reads as a broken product rather than a"; \
	  echo "missing credential"; exit 1; }
	@echo "platform: workspace token -> $(PRODUCT_NAME)/data/admin.pat"

prepare: ## Run the product's own pre-start step, if it declares one
# THE PLATFORM DOES NOT KNOW WHAT PREPARING MEANS. It asks the product whether
# it has a `prepare` target and runs it if so -- exactly as it asks the product
# for its dependencies without learning what they are.
#
# This exists because a product can have work that must happen BEFORE the worker
# image is built and the DAGs are parsed, and only the product knows what.
# contoso-data-product-databricks-airflow3 builds a dbt manifest here: cosmos
# renders its gold graph at DAG-parse time, and without the manifest it would
# silently render a graph missing every ODCS contract.
#
# A product with nothing to do says nothing and this is a no-op. A product whose
# prepare FAILS stops `up`, which is the point -- starting a stack whose DAGs
# cannot render is a slower way to learn the same thing.
	@if [ -f "$(PRODUCT_ABS)/Makefile" ] && \
	    $(MAKE) -C "$(PRODUCT_ABS)" -n prepare >/dev/null 2>&1; then \
	  echo "platform: $(PRODUCT_NAME) declares a prepare step -- running it"; \
	  $(MAKE) -C "$(PRODUCT_ABS)" prepare; \
	else \
	  echo "platform: $(PRODUCT_NAME) declares no prepare step"; \
	fi

sources: ## Generate the compose fragment for the vendors a sources repo declares
	@test -f "$(SOURCES_ABS)/sources.yaml" || { \
	  echo "no sources.yaml at $(SOURCES_ABS) -- the product's vendors cannot be started"; exit 1; }
	@python3 scripts/sources.py "$(SOURCES_ABS)/sources.yaml" "$(SOURCES_ABS)" > $(FRAGMENT)
	@echo "platform: $$(python3 -c "import json;print(len(json.load(open('$(FRAGMENT)'))['services']))") vendor(s) declared"

trigger: ## Trigger a DAG and return immediately:  make trigger DAG=contoso_daily
	$(COMPOSE) exec -T airflow airflow dags trigger $(DAG)

unpause: ## Let a DAG be scheduled:  make unpause DAG=contoso_daily
# ITS OWN TARGET so `verify` can point at one short command instead of printing
# the whole expanded compose invocation, and so that letting a DAG schedule
# itself stays a thing someone chose to do.
	@test -n "$(DAG)" || { echo "usage: make unpause DAG=<dag_id>"; exit 2; }
	$(COMPOSE) exec -T airflow airflow dags unpause $(DAG)

# How long `verify` waits for a run, and how often it looks.
VERIFY_TIMEOUT ?= 3600
VERIFY_POLL ?= 15
# How long it waits for the DAG to EXIST before saying it does not. A stack
# that was just brought up has an empty metadata database, and the dag
# processor's first scan takes tens of seconds; asking sooner is asking early,
# not asking about a missing DAG.
VERIFY_PARSE_WAIT ?= 300

verify: ## Run a DAG and FAIL if it fails:  make verify DAG=contoso_daily
# WHAT `trigger` ONLY LOOKED LIKE IT DID. Its help said "and wait" and it
# returned the moment the run was queued, so NOTHING IN THIS REPOSITORY EVER
# EXITED NON-ZERO BECAUSE A PIPELINE FAILED. Every green this platform reported
# rested on a person reading run state by hand afterwards. DoD 3 asks for green
# THROUGH the orchestrator; a command that cannot go red does not establish it.
#
# IT WATCHES ITS OWN RUN, by an explicit --run-id. A scheduled run of the same
# DAG can be in flight at the same moment -- that happened while this platform
# was being witnessed -- and "the most recent run" would then be the other one,
# reporting a verdict for a pipeline this command did not start.
#
# IT DOES NOT UNPAUSE, deliberately. Unpausing changes the DAG's schedule and
# starts a catch-up run ALONGSIDE this one: two runs writing the same tables,
# which is how a witness stops being one. Refusing with the command printed is
# the smaller surprise.
	@test -n "$(DAG)" || { echo "usage: make verify DAG=<dag_id>"; exit 2; }
# IT WAITS FOR THE DAG TO EXIST, and that is not politeness. `make up` recreates
# the metadata database, so for the first tens of seconds afterwards EVERY DAG
# is absent -- and this check used to call that "no DAG called X", a hard exit 1
# that reads as a broken DAG. It cost three witness runs in one evening. The
# distinction it now makes is the one that matters: an IMPORT ERROR is reported
# immediately, because waiting cannot fix a traceback; absence alone is retried
# until VERIFY_PARSE_WAIT, because the scan may simply not have happened.
	@waited=0; \
	  while :; do \
	    paused=$$($(COMPOSE) exec -T airflow airflow dags list -o plain 2>/dev/null \
	      | awk -v d="$(DAG)" '$$1 == d {print $$4}'); \
	    test -n "$$paused" && break; \
	    errs=$$($(COMPOSE) exec -T airflow airflow dags list-import-errors -o plain 2>/dev/null \
	      | grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' \
	      | grep -v '^No data found' | grep -v '^filepath' | head -5); \
	    test -z "$$errs" || { \
	      echo "a DAG file cannot be parsed, so $(DAG) will never appear:"; \
	      echo "$$errs"; exit 1; }; \
	    test $$waited -lt $(VERIFY_PARSE_WAIT) || { \
	      echo "no DAG called $(DAG) after $(VERIFY_PARSE_WAIT)s, and no import"; \
	      echo "error to explain it -- check the dag bundle is mounted:  make logs"; \
	      exit 1; }; \
	    test $$waited -gt 0 || echo "platform: waiting for $(DAG) to be scanned"; \
	    sleep $(VERIFY_POLL); waited=$$((waited + $(VERIFY_POLL))); \
	  done; \
	  test "$$paused" = "False" || { \
	    echo "$(DAG) is paused, so a triggered run would sit queued forever."; \
	    echo "unpause it deliberately -- it starts a catch-up run as well:"; \
	    echo "  make unpause DAG=$(DAG)"; \
	    exit 1; }; \
	  busy=$$($(COMPOSE) exec -T airflow airflow dags list-runs $(DAG) -o plain 2>/dev/null \
	    | awk '$$3 == "running" || $$3 == "queued" {print $$2}' | head -3); \
	  test -z "$$busy" || { \
	    echo "$(DAG) already has a run in flight, and max_active_runs is 1:"; \
	    for r in $$busy; do echo "  $$r"; done; \
	    echo "a trigger now would sit QUEUED behind it, and this command would"; \
	    echo "report 'still unqueued' after $(VERIFY_TIMEOUT)s having named nothing."; \
	    echo "two runs writing one catalog is also not a witness. wait for it, or:"; \
	    echo "  make kill-runs DAG=$(DAG)"; \
	    exit 1; }; \
	  run="verify__$$(date -u +%Y%m%dT%H%M%SZ)"; \
	  echo "platform: $(DAG) -> $$run"; \
	  $(COMPOSE) exec -T airflow airflow dags trigger $(DAG) --run-id "$$run" >/dev/null; \
	  waited=0; \
	  while :; do \
	    state=$$($(COMPOSE) exec -T airflow airflow dags list-runs $(DAG) -o plain 2>/dev/null \
	      | awk -v r="$$run" '$$2 == r {print $$3}'); \
	    case "$$state" in \
	      success) echo "platform: $$run SUCCEEDED"; exit 0;; \
	      failed) break;; \
	    esac; \
	    test $$waited -lt $(VERIFY_TIMEOUT) || { \
	      echo "platform: $$run is still $${state:-unqueued} after $(VERIFY_TIMEOUT)s."; \
	      echo "giving up WITHOUT a verdict -- this is not a pass:  make logs"; \
	      exit 1; }; \
	    sleep $(VERIFY_POLL); waited=$$((waited + $(VERIFY_POLL))); \
	  done; \
	  echo "platform: $$run FAILED."; \
	  echo "the ones that FAILED are the cause; the rest were blocked behind them:"; \
	  $(COMPOSE) exec -T postgres psql -U airflow -d airflow -t -A -c \
	    "select case when state = 'failed' then '  FAILED   ' else '  blocked  ' end \
	            || task_id from task_instance \
	     where dag_id='$(DAG)' and run_id='$$run' and coalesce(state,'x') <> 'success' \
	     order by (state = 'failed') desc, task_id;" 2>/dev/null || true; \
	  echo "logs:  make logs"; \
	  exit 1

# THE PRODUCT NAMES ITS DAG; THE PLATFORM MUST NOT. `verify` insists on DAG=
# so a typo cannot run the wrong graph, and a test here refuses any product
# identifier in this repository, so the argument-free `witness` DERIVES the
# id from the product it was pointed at: the one file under its dags/. Two
# files is an ambiguity and it says so rather than guessing. The id is the
# file's stem by the leaves' own convention; a product that breaks it gets
# `verify`'s "no such DAG" rather than a silent wrong run.
witness: ## The family's one word for `verify`, needing no arguments
	@n=$$(ls $(PRODUCT_ABS)/dags/*.py 2>/dev/null | wc -l | tr -d ' '); \
	test "$$n" -eq 1 || { echo "witness: $(PRODUCT_ABS)/dags holds $$n DAG files, so say which: make verify DAG=<dag_id>"; exit 2; }
	$(MAKE) verify DAG=$(basename $(notdir $(wildcard $(PRODUCT_ABS)/dags/*.py)))

kill-runs: ## Mark every in-flight run of a DAG failed:  make kill-runs DAG=contoso_daily
# THE ESCAPE HATCH `verify` POINTS AT. A fresh stack starts a CATCH-UP run of
# its own the moment the metadata database is created -- nobody unpaused
# anything -- and with max_active_runs 1 that run owns the only slot. Every
# later trigger queues behind it, which is what "still unqueued after 3600s"
# actually meant, three times in one evening.
#
# Deliberately not `dags backfill --reset-dagruns` or a pause: this only ends
# runs that are already in flight, so a witness starts from a quiet DAG
# without changing the schedule that produced them.
	@test -n "$(DAG)" || { echo "usage: make kill-runs DAG=<dag_id>"; exit 2; }
	@$(COMPOSE) exec -T postgres psql -U airflow -d airflow -q -c \
	  "update task_instance set state='failed' \
	    where dag_id='$(DAG)' and state in ('running','queued','scheduled','deferred'); \
	   update dag_run set state='failed', end_date=now() \
	    where dag_id='$(DAG)' and state in ('running','queued');" >/dev/null
	@echo "platform: in-flight runs of $(DAG) ended"

down: ## Stop and remove everything, volumes included
	$(COMPOSE) down -v

logs: ## Follow the Airflow logs
	$(COMPOSE) logs -f airflow

connections: ## Show the connections the product can ask for by name
	$(COMPOSE) exec airflow airflow connections list

# NOT CONFIGURED ANYWHERE, and that is the point. This platform sets no Airflow
# admin credential, so Airflow 3's simple auth manager generates one on first
# start and writes it under AIRFLOW_HOME. Pinning a password in docker-compose
# would put a working credential in the repository for every consumer of this
# platform at once, and a default that everyone knows is not a login.
#
# The startup log prints it too, but only once -- by the time anyone needs it,
# it is thousands of lines back. Reading the file is the reliable path.
#
# IT CHANGES WHEN THE CONTAINER DOES. The file lives in the container, not in a
# volume, so `make down` (which takes volumes with it) and then `make up` issues
# a NEW password. That is the usual reason a saved one stops working.
creds: ## The Airflow admin login for this stack
	@$(COMPOSE) exec -T airflow test -f $(PASSWORD_FILE) 2>/dev/null || { \
	  echo "no generated password at $(PASSWORD_FILE)."; \
	  echo "is the stack up? try: make up"; exit 1; }
	@echo "url:      http://localhost:$${AIRFLOW_PORT:-18082}"
	@$(COMPOSE) exec -T airflow python3 -c "import json;d=json.load(open('$(PASSWORD_FILE)'));[print(f'user:     {u}\npassword: {p}') for u, p in d.items()]"

doctor: ## Refuse to start against a product that cannot work
	@test -d "$(PRODUCT_ABS)" || { echo "no product at $(PRODUCT_ABS)"; exit 1; }
	@test -f "$(PRODUCT_ABS)/pyproject.toml" || { \
	  echo "$(PRODUCT_ABS) has no pyproject.toml -- the worker would install nothing"; exit 1; }
	@test -d "$(PRODUCT_ABS)/dags" || { \
	  echo "$(PRODUCT_ABS) has no dags/ -- the bundle would be empty"; exit 1; }
	@grep -q "^\[build-system\]" "$(PRODUCT_ABS)/pyproject.toml" || { \
	  echo "$(PRODUCT_ABS)/pyproject.toml has no [build-system] -- it declares"; \
	  echo "  dependencies but is not an installable package, so its own modules"; \
	  echo "  would be missing from the worker and every DAG importing them would"; \
	  echo "  fail at run time with ModuleNotFoundError."; exit 1; }
	@echo "platform: $(PRODUCT_NAME) provides pyproject.toml and dags/"

test: ## Repo-boundary tests (no Docker)
	uv run --frozen --group dev python -m pytest tests -q

lint: ## Lint this repository's own scripts and tests
# The PRODUCT's code is linted in the product repository. What is left here is
# the platform: scripts/ and tests/, neither of which imports anything
# third-party.
	uv run --frozen --group dev python -m ruff check .
