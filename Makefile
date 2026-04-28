.PHONY: install test coverage lint format run cli fixtures clean

install:
	pip install -e ".[dev]"
	pre-commit install

test:
	pytest

# Run the suite with coverage and emit an HTML report locally.
# Mirrors what CI does so a local "make coverage" pass and a CI run see
# the same numbers. Skip --cov-fail-under here so a low local run still
# produces the HTML for inspection instead of bailing.
coverage:
	pytest -m "not gui" \
		--cov=src/pixel_probe \
		--cov-branch \
		--cov-report=term-missing \
		--cov-report=html:htmlcov
	@echo ""
	@echo "  Open htmlcov/index.html in a browser to drill in."

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
