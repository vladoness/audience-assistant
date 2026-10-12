.PHONY: setup data run test lint eval eval-heldout eval-replay record bench

setup:          ## install dependencies into .venv
	uv sync

data:           ## (re)generate the 200k-line synthetic profile store
	uv run python -m app.synthdata --rows 200000 --out data/profiles.duckdb

run:            ## start the app on http://127.0.0.1:8000 (generates the synthetic store on first run)
	SYNTHETIC_DATA=1 uv run python -m app.main

test:           ## run the test suite (no network, no API key needed)
	uv run pytest

eval:           ## dev set against the live model, 3 runs (needs ANTHROPIC_API_KEY); MODEL=... EFFORT=...
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --repeat 3

eval-heldout:   ## frozen held-out set against the live model, 3 runs
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases heldout --repeat 3

record:         ## live dev + held-out runs that also save replay fixtures for offline mode
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --record
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases heldout --record

eval-replay:    ## reproduce run 1 of the dev and held-out evals offline from recorded fixtures (no key)
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low)
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases heldout

bench:          ## sizing latency at 200k / 1M / 5M lines
	uv run python -m scripts.bench --rows 200000 1000000 5000000

lint:           ## check the governance config (fields, labels, policies, parameters, glossary)
	uv run python -m app.lint
