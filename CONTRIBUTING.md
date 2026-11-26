# Contributing

Contributions, bug fixes, and documentation improvements are welcome.

## Development Setup

No third-party packages are required. You only need Python 3.9 or newer.

1. Fork the repository and create a branch from `main`.
2. Make your changes, keeping functions focused and preserving type hints.
3. Add or update unit tests in `tests/` when changing behavior.
4. Verify the test suite passes before opening a pull request:

```bash
python -m unittest discover -s tests -v
```

## Scope Notes

- Keep the core runtime free of external dependencies unless there is a clear reason to introduce one.
- Keep `hospital_system.py` decoupled from HTTP/UI concerns so the data structures remain easy to test and reuse.
