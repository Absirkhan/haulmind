# Haulmind — every recipe is a plain `uv run` command so it works on Windows, macOS and Linux.

.PHONY: setup run demo eval eval-replay eval-live test lint typecheck fmt clean reset models sim index mcp web

setup:        ## Install deps, run migrations, build the FAISS index
	uv sync
	uv run alembic upgrade head
	uv run python -m haulmind.rag.build_index
	@echo "setup complete. Copy .env.example to .env and add GEMINI_API_KEY (optional)."

run:          ## Start MCP server + web app (UI, event bus, simulator, agents)
	uv run honcho start

demo:         ## Seed a small fleet and fire a scripted HOS incident, then open the inbox
	uv run python -m haulmind.demo

sim:          ## Generate a seeded run (events + ground truth) without streaming
	uv run python -m haulmind.simulator --seed 42 --hours 48

mcp:          ## Run only the MCP server over streamable HTTP
	uv run python -m haulmind.mcp_server --transport http

web:          ## Run only the web process (connects to MCP over HTTP)
	MCP_TRANSPORT=http uv run python -m haulmind.web

index:        ## Rebuild the FAISS index for the DTC/service corpus
	uv run python -m haulmind.rag.build_index --force

models:       ## List Gemini models reachable with the configured key
	uv run python -m haulmind.llm.list_models

eval:         ## Full evaluation (uses LLM cache, calls the API on misses)
	uv run python -m evals.run_eval --all

eval-replay:  ## Evaluation with zero network calls (cache only)
	uv run python -m evals.run_eval --all --llm-cache-mode replay

eval-live:    ## Evaluation ignoring the cache (fresh LLM calls)
	uv run python -m evals.run_eval --all --llm-cache-mode off

test:         ## Run the test suite
	uv run pytest

lint:         ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt:          ## Auto-format
	uv run ruff format .
	uv run ruff check --fix .

typecheck:    ## mypy (strict)
	uv run mypy haulmind evals

reset:        ## Delete the SQLite DB and runs (keeps the LLM cache)
	uv run python -m haulmind.db.reset

clean:        ## Remove caches and build artefacts
	uv run python -c "import shutil,pathlib;[shutil.rmtree(p,ignore_errors=True) for p in ['.pytest_cache','.mypy_cache','.ruff_cache']];[shutil.rmtree(p,ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
