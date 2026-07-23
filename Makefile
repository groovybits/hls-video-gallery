PYTHON ?= python3
RENDER_DIR ?= build/check

.PHONY: validate render quality quality-test clean

validate:
	@./scripts/validate.sh

render:
	@$(PYTHON) scripts/configure.py --config config/gallery.example.json --output $(RENDER_DIR)
	@echo "Rendered example into $(RENDER_DIR)"

quality:
	@$(MAKE) -C tools/quality-analyzer

quality-test:
	@$(MAKE) -C tools/quality-analyzer test

clean:
	@rm -rf -- build dist
	@$(MAKE) -C tools/quality-analyzer clean
