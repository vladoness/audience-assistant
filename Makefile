.PHONY: setup data run mcp test lint eval eval-heldout eval-refine eval-advisor eval-replay record business-case bench

setup:          ## install dependencies into .venv
	uv sync

data:           ## (re)generate the 200k-line synthetic profile store
	uv run python -m app.synthdata --rows 200000 --out data/profiles.duckdb

run:            ## start the app on http://127.0.0.1:8000 (generates the synthetic store on first run)
	SYNTHETIC_DATA=1 uv run python -m app.main

mcp:            ## MCP server over stdio for AI agents (same governance; drafts only, a person approves in the UI)
	SYNTHETIC_DATA=1 uv run python -m app.mcp_server

test:           ## run the test suite (no network, no API key needed)
	uv run pytest

eval:           ## dev set against the live model, 3 runs (needs ANTHROPIC_API_KEY); MODEL=... EFFORT=...
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --repeat 3

eval-heldout:   ## frozen held-out set against the live model, 3 runs
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases heldout --repeat 3

eval-refine:    ## refinement set (fixed starting audience + one change) against the live model, 3 runs
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases refine --repeat 3

eval-advisor:   ## advisor set (known-truth synthetic history) against the live model, 3 runs
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases advisor --repeat 3

record:         ## live runs of all four eval sets that also save replay fixtures for offline mode
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --record
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases heldout --record
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases refine --record
	uv run python -m evals.run --provider anthropic --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases advisor --record

eval-replay:    ## reproduce run 1 of every eval set offline from recorded fixtures (no key)
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low)
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases heldout
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases refine
	uv run python -m evals.run --provider replay --model $(or $(MODEL),claude-sonnet-5-5) --effort $(or $(EFFORT),low) --cases advisor

business-case:  ## low / base / high business case from docs/business_case.yaml (ARGS="--set key=value" to change one)
	uv run python -m scripts.business_case $(ARGS)

bench:          ## sizing latency at 200k / 1M / 5M lines
	uv run python -m scripts.bench --rows 200000 1000000 5000000

lint:           ## check the governance config (fields, labels, policies, parameters, glossary)
	uv run python -m app.lint
