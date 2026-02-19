# Release Process

This project uses a lightweight release hygiene checklist.

## Versioning

- Use Semantic Versioning: `MAJOR.MINOR.PATCH`.
- Update `CHANGELOG.md` before tagging a release.

## Pre-release Checklist

1. Ensure working tree is clean (except intended release changes).
2. Update docs/config examples if behavior changed.
3. Run checks locally:
   - `python -m compileall chronicle_keeper`
   - `python -m pytest -q`
4. Confirm CI is green on target commit.
   - Includes secret scanning workflow.
5. Move relevant notes from `Unreleased` to the new release section in `CHANGELOG.md`.

## Tagging

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

## GitHub Release Notes

Include:
- highlights
- migration notes (if any)
- known limitations
- rollback guidance
