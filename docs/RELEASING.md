# Releasing

1. Confirm the supported Codex CLI behavior with the smallest practical real smoke test.
2. Update the version in `pyproject.toml` and `src/codex_autogoal/__init__.py`.
3. Move the relevant entries from `Unreleased` in `CHANGELOG.md` into a dated release.
4. Run the release checks:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -e '.[dev]'
   python -m pytest -q
   python -m build
   ```

5. Inspect the source archive and wheel. They must not contain credentials, session state,
   job output, absolute home-directory paths, or generated caches.
6. Commit the version change, create a signed `vX.Y.Z` tag, and push the commit and tag.
7. Create a GitHub release from the matching changelog section.

Publishing to a package index is intentionally not automated in `0.1.0`. Add trusted
publishing only after the project name and release process have been verified.
