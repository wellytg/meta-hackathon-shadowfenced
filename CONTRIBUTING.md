# Contributing

Testing and contributions are welcome. This repo is docs-plus-code from a
hackathon. Small, careful changes are best.

## Setup and tests

Follow `setup/AGENT_SETUP.md` to set up from a fresh clone. Then run:

```powershell
python -m pytest -q -p no:cacheprovider
```

The suite needs no network and no GPU. It must pass before you open a PR.

## Synthetic data only

Never use real keys, real people, or real company data. Use the fictional org
and minted canaries from the corpus and tests.

The repo guard test fails on key shapes in tracked files. See
`tests/test_repo_guard.py`. If your change trips it, replace the value with
synthetic data.

## One change per PR

Keep each PR to one change. Include tests for the new behavior. Match the
plain style of the nearby code and docs. Short sentences. No hype.

## Good first areas

- New detectors for Tier 1 (new PII or secret shapes, with tests).
- DOCX parts the file guard does not scan yet.
- More AI sites in the extension (it covers four today; see
  `extension/README.md`).
- Red-team cases: new leak or evasion rows for the test sets.

Open an issue first if the change is large. Link the issue in the PR.
