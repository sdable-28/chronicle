# Contributing to Chronicle

Thank you for your interest in contributing.

## Getting started

```bash
git clone https://github.com/sdable-28/chronicle.git
cd chronicle
pip install -e ".[embed]"
```

## Submitting changes

1. Fork the repo and create a branch from `main`.
2. Make your changes — keep them focused on a single concern.
3. Test manually: `chronicle init`, `chronicle brief`, `chronicle search "..."`.
4. Open a pull request with a clear description of what changed and why.

## Reporting bugs

Open a GitHub Issue. Include:
- Chronicle version (`pip show ai-chronicle`)
- Python version
- What you ran and what you expected vs what happened

## Code style

- Python 3.12+
- No external dependencies beyond `click` (core) and `sqlite-vec`/`sentence-transformers` (optional embed)
- Keep CLI commands self-contained — one command, one concern
