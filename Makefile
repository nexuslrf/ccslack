.PHONY: fmt lint test typecheck deptry check install dev build clean

# Use --extra dev so the pinned ruff (0.14.x) is used, not the global one.
# ruff 0.15.x formatter regresses multi-exception clauses (rewrites
# `except (A, B):` to `except A, B:` — a Python 3 SyntaxError).
fmt:
	uv run --extra dev ruff format src/ tests/

lint:
	uv run --extra dev ruff check src/ tests/

typecheck:
	uv run pyright src/ccslack/ tests/

deptry:
	uv run deptry src

test:
	uv run pytest tests/ -m "not integration and not e2e"

test-integration:
	uv run pytest tests/integration/ -m "not llm" -v

test-e2e:
	uv run pytest tests/e2e/ -v --timeout=300

test-all:
	uv run pytest tests/ -v -m "not e2e"

check: fmt lint typecheck test

install:
	uv sync

dev:
	uv sync --extra dev

build:
	uv build

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +
