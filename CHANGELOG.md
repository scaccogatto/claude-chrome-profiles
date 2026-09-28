# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-28

### Added
- **`chrome-profiles` skill.** Loads only when a Claude in Chrome task starts. It
  resolves connected browsers to their signed-in Google account, asks instead of
  guessing on an unmapped browser, and offers to open Chrome on the expected profile
  when that account is not connected.
- **Per-repo account in local git config** (`chrome-profiles.account`). The skill forces
  a one-time setup in any repo without one: it is per user, never committed, and shared
  by every worktree.
- **Resolver `chrome-browser-map.py`**, read-only toward Chrome, with `--for-path`,
  the new `--assign`, `--resolve`, `--set`, `--device`, `--forget` and an offline
  `--self-check` that CI runs on every push.

[0.1.0]: https://github.com/scaccogatto/claude-chrome-profiles/releases/tag/v0.1.0
