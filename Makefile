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
SCAN_PLAYBOOK := scan.yml
RELEASE_PLAYBOOK := release.yml
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
GITLEAKS ?= gitleaks
RUFF := $(BIN)ruff

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
	validate-ansible validate-yaml validate-shell validate-python validate-links \
	validate-catalog validate-provisioning validate-systemd validate-secrets validate-ci preflight deploy dl media infra pve \
	check check-diff verify verify-disruptive scan image-digest image-check image-bump \
	release-check release-report image-release roster-check drift reconcile access ping lint owui-image-config image-gen-check image-edit-check \
	owui-personas owui-export \
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

validate: validate-tools validate-syntax validate-ansible validate-yaml validate-shell validate-python validate-links validate-catalog validate-provisioning validate-systemd validate-secrets validate-ci ## Run every offline validation gate

validate-tools:
	@mkdir -p .ansible/tmp .ansible/cache
	@test -x "$(ANSIBLE)" || { echo "missing $(ANSIBLE); run 'make deps-dev'" >&2; exit 127; }
	@test -x "$(ANSIBLE_LINT)" || { echo "missing $(ANSIBLE_LINT); run 'make deps-dev'" >&2; exit 127; }
	@test -x "$(YAMLLINT)" || { echo "missing $(YAMLLINT); run 'make deps-dev'" >&2; exit 127; }
	@command -v "$(SHELLCHECK)" >/dev/null || { echo "missing ShellCheck (install it with your OS package manager)" >&2; exit 127; }
	@command -v "$(GITLEAKS)" >/dev/null || { echo "missing gitleaks (install it with your OS package manager)" >&2; exit 127; }
	@test -x "$(RUFF)" || { echo "missing $(RUFF); run 'make deps-dev'" >&2; exit 127; }

validate-syntax:
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(PREFLIGHT_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(VERIFY_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(DISRUPTIVE_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(SCAN_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(RELEASE_PLAYBOOK)

validate-ansible:
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE_LINT) --offline --profile min $(PLAYBOOK) $(PREFLIGHT_PLAYBOOK) $(VERIFY_PLAYBOOK) $(DISRUPTIVE_PLAYBOOK) $(SCAN_PLAYBOOK) $(RELEASE_PLAYBOOK)

validate-yaml:
	$(YAMLLINT) $(YAML_FILES)

validate-shell:
	$(SHELLCHECK) $(SHELL_FILES)
# Gates are discovered, not listed. Each tests/validate_*.py names its own
# group in a GATE_GROUP constant and tests/run_gates.py collects them, so
# adding a gate is a new FILE rather than a new line here. Three branches in
# one week collided on this target when it was a list, always with the same
# resolution of keeping both lines.
#
# The discovery also refuses to run at all if any tests/validate_*.py declares
# no group — a gate nobody invokes used to be indistinguishable from a passing
# estate, and remembering to add a line here was the only thing preventing it.
	$(PYTHON) tests/run_gates.py shell

validate-python:
# Invoked directly rather than through tests/run_gates.py, the same way
# ShellCheck and gitleaks are. run_gates.py exists so that ADDING a gate is a
# new file rather than a new Makefile line; ruff is not a gate, it is a tool
# that lints the gates, and routing it through the runner it lints would invert
# that dependency for nothing.
	$(RUFF) check --no-cache tests scripts

validate-links:
	$(PYTHON) tests/run_gates.py links

validate-catalog:
	$(PYTHON) tests/run_gates.py catalog

validate-provisioning:
	$(PYTHON) tests/run_gates.py provisioning

validate-systemd:
	$(PYTHON) tests/run_gates.py systemd

validate-secrets:
	$(PYTHON) tests/run_gates.py secrets
	$(PYTHON) tests/scan_history_secrets.py
# Second opinion on the working tree with gitleaks' ~170 upstream rules. The
# discovered gates above stay: they know this repo's conventions (vault_
# naming, the placeholder forms in all_vault.yml.example) and gitleaks does
# not. Scope and the two allowlists are explained in .gitleaks.toml. History is
# deliberately not re-scanned here — scan_history_secrets.py already walks
# every blob.
	$(GITLEAKS) dir . --config .gitleaks.toml --no-banner --redact

validate-ci:
	$(PYTHON) tests/run_gates.py ci

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

pve: ## Configure and verify hypervisor monitoring (disk, SMART, ZFS events)
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit pve_mon_hosts $(ARGS)

check: ## Safe check mode without displaying file diffs
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --check $(ARGS)

check-diff: ## Check mode with diffs; validation enforces secret-task redaction
	ANSIBLE_DISPLAY_ARGS_TO_STDOUT=False $(ANSIBLE) $(PLAYBOOK) $(VAULT) --check --diff $(ARGS)

drift: check-diff ## Alias for the sanitized diff check

reconcile: ## Reconcile cores, memory, onboot, and startup on existing VMs
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --tags provision -e pve_reconcile=true $(ARGS)

access: ## Re-run the media VM's DNS, Caddy, and Homepage access layer
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit media_vms --tags access $(ARGS)

verify: ## Run the non-disruptive verification playbook
	$(ANSIBLE) $(VERIFY_PLAYBOOK) $(VAULT) $(ARGS)

image-check: ## Report which pinned images their tag has moved past
	@scripts/image-check.sh

image-bump: ## Bump REF=<image:tag> to the digest that tag now resolves to
	@test -n "$(REF)" || { echo 'usage: make image-bump REF=docker.io/louislam/uptime-kuma:1' >&2; exit 64; }
	@scripts/image-bump.sh "$(REF)"

image-digest: ## Resolve REF=<image:tag> to the pinnable @sha256 digest
	@test -n "$(REF)" || { echo 'usage: make image-digest REF=ghcr.io/owner/image:tag' >&2; exit 64; }
	@scripts/image-digest.sh "$(REF)"

scan: ## Run the report-only security scan (never remediates)
	$(ANSIBLE) $(SCAN_PLAYBOOK) $(VAULT) $(ARGS)

release-check: ## Report what upstream has released, against the tree in front of you
	@scripts/release-check.sh $(ARGS)

release-report: ## Run the weekly release report on svc-infra (publishes + records state)
	$(ANSIBLE) $(RELEASE_PLAYBOOK) $(VAULT) --limit infra_vms $(ARGS)

image-release: ## What version is REF, and what has upstream released since?
	@test -n "$(REF)" || { echo 'usage: make image-release REF=lscr.io/linuxserver/sonarr' >&2; exit 64; }
	@scripts/image-release.sh "$(REF)"

DB ?= /opt/homelab/appdata/open-webui/webui.db

roster-check: ## Compare models.yml against Ollama and Open WebUI (needs both up; override DB= off svc-infra)
	$(PYTHON) scripts/roster_reconcile.py \
	  --webui-db $(DB)
owui-image-config: ## Push inventory/group_vars/all/images.yml into Open WebUI (needs OWUI_ADMIN_TOKEN)
	$(PYTHON) scripts/owui_image_config.py $(ARGS)

image-gen-check: ## Generate one image end to end and assert it is a 1024x1024 PNG
	$(PYTHON) scripts/image_generation_check.py $(ARGS)

image-edit-check: ## Edit one image end to end and assert Open WebUI reached our checkpoint
	$(PYTHON) scripts/image_edit_check.py $(ARGS)

owui-personas: ## Seed inventory/group_vars/all/personas.yml into Open WebUI (needs OWUI_ADMIN_TOKEN)
	$(PYTHON) scripts/owui_personas.py $(ARGS)

owui-export: ## Record Open WebUI's live config so UI drift shows in git diff (needs OWUI_ADMIN_TOKEN)
	$(PYTHON) scripts/owui_config_export.py $(ARGS)

verify-disruptive: ## Explicitly run the fail-closed recovery drill
	$(ANSIBLE) $(DISRUPTIVE_PLAYBOOK) $(VAULT) $(ARGS)

ping: ## Require Ansible connectivity to every service VM
	$(ANSIBLE_ADHOC) service_vms --module-name ansible.builtin.ping $(VAULT) $(ARGS)

lint: validate-ansible validate-yaml validate-shell ## Run the strict lint subset

vault-edit: ## Edit the encrypted secrets file
	$(ANSIBLE_VAULT) edit $(VAULT) inventory/group_vars/all/vault.yml

clean: ## Remove local validation and Ansible scratch files
	rm -rf -- *.retry .ansible collections
