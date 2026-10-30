# Security Policy

## Supported versions

Only the latest release receives fixes.

| Version | Supported |
|---------|-----------|
| 0.1.x (latest) | Yes |
| Older | No |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: go to the repository's **Security** tab → **Report a vulnerability**. This opens a private advisory visible only to the maintainer.

Include what you can:

- the command or file affected
- what an attacker could do, and the steps to reproduce it
- the version or commit you tested

You can expect an acknowledgement within 7 days. Confirmed issues are fixed in a new release, and the advisory is published once a fix is out. Credit is given unless you ask otherwise.

## What is in scope

Chronicle is a local CLI that keeps a SQLite database and wires hooks into Claude Code. Worth reporting:

- **Hook behavior.** `chronicle setup-hooks` adds two hooks to `~/.claude/settings.json`: SessionStart runs `chronicle brief`, Stop runs `chronicle ingest --pending`. Anything those hooks do beyond that — network access, running other commands, changing unrelated settings — is a bug.
- **Injection through ingested text.** Text from a Claude response or a log file is stored and later shown back to Claude in the session brief. A way to make ingested text execute code, escape the SQL layer, or break out of the brief's formatting to plant instructions is in scope.
- **Files written outside Chronicle's own locations:** `~/.chronicle/`, plus `~/.claude/settings.json` and `~/.claude/CLAUDE.md` (skip with `--no-claude-md`) when you run `setup-hooks`.
- **Package integrity.** A published package that does not match the tagged source in this repository.

Out of scope: what a model decides to do with the brief, and the contents of your own database.

## Notes for users

- **Your database is personal data.** `~/.chronicle/chronicle.db` holds your decisions and experiment notes. Do not commit it; the repository's `.gitignore` excludes `*.db`.
- **Review hooks before installing them.** `setup-hooks` changes how Claude Code behaves at session start and stop. Read the hook commands it writes.
- **Optional embeddings download a model.** The `[embed]` extra installs `sentence-transformers`, which downloads model weights on first use. The core CLI makes no network calls; the only external program it runs is local `git` (to follow renamed files when checking wired locations).
