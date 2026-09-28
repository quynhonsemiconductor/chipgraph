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
#   image      build the chipgraph-eda image (docker/Dockerfile), lean default target
#   image-e2e  build the image's `test` target and run the tinysoc e2e suite inside it

.PHONY: sync lint fmt type test check schemas schemas-check image image-e2e

sync:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

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

image:
	docker build -t chipgraph-eda -f docker/Dockerfile .

image-e2e:
	docker build --target test -t chipgraph-eda:test -f docker/Dockerfile .
	docker run --rm chipgraph-eda:test uv run --no-sync pytest -m e2e tests/e2e
