# Security Policy

## Supported versions

AutoGoal is currently alpha software. Security fixes are applied to the latest `main` branch
and the most recent release only.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose credentials, escape a
sandbox, execute an unintended command, or resume the wrong Codex session. Use GitHub's
private vulnerability reporting for this repository. If that feature is unavailable, contact
the repository owner privately through the contact method shown on their GitHub profile.

Include the affected version, operating system, Codex CLI version, reproduction steps, and
impact. Remove tokens, prompts, session state, job logs, home-directory paths, and other
personal data before sharing evidence.

## Security model

AutoGoal is an orchestration helper, not a security boundary. It launches Codex CLI and user
commands with the permissions selected by the user. Its PreToolUse hook only blocks obvious
long waits and polling patterns; it cannot prove that arbitrary shell input is safe.

The default sandbox is `workspace-write`. Review all hooks and prompts before installation,
and do not use `--bypass-hook-trust` or `danger-full-access` unless the environment and task
have been independently audited.
