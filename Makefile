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

.PHONY: help up down logs connections creds doctor sources trigger prepare token
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

trigger: ## Trigger a DAG and wait:  make trigger DAG=contoso_daily
	$(COMPOSE) exec -T airflow airflow dags trigger $(DAG)

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
