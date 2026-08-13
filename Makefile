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

# Serializes deploys between the two control nodes. See scripts/deploy-lock.sh.
DEPLOY_LOCK := scripts/with-deploy-lock.sh

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
	validate-catalog validate-provisioning validate-systemd validate-secrets validate-ci preflight deploy dl media infra pve mac \
	check check-diff verify verify-disruptive scan image-digest image-check image-bump \
	release-check release-report image-release drift reconcile access ping lint \
	vault-edit clean deploy-unlock

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
	@command -v "$(GITLEAKS)" >/dev/null || { echo "missing gitleaks (install it with your OS package manager)" >&2; exit 127; }

validate-syntax:
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(PREFLIGHT_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(VERIFY_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(DISRUPTIVE_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(SCAN_PLAYBOOK)
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE) --inventory $(FIXTURE_INVENTORY) --syntax-check $(RELEASE_PLAYBOOK)

validate-ansible:
	ANSIBLE_INVENTORY=$(FIXTURE_INVENTORY) $(ANSIBLE_LINT) --offline --profile min $(PLAYBOOK) $(PREFLIGHT_PLAYBOOK) $(VERIFY_PLAYBOOK) $(DISRUPTIVE_PLAYBOOK) $(SCAN_PLAYBOOK) $(RELEASE_PLAYBOOK)
# macOS state is mostly not file-shaped, so roles/mac_control is largely
# command tasks — and Ansible reports every one of them as changed unless
# told otherwise. This keeps changed=0 meaningful for that role too.
	$(PYTHON) tests/validate_mac_idempotence.py

validate-yaml:
	$(YAMLLINT) $(YAML_FILES)

validate-shell:
	$(SHELLCHECK) $(SHELL_FILES)
	$(PYTHON) tests/validate_shell_templates.py
# Sits here because it exercises a shell template, but it is doing something the
# other shell gates do not: running the script against fixtures and asserting it
# FAILS in the four ways it has to. On live hosts the answer is always OK, so
# without this nobody could tell the check had stopped working.
	$(PYTHON) tests/validate_container_drift.py
# Same reason as the drift check above: on a healthy host the retry wrapper
# succeeds on its first call every time, so the retry and — more importantly —
# its refusal to retry forever are never exercised in production.
	$(PYTHON) tests/validate_dnf_makecache_retry.py
# Python rather than shell, but it belongs with the drift check above for the
# same reason: it is a script whose refusal paths are the point, and on a
# healthy host it succeeds every time. Its "leave the old file alone rather
# than publish zeros" branch is the one that keeps a broken emitter legible.
	$(PYTHON) tests/validate_metric_write.py

validate-links:
	$(PYTHON) tests/validate_links.py

validate-catalog:
	$(PYTHON) tests/validate_catalog.py
	$(PYTHON) tests/validate_infra_catalog.py
# Sits with the catalog gates because it validates a provisioned artifact the
# same way: the dashboards are files this repo owns and Grafana loads verbatim.
# The metric cross-check is the reason it exists — a name renamed in a play
# leaves the panel blank and reports nothing anywhere.
	$(PYTHON) tests/validate_grafana_dashboards.py
	$(PYTHON) tests/validate_generated_catalog.py
	$(PYTHON) tests/validate_sso.py
	$(PYTHON) tests/validate_scan_image_coverage.py
	$(PYTHON) tests/validate_image_provenance.py
	$(PYTHON) tests/validate_release_overrides.py

validate-provisioning:
	$(PYTHON) tests/validate_pve_states.py
	$(PYTHON) tests/validate_preflight_addressing.py
# Sits with the provisioning gates because provision time is the only time
# admin_ssh_pubkeys is read. The narrow thing it proves: the cloud-init
# template renders one key per line. A bare `{{ admin_ssh_pubkeys }}` renders
# Python's list repr instead, which cloud-init accepts without complaint as a
# single malformed key — so a VM would come up authorising nobody, and the
# provisioning run that did it would report success.
	$(PYTHON) tests/validate_admin_keys.py

validate-systemd:
	$(PYTHON) tests/validate_systemd_units.py
	$(PYTHON) tests/validate_onfailure.py
# Sits with the OnFailure gate because it guards the same thing from the other
# end: validate_onfailure.py checks that a failure reaches the alerter, and this
# checks that what the alerter publishes reaches a topic somebody reads.
	$(PYTHON) tests/validate_alert_topics.py

validate-secrets:
	$(PYTHON) tests/validate_secrets.py
	$(PYTHON) tests/scan_history_secrets.py
	$(PYTHON) tests/validate_secret_tasks.py
	$(PYTHON) tests/validate_secret_output.py
	$(PYTHON) tests/validate_vault_guards.py
# Second opinion on the working tree with gitleaks' ~170 upstream rules. The
# four gates above stay: they know this repo's conventions (vault_ naming, the
# placeholder forms in all_vault.yml.example) and gitleaks does not. Scope and
# the two allowlists are explained in .gitleaks.toml. History is deliberately
# not re-scanned here — scan_history_secrets.py already walks every blob.
	$(GITLEAKS) dir . --config .gitleaks.toml --no-banner --redact

validate-ci:
	$(PYTHON) tests/validate_ci_safety.py
	$(PYTHON) tests/validate_verify_safety.py
	$(PYTHON) tests/validate_scan_readonly.py
	$(PYTHON) tests/validate_deploy_lock.py
	$(PYTHON) tests/validate_with_deploy_lock.py
	$(PYTHON) tests/validate_ollama_binding_check.py

preflight: ## Authenticate, show the safe inventory graph, and require VM connectivity
	$(INVENTORY_CMD) --graph $(VAULT)
	$(ANSIBLE) $(PREFLIGHT_PLAYBOOK) $(VAULT) $(ARGS)

deploy: ## Full provision, configuration, and verification
	$(DEPLOY_LOCK) $(ANSIBLE) $(PLAYBOOK) $(VAULT) $(ARGS)

dl: ## Configure and verify the download VM
	$(DEPLOY_LOCK) $(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit download_vms $(ARGS)

media: ## Configure and verify the media VM
	$(DEPLOY_LOCK) $(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit media_vms $(ARGS)

infra: ## Configure and verify the infra VM
	$(DEPLOY_LOCK) $(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit infra_vms $(ARGS)

pve: ## Configure and verify hypervisor monitoring (disk, SMART, ZFS events)
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit pve_mon_hosts $(ARGS)

mac: ## Configure and verify the always-on control node (mac-control)
	$(DEPLOY_LOCK) $(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit control_nodes $(ARGS)

deploy-unlock: ## Clear a stale deploy lock left by a crashed run
	@ssh -o BatchMode=yes root@192.168.1.10 \
	  bash -s -- release /var/lock/homelab-deploy.lock manual \
	  < scripts/deploy-lock.sh

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

verify-disruptive: ## Explicitly run the fail-closed recovery drill
	$(ANSIBLE) $(DISRUPTIVE_PLAYBOOK) $(VAULT) $(ARGS)

ping: ## Require Ansible connectivity to every service VM
	$(ANSIBLE_ADHOC) service_vms --module-name ansible.builtin.ping $(VAULT) $(ARGS)

lint: validate-ansible validate-yaml validate-shell ## Run the strict lint subset

vault-edit: ## Edit the encrypted secrets file
	$(ANSIBLE_VAULT) edit $(VAULT) inventory/group_vars/all/vault.yml

clean: ## Remove local validation and Ansible scratch files
	rm -rf -- *.retry .ansible collections
