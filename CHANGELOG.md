# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Reject invalid session and job identifiers at every filesystem boundary.
- Store runtime state and logs with user-only permissions and migrate existing state.
- Verify WezTerm foreground process before visible resume and send a sanitized one-line message.
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

[Unreleased]: https://github.com/Michi-314/codex-autogoal/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Michi-314/codex-autogoal/releases/tag/v0.1.0
