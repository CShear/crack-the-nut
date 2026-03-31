# Contributing to crack-the-nut

## Adding a Strategy

1. Create a new file in `strategies/` (or `strategies/examples/` if it's a reference impl)
2. Extend `Strategy` from `strategies/base.py`
3. Implement `on_data()`, `should_enter()`, `should_exit()`
4. Add a docstring explaining the thesis — what market inefficiency are you exploiting?
5. Test it against the backtest engine before deploying live

## Adding an Exchange Adapter

1. Create a directory in `exchanges/<name>/`
2. Extend `ExchangeAdapter` from `exchanges/base.py`
3. Implement all abstract methods
4. Add any exchange-specific gotchas to `docs/exchange-notes.md`

## Adding an Agentic Wrapper

Agentic strategies live in `agents/`. These use LLMs to analyze data and generate signals.
They should still emit `Signal` objects so they compose with the rest of the framework.

## Code Style

- Python 3.12+, async throughout
- Use `ruff` for formatting: `ruff check . && ruff format .`
- Type hints everywhere
- structlog for logging (not print statements)
- Tests in `tests/` — at minimum, test your strategy against historical data

## PR Process

1. Branch off `main`
2. Write your code
3. Run `ruff check .` and fix any issues
4. Open a PR with:
   - What the strategy/feature does
   - Backtest results if applicable
   - Any config changes needed
5. Get at least one review from another group member
