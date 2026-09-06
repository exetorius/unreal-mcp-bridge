# Contributing

Thanks for taking a look at the bridge. It is a single-file stdio↔HTTP relay
(`mcp_bridge.py`), stdlib only, no runtime dependencies — please keep it that way.

## Where pull requests go

**Open pull requests against `contrib`, not `main`.**

`main` is what people actually run against a live Unreal Editor, so changes are
staged on `contrib` and reviewed before they are moved across.

If you forget, nothing breaks: a pull request opened against `main` from outside
the repo is **retargeted onto `contrib` automatically** and a comment is left
saying so. Your commits and the discussion are untouched — only the base branch
changes, and there is nothing you need to do.

```bash
gh pr create --base contrib
```

## Before you open it

Everything CI runs, you can run locally in a few seconds:

```bash
pip install ruff==0.16.6
ruff check .              # settings come from pyproject.toml
python -m compileall -q mcp_bridge.py
python -c "import mcp_bridge"
python tests/smoke_test.py
```

If you touch anything under `.github/workflows/`, lint it too:

```bash
# actionlint also runs shellcheck over every `run:` block
actionlint
```

The same four gates run on your pull request:

| Check | What it protects |
|---|---|
| **Lint (ruff)** | undefined names, likely bugs, style — configured in `pyproject.toml` |
| **Compile & import (3.10–3.13)** | the file parses and imports with no side effects on every supported Python |
| **Smoke (no editor required)** | the bridge survives an absent editor, exits cleanly when stdin closes, and still answers `initialize` from its cold-start cache |
| **Lint workflows (actionlint)** | workflow schema and expressions, plus shellcheck over every embedded `run:` script |

Those four report through a fifth job named **`CI`**, which goes green only if
all of them do. That name is load-bearing: the branch ruleset on `main` requires
a status check with the exact context `CI`, matched by string. Renaming or
removing the job does not turn pull requests red — it means the required check
never reports, so they sit on "Expected — Waiting for status to be reported" and
cannot be merged. Change the job name and the ruleset together, or not at all.

## Things that will fail review

- **Python older than 3.10 is not supported.** `mcp_bridge.py` uses PEP 604
  `X | None` annotations in function signatures with no
  `from __future__ import annotations`, so they are evaluated at definition time.
- **No new runtime dependencies.** The bridge is launched by Claude Code with
  whatever Python is on PATH; it cannot assume a virtualenv.
- **No module-level side effects.** Importing the bridge must not open a socket,
  read from stdin, or block — CI asserts this.
- **Don't fail silently.** When the upstream editor is unreachable, report the
  actual error to the client; never assume a cause.

## Reporting a problem

Bridge behaviour depends heavily on what the editor is doing, so please include
the bridge's stderr (it logs every reconnect and handshake with a `[mcp-bridge]`
prefix), your Unreal Engine version, and your Python version.

## Licence

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE) that covers this project.
