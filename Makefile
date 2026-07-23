PYTHON ?= python3
RENDER_DIR ?= build/check

.PHONY: validate render clean

validate:
	@./scripts/validate.sh

render:
	@$(PYTHON) scripts/configure.py --config config/gallery.example.json --output $(RENDER_DIR)
	@echo "Rendered example into $(RENDER_DIR)"

clean:
	@rm -rf -- build dist
