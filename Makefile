.PHONY: setup data run test lint eval eval-replay record bench

setup:          ## install dependencies into .venv
	uv sync

data:           ## (re)generate the 200k-line synthetic profile store
	uv run python -m app.synthdata --rows 200000 --out data/profiles.duckdb

run:            ## start the app on http://127.0.0.1:8000 (generates the synthetic store on first run)
	SYNTHETIC_DATA=1 uv run python -m app.main

test:           ## run the test suite (no network, no API key needed)
	uv run pytest

eval:           ## run the golden set against the live model (needs ANTHROPIC_API_KEY); MODEL=...
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-haiku-5-5)

record:         ## live eval run that also saves replay fixtures for offline mode
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-haiku-5-5) --record

eval-replay:    ## reproduce an eval run offline from recorded fixtures
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-haiku-5-5)

bench:          ## sizing latency at 200k / 1M / 5M lines
	uv run python -m scripts.bench --rows 200000 1000000 5000000

lint:           ## check the governance config (fields, labels, policies, parameters, glossary)
	uv run python -m app.lint
