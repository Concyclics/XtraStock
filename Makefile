PYTHON ?= .venv/bin/python
APP := apps/finagent
export PYTHONPATH := $(CURDIR)/src:$(CURDIR)/packages/mmclaw:$(CURDIR)/$(APP)

.PHONY: install configure test test-core test-agent test-gateway lint doctor scan deploy-plan
install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -c config/development.lock -e '.[analytics,gateway,dev]' -e packages/mmclaw

configure:
	$(PYTHON) tools/init_config.py

test: test-core test-agent test-gateway

test-core:
	$(PYTHON) -m unittest discover -s tests -v

test-agent:
	$(PYTHON) $(APP)/scripts/agent/run_quality_gate.py --unit-only --output-dir .local/test-results

test-gateway:
	$(PYTHON) -m unittest discover -s services/model_gateway -p 'test_*.py' -v

lint:
	$(PYTHON) -m ruff check src tools tests apps/finagent packages/mmclaw/mmclaw services/model_gateway

doctor:
	$(PYTHON) $(APP)/scripts/ops/check_repository.py

scan:
	$(PYTHON) tools/check_publication.py

deploy-plan:
	$(PYTHON) tools/render_systemd.py --check
