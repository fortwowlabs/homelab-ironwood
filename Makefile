# homelab-iac operator entry points.
#
# Live targets prompt for the Ansible Vault password by default. For unattended
# use, create a mode-0600 `.vault_pass` and add `USE_VAULT_FILE=1`.
# Additional Ansible arguments pass through in ARGS.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PLAYBOOK := site.yml
PREFLIGHT_PLAYBOOK := preflight.yml
VERIFY_PLAYBOOK := verify.yml
DISRUPTIVE_PLAYBOOK := verify-disruptive.yml
FIXTURE_INVENTORY := tests/fixtures/inventory.yml

VENV := .venv
BIN := $(if $(wildcard $(VENV)/bin/ansible-playbook),$(VENV)/bin/,)
PYTHON := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
ANSIBLE := $(BIN)ansible-playbook
ANSIBLE_ADHOC := $(BIN)ansible
INVENTORY_CMD := $(BIN)ansible-inventory
ANSIBLE_VAULT := $(BIN)ansible-vault
ANSIBLE_LINT := $(BIN)ansible-lint
YAMLLINT := $(BIN)yamllint
SHELLCHECK ?= shellcheck

# Keep Ansible's controller-side scratch data inside the checkout. This makes
# validation work in restricted runners and `clean` removes it predictably.
export ANSIBLE_LOCAL_TEMP := $(CURDIR)/.ansible/tmp
export ANSIBLE_HOME := $(CURDIR)/.ansible
export XDG_CACHE_HOME := $(CURDIR)/.ansible/cache

# Trust the committed PVE cluster CA so the proxmox modules verify TLS with
# validation ON (pve_validate_certs stays true). The community.proxmox modules
# read this via their PROXMOX_CA_PATH env fallback; when set alongside
# validate_certs=true they verify against this bundle. See group_vars/pve.yml.
export PROXMOX_CA_PATH := $(CURDIR)/inventory/pve-cluster-ca.crt

ifeq ($(USE_VAULT_FILE),1)
  VAULT := --vault-password-file .vault_pass
else
  VAULT := --ask-vault-pass
endif

REPOSITORY_YAML := $(shell git ls-files --cached --others --exclude-standard '*.yml' '*.yaml')
REPOSITORY_SHELL := $(shell git ls-files --cached --others --exclude-standard '*.sh')
# `git ls-files --cached` retains worktree deletions until commit time. Filter
# those paths while still validating new, untracked implementation files.
YAML_FILES := $(foreach file,$(REPOSITORY_YAML),$(if $(wildcard $(file)),$(file)))
SHELL_FILES := $(foreach file,$(REPOSITORY_SHELL),$(if $(wildcard $(file)),$(file)))

.DEFAULT_GOAL := help

.PHONY: help deps deps-dev validate validate-tools validate-syntax \
	validate-ansible validate-yaml validate-shell validate-links \
	validate-catalog validate-provisioning validate-systemd validate-secrets validate-ci preflight deploy dl media infra \
	check check-diff verify verify-disruptive drift reconcile access ping lint \
	vault-edit clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

deps: ## Create .venv and install the pinned runtime and Ansible collections
	python3 -m venv $(VENV)
	$(VENV)/bin/python -m pip install --requirement requirements.txt
	$(VENV)/bin/ansible-galaxy collection install --force --collections-path collections --requirements-file requirements.yml

deps-dev: deps ## Install pinned validation dependencies as well
	$(VENV)/bin/python -m pip install --requirement requirements-dev.txt

validate: validate-tools validate-syntax validate-ansible validate-yaml validate-shell validate-links validate-catalog validate-provisioning validate-systemd validate-secrets validate-ci ## Run every offline validation gate

validate-tools:
	@mkdir -p .ansible/tmp .ansible/cache
	@test -x "$(ANSIBLE)" || { echo "missing $(ANSIBLE); run 'make deps-dev'" >&2; exit 127; }
	@test -x "$(ANSIBLE_LINT)" || { echo "missing $(ANSIBLE_LINT); run 'make deps-dev'" >&2; exit 127; }
	@test -x "$(YAMLLINT)" || { echo "missing $(YAMLLINT); run 'make deps-dev'" >&2; exit 127; }
	@command -v "$(SHELLCHECK)" >/dev/null || { echo "missing ShellCheck (install it with your OS package manager)" >&2; exit 127; }

validate-syntax:
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(PREFLIGHT_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(VERIFY_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(DISRUPTIVE_PLAYBOOK)

validate-ansible:
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE_LINT) --offline --profile min $(PLAYBOOK) $(PREFLIGHT_PLAYBOOK) $(VERIFY_PLAYBOOK) $(DISRUPTIVE_PLAYBOOK)

validate-yaml:
	$(YAMLLINT) $(YAML_FILES)

validate-shell:
	$(SHELLCHECK) $(SHELL_FILES)
	$(PYTHON) tests/validate_shell_templates.py

validate-links:
	$(PYTHON) tests/validate_links.py

validate-catalog:
	$(PYTHON) tests/validate_catalog.py
	$(PYTHON) tests/validate_generated_catalog.py

validate-provisioning:
	$(PYTHON) tests/validate_pve_states.py

validate-systemd:
	$(PYTHON) tests/validate_systemd_units.py

validate-secrets:
	$(PYTHON) tests/validate_secrets.py
	$(PYTHON) tests/scan_history_secrets.py
	$(PYTHON) tests/validate_secret_tasks.py
	$(PYTHON) tests/validate_secret_output.py

validate-ci:
	$(PYTHON) tests/validate_ci_safety.py
	$(PYTHON) tests/validate_verify_safety.py

preflight: ## Authenticate, show the safe inventory graph, and require VM connectivity
	$(INVENTORY_CMD) --graph $(VAULT)
	$(ANSIBLE) $(PREFLIGHT_PLAYBOOK) $(VAULT) $(ARGS)

deploy: ## Full provision, configuration, and verification
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) $(ARGS)

dl: ## Configure and verify the download VM
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit download_vms $(ARGS)

media: ## Configure and verify the media VM
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit media_vms $(ARGS)

infra: ## Configure and verify the infra VM
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit infra_vms $(ARGS)

check: ## Safe check mode without displaying file diffs
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --check $(ARGS)

check-diff: ## Check mode with diffs; validation enforces secret-task redaction
	ANSIBLE_DISPLAY_ARGS_TO_STDOUT=False $(ANSIBLE) $(PLAYBOOK) $(VAULT) --check --diff $(ARGS)

drift: check-diff ## Alias for the sanitized diff check

reconcile: ## Reconcile cores, memory, onboot, and startup on existing VMs
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --tags provision -e pve_reconcile=true $(ARGS)

access: ## Re-run the media VM's Caddy access layer
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit media_vms --tags access $(ARGS)

verify: ## Run the non-disruptive verification playbook
	$(ANSIBLE) $(VERIFY_PLAYBOOK) $(VAULT) $(ARGS)

verify-disruptive: ## Explicitly run the fail-closed recovery drill
	$(ANSIBLE) $(DISRUPTIVE_PLAYBOOK) $(VAULT) $(ARGS)

ping: ## Require Ansible connectivity to every service VM
	$(ANSIBLE_ADHOC) service_vms --module-name ansible.builtin.ping $(VAULT) $(ARGS)

lint: validate-ansible validate-yaml validate-shell ## Run the strict lint subset

vault-edit: ## Edit the encrypted secrets file
	$(ANSIBLE_VAULT) edit $(VAULT) inventory/group_vars/all/vault.yml

clean: ## Remove local validation and Ansible scratch files
	rm -rf -- *.retry .ansible collections
