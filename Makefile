# homelab-iac — task runner.
#
# Vault password handling (pick one):
#   1. Interactive:   every target prompts once via --ask-vault-pass (default).
#   2. Password file: `echo 'yourpass' > .vault_pass && chmod 600 .vault_pass`
#      then run any target with USE_VAULT_FILE=1, e.g. `make deploy USE_VAULT_FILE=1`.
#      .vault_pass is .gitignored. This is what the systemd timer uses.
#
# Extra ansible args pass through via ARGS, e.g.:
#   make deploy ARGS="--limit svc-download --tags jail,verify"
#   make check  ARGS="--limit svc-media"

SHELL       := /bin/bash
PLAYBOOK    := site.yml
VERIFY      := verify.yml
# Prefer the project venv (created by `make deps`) when it exists.
BIN         := $(if $(wildcard .venv/bin/ansible-playbook),.venv/bin/,)
ANSIBLE     := $(BIN)ansible-playbook
LINT        := $(BIN)ansible-lint

ifeq ($(USE_VAULT_FILE),1)
  VAULT := --vault-password-file .vault_pass
else
  VAULT := --ask-vault-pass
endif

.DEFAULT_GOAL := help

.PHONY: help deps preflight deploy dl media check verify drift reconcile access ping lint vault-edit clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

deps: ## Install control-node deps (venv + collections + python libs)
	python3 -m venv .venv
	.venv/bin/pip install ansible "proxmoxer>=2.0" requests
	.venv/bin/ansible-galaxy collection install -r requirements.yml

preflight: ## Cheap checks before a real run: syntax + inventory + connectivity
	$(ANSIBLE) --syntax-check $(PLAYBOOK) $(VERIFY)
	$(BIN)ansible-inventory --list >/dev/null && echo "inventory OK"
	@echo "Ping service VMs (only works once they exist):"
	-$(BIN)ansible service_vms -m ping

deploy: ## Full unattended deploy (provision + configure + verify + ntfy)
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) $(ARGS)

dl: ## Configure/verify only svc-download
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit svc-download $(ARGS)

media: ## Configure/verify only svc-media
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit svc-media $(ARGS)

check: ## Dry-run the full deploy (no changes) with a diff
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --check --diff $(ARGS)

drift: check ## Alias for check — see what has drifted since last apply

reconcile: ## Push cores/memory/onboot/startup from host_vars onto EXISTING VMs (opt-in)
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --tags provision -e pve_reconcile=true $(ARGS)

access: ## (Re)run just the svc-media access layer (Caddy *.fort.wow)
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit svc-media --tags access $(ARGS)

verify: ## Run ONLY the gate assertions (health check) + ntfy
	$(ANSIBLE) $(VERIFY) $(VAULT) --tags verify $(ARGS)

ping: ## Ansible ping the two service VMs
	$(BIN)ansible service_vms -m ping $(ARGS)

lint: ## ansible-lint (install: pip install ansible-lint)
	$(LINT) $(PLAYBOOK) $(VERIFY) || echo "(ansible-lint not installed or found issues)"

vault-edit: ## Edit the encrypted secrets file
	$(BIN)ansible-vault edit inventory/group_vars/all/vault.yml

clean: ## Remove local ansible cruft
	rm -rf *.retry .ansible collections
