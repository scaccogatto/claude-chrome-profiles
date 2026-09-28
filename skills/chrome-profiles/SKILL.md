---
name: chrome-profiles
description: >-
  Pick the right Chrome browser when several Chrome profiles (Google accounts) run
  Claude in Chrome. Use before the first mcp__claude-in-chrome__* call of a task,
  whenever more than one browser is connected, when a browser has to be selected or
  switched, or when the user asks which Chrome/profile/account to use. Identifies
  browsers by the signed-in Google account and enforces the account each repo expects.
user-invocable: true
allowed-tools: Bash
---

# Chrome profiles

A browser is identified by the **Google account signed into its Chrome profile**, never
by the extension name, device name, or local profile label: labels are not unique and
two profiles often share one.

Everything goes through the resolver. It is read-only toward Chrome (it parses the
profile's LevelDB storage without locks or writes) and caches deviceIds in
`~/.claude/chrome-devices.json`.

```sh
MAP="${CLAUDE_PLUGIN_ROOT}/skills/chrome-profiles/scripts/chrome-browser-map.py"
```

Output is tab-separated: `deviceId  profile-dir  name  email  gaia-id  source`.

## 1. Resolve the connected browsers

1. Call `mcp__claude-in-chrome__list_connected_browsers` to get the deviceIds.
2. Run `python3 "$MAP" --resolve <deviceId> [<deviceId> ...]`.
   - Exit 0: every deviceId maps to an account.
   - Exit 1: a line `UNKNOWN <deviceId>` is followed by `CANDIDATE` rows (profiles not
     yet claimed, most recently active first). **An unmapped deviceId is a question for
     the user, never a guess.** Show the candidates, ask which profile that browser is,
     then record the answer with `python3 "$MAP" --set <deviceId> <profile-dir>`.

## 2. Find the account this repo expects

Run `python3 "$MAP" --for-path "$PWD"`.

| Exit | Output | Meaning |
|---|---|---|
| 0 | profile row | The repo expects this account |
| 1 | `NO-PROFILE <email>` | The repo expects an account no Chrome profile is signed into: tell the user |
| 2 | error on stderr | Not a git repository |
| 3 | `UNSET <repo>` | The repo has no account set: **setup required** |

**Setup (exit 3) comes before any browser action.** Do not open tabs, navigate, or pick a
browser "for now":

1. List the accounts with `python3 "$MAP"` (column 4 is the email).
2. Ask the user which account this repo uses.
3. Record it with `python3 "$MAP" --assign <repo> <email>`. It is stored in the repo's
   local git config (`chrome-profiles.account`): per user, never committed, and shared by
   every worktree of the repo.

**Not a git repository (exit 2):** ask the user which account to use for this session.
Nothing is persisted.

## 3. Select the browser

Select the connected browser whose account (step 1) matches the expected one (step 2).

If the expected account is not connected, say which account is expected and which are
connected, then offer:

1. **Open Chrome on that profile**: `open -na "Google Chrome" --args --profile-directory="<profile-dir>"`,
   then poll `list_connected_browsers` every 3s for up to 30s. If it never appears, ask
   the user once (the extension may be signed out or paused on that profile).
2. **Use a connected browser** anyway, for this task only.
3. **Let the user pick.**

## Other modes

| Command | Use |
|---|---|
| `--device <uuid>` | Print the account email of one deviceId |
| `--forget <uuid>` | Drop a wrong cache entry, then resolve again |
| `--help` | Full usage and exit codes |
