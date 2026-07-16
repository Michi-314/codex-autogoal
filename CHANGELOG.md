# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] - 2026-07-16

### Security

- Detect multiply-linked regular files during legacy control-home migration and quarantine the entire home.
- Reject control-file reads, writes, appends, markers, and locks unless the inode has exactly one hard link.
- Validate lock type, owner, mode, and link count before truncating or writing its PID.
- Skip all control-home scanning and permission changes when the global Stop Hook is not enabled for a session.

## [0.1.2] - 2026-07-16

### Security

- Quarantine the entire legacy control home before use when any symlink is present, including symlinked `state` or `jobs` roots.
- Replace remaining control-log and doctor writes with no-follow private I/O.
- Use an environment allowlist for Codex, watcher, and detached jobs by default; require `--inherit-env` for full job inheritance.
- Remove control-home paths and raw job logs from automatic resume messages.
- Document that control files remain readable to same-user Codex sandboxes and that detached jobs run outside the Codex sandbox.

## [0.1.1] - 2026-07-16

### Security

- Remove `--add-dir` access to the AutoGoal control home, eliminating the model-to-hook trust-boundary reversal.
- Disable WezTerm keystroke resume entirely and require headless resume.
- Stop loading mutable protocol instructions from runtime state.
- Use no-follow, owner/mode/type-checked I/O for trusted control files.
- Refuse recursive uninstall unless a validated managed-install sentinel is present.
- Reject invalid session and job identifiers at every filesystem boundary.
- Store runtime state and logs with user-only permissions and migrate existing state.
- Reject legacy visible-resume state instead of sending terminal input.
- Verify process identity before killing a detached job and enforce a configurable job-log limit.
- Pin CI actions by commit SHA and add CodeQL scanning.

## [0.1.0] - 2026-07-15

### Added

- Stop-hook-driven autonomous continuation with bounded loop detection.
- Detached background command and timer jobs.
- Completion watcher with headless Codex resume and optional visible WezTerm resume.
- Session status, logs, cancellation, recovery, and environment diagnostics.
- Path, job identifier, and symlink traversal guards.
- Unit and fake-Codex integration tests.

[Unreleased]: https://github.com/Michi-314/codex-autogoal/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/Michi-314/codex-autogoal/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Michi-314/codex-autogoal/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Michi-314/codex-autogoal/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Michi-314/codex-autogoal/releases/tag/v0.1.0
