# Contributing

Issues and pull requests in Japanese or English are welcome.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m build
```

Keep runtime dependencies at zero unless a change has a clear operational benefit. New
behavior must include tests, especially for state transitions, process cleanup, path
validation, hook responses, and resume behavior.

## Safety expectations

- Never commit Codex credentials, environment dumps, real session state, or job logs.
- Tests must use temporary directories and fake process/session identifiers.
- Do not weaken the default `workspace-write` sandbox or silently enable
  `danger-full-access`.
- Commands must remain argv arrays; do not introduce `shell=True`.
- Changes to hook response formats must be validated against the supported Codex CLI.

Run the complete test suite before opening a pull request. Real Codex smoke tests can incur
API usage and should be performed only when necessary, with a minimal prompt.
