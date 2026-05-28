# Release process

**Matryca Plumber** (Marco Porcellato · [Matryca.ai](https://matryca.ai)) uses a **curated** [`CHANGELOG.md`](../CHANGELOG.md) (Keep a Changelog). GitHub Release notes are **not** auto-generated from commits — CI copies the matching changelog section when you push a `v*` tag.

---

## During development

Add user-facing bullets under **`## [Unreleased]`** (`Added` / `Changed` / `Fixed` / `Removed`). One line per notable change.

---

## Release day (local)

Replace `X.Y.Z` with the semver you are shipping (no `v` prefix in `pyproject.toml`; use `vX.Y.Z` for the git tag).

### 1. Prepare (Cursor or manual)

- [ ] Move everything from `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`
- [ ] Leave an empty `## [Unreleased]` section at the top
- [ ] Set `version = "X.Y.Z"` in `pyproject.toml`
- [ ] Run `uv lock`
- [ ] Run `make check` (or at minimum: `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src tests`)
- [ ] For performance-heavy releases (e.g. **1.8.x**): optionally run `make perf` (`pytest -m slow`) and note results in the GitHub release; see [`v1.8-OPTIMIZATION-PLAN.md`](v1.8-OPTIMIZATION-PLAN.md#verification-matrix)

**Cursor shortcut:** ask the agent to *“prepare release vX.Y.Z”* (see [`.cursor/rules/05-release-preparation.mdc`](../.cursor/rules/05-release-preparation.mdc)).

### 2. Verify release notes (optional but recommended)

```bash
python scripts/extract_changelog.py vX.Y.Z | less
```

You should see exactly the section that will appear on GitHub.

### 3. Commit, tag, push

```bash
git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "chore: release X.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

### 4. CI does the rest

On tag push, [`.github/workflows/release.yml`](../.github/workflows/release.yml):

1. Builds the Sovereign UI frontend
2. Builds sdist/wheel with `uv build`
3. Creates a GitHub Release with notes from `scripts/extract_changelog.py`
4. Publishes to PyPI (trusted publishing)

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Release workflow fails on “extract changelog” | Ensure `## [X.Y.Z]` exists in `CHANGELOG.md` and matches the tag (`v1.6.2` → section `[1.6.2]`). |
| PyPI version already exists | Bump patch version; never re-use a published version. |
| Notes on GitHub look wrong | Re-run locally: `python scripts/extract_changelog.py vX.Y.Z` and compare to the file. |

---

## Related

- [`CHANGELOG.md`](../CHANGELOG.md)
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — quality gates before tag
- [`scripts/extract_changelog.py`](../scripts/extract_changelog.py)
