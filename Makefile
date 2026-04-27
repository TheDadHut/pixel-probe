.PHONY: install test lint format run cli fixtures clean

install:
	pip install -e ".[dev]"
	pre-commit install

test:
	pytest

lint:
	ruff check src tests
	ruff format --check src tests
	mypy src

format:
	ruff format src tests
	ruff check --fix src tests

run:
	python -m pixel_probe.gui.main_window

cli:
	python -m pixel_probe.cli $(ARGS)

fixtures:
	python scripts/build_fixtures.py

clean:
	rm -rf build/ dist/ *.egg-info/ src/*.egg-info/
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ .hypothesis/
	rm -rf htmlcov/ coverage.xml .coverage junit.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
