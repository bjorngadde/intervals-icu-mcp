---
name: release-check
description: |
  Run the full verification suite and summarize results.
  Use when the user asks to check if the code is ready for release,
  before any PR/merge, or when finishing a feature.
allowed-tools: Bash(make can-release), Bash(make test:*), Bash(make lint:*)
---

## Instructions

1. Run the full pre-release check:
   ```bash
   make can-release
   ```

2. This fork cuts no releases (see CLAUDE.md, "Releases"), so there is no version bump or
   changelog to check. If the change touches `ha-addon/` or anything the add-on runs
   (`src/`, `pyproject.toml`, `uv.lock`), check whether `ha-addon/config.yaml` needs a
   `version` bump and `ha-addon/CHANGELOG.md` an entry, so Home Assistant offers the update.

3. Parse the output and report a clear summary:

   **If everything passes:**
   > ✅ Release check passed. All tests, linting (ruff + pyright), and package checks are green.

   **If something fails:**
   > ❌ Release check failed.
   
   List each failing check with:
   - What failed (test name, lint rule, file, or a mistyped or non-conventional commit title)
   - A brief explanation of the issue
   - A suggested fix

4. If tests fail, offer to run the specific failing test in verbose mode:
   ```bash
   make test/verbose
   ```

5. If lint fails, offer to auto-fix:
   ```bash
   make format
   ```

## What `make can-release` checks

- `make lint` — ruff check + pyright type checking
- `make test` — full pytest suite
- Code formatting compliance
