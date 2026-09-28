# chipgraph developer tasks
#
# Targets:
#   sync    install dependencies (uv sync)
#   lint    ruff check + ruff format --check
#   fmt     ruff format + ruff check --fix
#   type    mypy
#   test    pytest
#   check   lint + type + test + schemas-check
#   schemas regenerate schemas/ from the pydantic contracts (M0-02)
#   schemas-check   verify schemas/ matches the pydantic contracts, without writing

.PHONY: sync lint fmt type test check schemas schemas-check

sync:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

type:
	uv run mypy

test:
	uv run pytest

check: lint type test schemas-check

schemas:
	@if [ ! -d src/chipgraph/core/contracts ]; then \
		echo "schemas: added by task M0-02"; \
	else \
		uv run python -m chipgraph.core.contracts.export; \
	fi

schemas-check:
	uv run python -m chipgraph.core.contracts.export --check
