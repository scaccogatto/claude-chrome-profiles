#!/usr/bin/env python3
"""
Map Claude-in-Chrome extension deviceIds to Chrome profiles and accounts.
Strictly read-only toward Chrome: parses LevelDB storage without locks or writes.
Cache-first: remembers resolved deviceIds in ~/.claude/chrome-devices.json.
The cache is written on --set and --forget, and also opportunistically whenever
a LevelDB association is discovered that is not yet cached.
The account a repository expects lives in its local git config
(chrome-profiles.account): per repo, per user, never committed, and shared by
every worktree of the repo. Works while Chrome is running.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
CHROME_ROOT = Path(os.path.expanduser(os.getenv("CHROME_ROOT", "~/Library/Application Support/Google/Chrome")))
CACHE_FILE = Path(os.path.expanduser(os.getenv("CHROME_MAP_CACHE", "~/.claude/chrome-devices.json")))
CONFIG_KEY = "chrome-profiles.account"

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The extension writes bridgeDeviceId once, at pairing. LevelDB later compacts it
# into a Snappy block, so this scan only finds it in the window before compaction.
DEVICE_ID_PATTERN = re.compile(
    rb"bridgeDeviceId.{0,8}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
# Characters that would break the tab-separated layout, dropped for display only.
# The cache keeps the original text; json.dump escapes it safely.
CLEAN = str.maketrans({'"': None, "\\": None, "\n": None, "\r": None, "\t": " "})

USAGE = """\
Usage: chrome-browser-map.py [OPTIONS]
  Default: list all Claude-in-Chrome profiles (tab-separated: deviceId, profile dir, name, email, source)
  --device UUID: print account email for UUID, exit 0; exit 1 if not found
  --resolve UUID [UUID ...]: resolve deviceIds, print rows (cache-first), then CANDIDATES block if any unknown; exit 0 if all known, 1 if any unknown
  --set UUID PROFILE_DIR: write cache entry, exit 0; exit 2 if profile_dir invalid or UUID malformed
  --forget UUID: remove cache entry, exit 0
  --for-path DIR: print profile and account the repo at DIR expects, exit 0; exit 1 if no profile has that account,
      exit 2 if DIR is not a git repo, exit 3 (UNSET line) if the repo has no account set
  --assign DIR EMAIL: set the account the repo at DIR expects (local git config), print its row, exit 0;
      exit 2 if DIR is not a git repo or no profile has EMAIL signed in
  --self-check: run offline self-test, exit 0 or 1 with assertion text
  -h, --help: show this message
"""

Profile = namedtuple("Profile", "dir gaia_name email gaia_id devices")


def usage():
    sys.stderr.write(USAGE)


def fail(msg):
    sys.stderr.write(f"chrome-browser-map: error: {msg}\n")


# --- cache -----------------------------------------------------------------

def load_cache():
    """Cached deviceId map, or {} when absent or unreadable."""
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def write_cache(cache):
    """Replace the cache file atomically, so a crash never truncates it."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=CACHE_FILE.parent, delete=False, suffix=".tmp")
    try:
        json.dump(cache, tmp, indent=2)
        tmp.write("\n")
        tmp.close()
        os.replace(tmp.name, CACHE_FILE)
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# --- Chrome (read-only) ----------------------------------------------------

def ensure_local_state():
    if not (CHROME_ROOT / "Local State").is_file():
        fail(f"Local State not found at {CHROME_ROOT / 'Local State'}")
        sys.exit(1)


def info_cache():
    """profile.info_cache from Local State, keyed by profile directory."""
    try:
        with open(CHROME_ROOT / "Local State") as f:
            return json.load(f).get("profile", {}).get("info_cache", {})
    except Exception as e:
        fail(f"reading Local State: {e}")
        sys.exit(1)


def account_of(entry):
    """
    Google account identity for one info_cache entry.
    Never falls back to entry["name"], the local Chrome label, which is not unique.
    """
    email = entry.get("user_name", "")
    return (entry.get("gaia_name") or email or "-", email, entry.get("gaia_id", ""))


def settings_dir(profile_dir):
    return CHROME_ROOT / profile_dir / "Local Extension Settings" / EXTENSION_ID


def extension_installed(profile_dir):
    return (CHROME_ROOT / profile_dir / "Extensions" / EXTENSION_ID).is_dir() or settings_dir(profile_dir).is_dir()


def storage_files(profile_dir):
    """Extension storage files, newest first."""
    d = settings_dir(profile_dir)
    if not d.is_dir():
        return []
    try:
        files = [f for f in d.iterdir() if f.is_file()]
    except OSError:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files


def device_ids(profile_dir):
    """deviceIds found in this profile's storage; first file with any match wins."""
    for f in storage_files(profile_dir):
        if f.suffix not in (".ldb", ".log"):
            continue
        try:
            matches = DEVICE_ID_PATTERN.findall(f.read_bytes())
        except OSError:
            continue
        if matches:
            return [m.decode("ascii") for m in matches]
    return []


def storage_mtime(profile_dir):
    files = storage_files(profile_dir)
    return files[0].stat().st_mtime if files else 0


def _merge_new_leveldb_devices(profiles):
    """
    For each Profile with LevelDB deviceIds not yet in the cache,
    add a cache entry. Updates the cache file only if new associations
    were found; otherwise leaves the file untouched.
    Swallows OSError to remain opportunistic.
    """
    cache = load_cache()
    to_add = {}

    for profile in profiles:
        for dev in profile.devices:
            if dev not in cache:
                to_add[dev] = {
                    "profile": profile.dir,
                    "gaia_name": profile.gaia_name,
                    "email": profile.email,
                    "gaia_id": profile.gaia_id,
                }

    if not to_add:
        return  # Nothing new, don't touch the file

    # Merge and write atomically
    new_cache = {**cache, **to_add}
    try:
        write_cache(new_cache)
    except OSError:
        # Opportunistic: swallow the error, the command still works
        pass


def scan_profiles():
    """Every profile with the extension installed, with its LevelDB deviceIds."""
    profiles = [
        Profile(d, *account_of(entry), device_ids(d))
        for d, entry in sorted(info_cache().items())
        if extension_installed(d)
    ]
    _merge_new_leveldb_devices(profiles)
    return profiles


# --- output ----------------------------------------------------------------

def row(device_id, profile_dir, gaia_name, email, gaia_id, source):
    fields = (device_id, profile_dir, gaia_name, email, gaia_id, source)
    return "\t".join(str(f).translate(CLEAN) for f in fields)


def cache_row(uuid, entry, source="cache"):
    return row(uuid, entry.get("profile", ""), entry.get("gaia_name", ""),
               entry.get("email", ""), entry.get("gaia_id", ""), source)


# --- modes -----------------------------------------------------------------

def list_profiles():
    cache = load_cache()
    cached_dirs = {e.get("profile") for e in cache.values()}
    rows = []

    for p in scan_profiles():
        for uuid, entry in cache.items():
            if entry.get("profile") == p.dir:
                rows.append((p.dir, cache_row(uuid, entry)))
        for dev in p.devices:
            if dev not in cache:
                rows.append((p.dir, row(dev, p.dir, p.gaia_name, p.email, p.gaia_id, "leveldb")))
        if not p.devices and p.dir not in cached_dirs:
            rows.append((p.dir, row("-", p.dir, p.gaia_name, p.email, p.gaia_id, "-")))

    rows.sort(key=lambda r: r[0])
    for _, line in rows:
        print(line)
    return 0


def lookup_device(uuid):
    entry = load_cache().get(uuid)
    if entry:
        print(entry.get("email"))
        return 0

    for p in scan_profiles():
        if uuid in p.devices:
            print(p.email)
            return 0
    return 1


def resolve_devices(*uuids):
    cache = load_cache()
    profiles = scan_profiles()
    unknown = []
    # Profiles accounted for by a row printed in THIS invocation cannot also own
    # one of the unknown ids, so they are excluded from the candidate list.
    resolved_here = set()

    for uuid in uuids:
        entry = cache.get(uuid)
        if entry:
            print(cache_row(uuid, entry))
            resolved_here.add(entry.get("profile"))
            continue

        match = next((p for p in profiles if uuid in p.devices), None)
        if match:
            print(row(uuid, match.dir, match.gaia_name, match.email, match.gaia_id, "leveldb"))
            resolved_here.add(match.dir)
            continue

        print(f"UNKNOWN\t{uuid}")
        unknown.append(uuid)

    if not unknown:
        return 0

    claimed = {e.get("profile") for e in cache.values()} | resolved_here
    candidates = sorted(
        ((storage_mtime(p.dir), p) for p in profiles if p.dir not in claimed),
        key=lambda c: c[0],
        reverse=True,
    )
    for mtime, p in candidates:
        when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(row("CANDIDATE", p.dir, p.gaia_name, p.email, p.gaia_id, when))
    return 1


def set_device(uuid, profile_dir):
    if not UUID_PATTERN.match(uuid):
        fail(f"invalid UUID format: {uuid}")
        return 2

    entry = info_cache().get(profile_dir)
    if entry is None:
        fail(f"profile not found in Local State: {profile_dir}")
        return 2

    gaia_name, email, gaia_id = account_of(entry)
    if not email:
        fail(f"profile has no Google account signed in: {profile_dir}")
        return 2

    cache = load_cache()
    cache[uuid] = {"profile": profile_dir, "gaia_name": gaia_name, "email": email, "gaia_id": gaia_id}
    write_cache(cache)
    print(cache_row(uuid, cache[uuid]))
    return 0


def forget_device(uuid):
    cache = load_cache()
    cache.pop(uuid, None)
    write_cache(cache)
    return 0


def git(directory, *args):
    """(returncode, stdout) of one git command run in directory; never raises."""
    try:
        r = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)
    except OSError as e:
        return 127, str(e)
    return r.returncode, r.stdout.strip()


def repo_root(directory):
    """Top level of the git repo containing directory, or None with an error already reported."""
    code, out = git(os.path.expanduser(directory), "rev-parse", "--show-toplevel")
    if code != 0:
        fail(f"not a git repository: {directory}")
        return None
    return out


def for_path(directory):
    root = repo_root(directory)
    if root is None:
        return 2

    # --local only: a global default would silently skip the per-repo setup.
    # Worktrees share the repo's local config, so every worktree sees the value.
    code, email = git(root, "config", "--local", "--get", CONFIG_KEY)
    if code == 1:
        print(f"UNSET\t{root}")
        return 3
    if code != 0 or not email:
        fail(f"reading {CONFIG_KEY} in {root}")
        return 2
    return print_account(email)


def assign(directory, email):
    root = repo_root(directory)
    if root is None:
        return 2

    if not any(account_of(entry)[1] == email for entry in info_cache().values()):
        fail(f"no Chrome profile has {email} signed in")
        return 2

    code, _ = git(root, "config", "--local", CONFIG_KEY, email)
    if code != 0:
        fail(f"writing {CONFIG_KEY} in {root}")
        return 2
    return print_account(email)


def print_account(email):
    """
    Print the row for the profile signed into email: a cached deviceId first,
    then any profile in Local State. NO-PROFILE and exit 1 when none has it.
    """
    for uuid, entry in load_cache().items():
        if entry.get("email") == email:
            print(cache_row(uuid, entry))
            return 0

    for profile_dir, entry in info_cache().items():
        gaia_name, found_email, gaia_id = account_of(entry)
        if found_email == email:
            print(row("-", profile_dir, gaia_name, email, gaia_id, "-"))
            return 0

    print(f"NO-PROFILE\t{email}")
    return 1


# --- self-check ------------------------------------------------------------

class Failed(Exception):
    """One assertion did not hold."""


def need(condition, message):
    if not condition:
        raise Failed(f"assertion failed: {message}")


SYNTHETIC_PROFILES = {
    "Default": {"name": "Default", "user_name": "test@example.com",
                "gaia_id": "123456789", "gaia_name": "Test User"},
    "Profile 1": {"name": "Work", "user_name": "profile1@example.com",
                  "gaia_id": "987654321", "gaia_name": "Profile 1 User"},
    "Profile 2": {"name": "Work", "user_name": "profile2@example.com",
                  "gaia_id": "555555555", "gaia_name": "Profile 2 User"},
    "Profile 3": {"name": "Special Chars", "user_name": "special@example.com",
                  "gaia_id": "333333333",
                  "gaia_name": 'Name with "quotes" and \\ backslash\nand newline'},
    "Profile 4": {"name": "Empty Account", "user_name": "",
                  "gaia_id": "444444444", "gaia_name": "No Account"},
}

UUID_DEFAULT = "12345678-1234-5678-1234-567812345678"
UUID_P1 = "87654321-4321-8765-4321-876543218765"
UUID_ABSENT = "00000000-0000-0000-0000-000000000000"


def build_fixture(root):
    """Synthetic Chrome tree: five profiles, two with a discoverable deviceId."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "Local State", "w") as f:
        json.dump({"profile": {"info_cache": SYNTHETIC_PROFILES}}, f)

    for name in SYNTHETIC_PROFILES:
        (root / name / "Local Extension Settings" / EXTENSION_ID).mkdir(parents=True, exist_ok=True)

    for name, uuid in (("Default", UUID_DEFAULT), ("Profile 1", UUID_P1)):
        ldb = root / name / "Local Extension Settings" / EXTENSION_ID / "000001.ldb"
        ldb.write_bytes(b"someprefix\x01\x02\x03bridgeDeviceId\x01\x02" + uuid.encode() + b"\x00suffix")


def self_check():
    """Offline self-test against a synthetic Chrome tree, cache and git repos."""
    global CHROME_ROOT, CACHE_FILE
    saved = (CHROME_ROOT, CACHE_FILE)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        CHROME_ROOT = tmp / "chrome"
        CACHE_FILE = tmp / "cache" / "chrome-devices.json"
        try:
            build_fixture(CHROME_ROOT)
            run_checks(tmp)
            return True, None
        except Failed as e:
            return False, str(e)
        except Exception as e:  # a crash is a failed self-check, not a traceback
            return False, f"assertion failed: {e}"
        finally:
            CHROME_ROOT, CACHE_FILE = saved


def run_checks(tmp):
    script = str(Path(__file__).absolute())
    # Git is isolated from the user's global and system config, and cannot
    # discover a repo above tmp, so the checks see only the repos they create.
    env = {**os.environ,
           "CHROME_ROOT": str(CHROME_ROOT),
           "CHROME_MAP_CACHE": str(CACHE_FILE),
           "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CEILING_DIRECTORIES": str(tmp),
           "GIT_AUTHOR_NAME": "self-check", "GIT_AUTHOR_EMAIL": "self-check@example.com",
           "GIT_COMMITTER_NAME": "self-check", "GIT_COMMITTER_EMAIL": "self-check@example.com"}

    def run(*args):
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True, env=env)
        return r.stdout, r.returncode

    def git_ok(directory, *args):
        r = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, env=env)
        need(r.returncode == 0, f"fixture git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout.strip()

    def cache_json():
        with open(CACHE_FILE) as f:
            return json.load(f)

    repo = tmp / "work" / "repo"
    plain = tmp / "work" / "plain"
    worktree = tmp / "work" / "repo-wt"
    (repo / "a" / "b").mkdir(parents=True)
    plain.mkdir(parents=True)
    git_ok(repo, "init", "-q")
    git_ok(repo, "commit", "-q", "--allow-empty", "-m", "fixture")

    # --- listing and lookup, before anything is cached ---
    out, _ = run()
    need(UUID_DEFAULT in out, "UUID not in output")
    need("Test User" in out and "test@example.com" in out,
         "Default profile, gaia_name or email not in output")
    need(len(out.strip().splitlines()[0].split("\t")) == 6, "listing rows should have 6 columns")

    out, code = run("--device", UUID_DEFAULT)
    need(code == 0 and out.strip() == "test@example.com", f"device lookup returned '{out.strip()}'")
    need(run("--device", UUID_ABSENT)[1] == 1, "non-existent device should exit 1")

    # Two profiles share the local label "Work"; only the account tells them apart.
    out, _ = run()
    need("profile1@example.com" in out and "profile2@example.com" in out,
         "both profiles should appear in listing")
    need("987654321" in out and "555555555" in out,
         "profiles with same local name should be distinguished by email/gaia_id")

    # --- --for-path and --assign ---
    out, code = run("--for-path", str(repo / "a" / "b"))
    need(code == 3 and out.strip() == f"UNSET\t{repo}", f"repo with no account should print UNSET and exit 3, got {code}: {out!r}")

    need(run("--for-path", str(plain))[1] == 2, "--for-path outside a git repo should exit 2")
    need(run("--assign", str(plain), "test@example.com")[1] == 2, "--assign outside a git repo should exit 2")
    need(run("--assign", str(repo), "nobody@example.com")[1] == 2, "--assign with an account no profile has should exit 2")
    need(run("--for-path", str(repo))[1] == 3, "a rejected --assign must not write the account")

    out, code = run("--assign", str(repo), "profile2@example.com")
    cols = out.strip().split("\t")
    need(code == 0, f"--assign with a signed-in account should succeed, got {code}")
    need(len(cols) == 6, "--assign output should have 6 columns")
    need(cols[0] == "-" and cols[5] == "-",
         f"Local State-only profile should have - for deviceId and source, got: {out}")
    need(git_ok(repo, "config", "--local", "--get", CONFIG_KEY) == "profile2@example.com",
         "--assign should write the account to the repo's local git config")

    out, code = run("--for-path", str(repo / "a" / "b"))
    need(code == 0 and "profile2@example.com" in out, f"a subdirectory should resolve its repo's account, got: {out}")

    git_ok(repo, "worktree", "add", "-q", str(worktree))
    out, code = run("--for-path", str(worktree))
    need(code == 0 and "profile2@example.com" in out, f"a worktree should inherit the repo's account, got: {out}")

    git_ok(repo, "config", "--local", CONFIG_KEY, "nobody@example.com")
    out, code = run("--for-path", str(repo))
    need(code == 1 and out.startswith("NO-PROFILE\t"),
         "an account no profile has should return NO-PROFILE and exit 1")

    # --- cache writes ---
    need(run("--set", UUID_DEFAULT, "Default")[1] == 0, "--set command failed")
    out, code = run("--device", UUID_DEFAULT)
    need(code == 0 and out.strip() == "test@example.com", f"cache --device lookup returned '{out.strip()}'")

    run("--assign", str(repo), "test@example.com")
    out, code = run("--for-path", str(repo))
    cols = out.strip().split("\t")
    need(code == 0, "--for-path with cached profile should succeed")
    need(cols[0] == UUID_DEFAULT and cols[5] == "cache",
         "--for-path with cached profile should show deviceId and source cache")

    # A cache entry shadows the LevelDB row for the same deviceId.
    run("--set", UUID_P1, "Profile 2")
    out, _ = run()
    matching = [l for l in out.splitlines() if l.startswith(UUID_P1)]
    need(len(matching) == 1, f"shadow test, expected 1 row for {UUID_P1}, got {len(matching)}")
    need(matching[0].split("\t")[5] == "cache",
         f"shadow test, expected source=cache, got {matching[0].split(chr(9))[5]}")

    # --- --resolve ---
    out, code = run("--resolve", UUID_DEFAULT, UUID_ABSENT)
    need(UUID_DEFAULT in out, "--resolve should show known UUID from cache")
    need(f"UNKNOWN\t{UUID_ABSENT}" in out, "--resolve should show UNKNOWN line")
    need("CANDIDATE" in out, "--resolve should show CANDIDATES block")
    need(code == 1, "--resolve should exit 1 when unknown present")
    need(not any(l.startswith("CANDIDATE\tDefault\t") for l in out.splitlines()),
         "--resolve should not offer a profile already resolved in this run")

    # --- rejected writes ---
    need(run("--set", UUID_ABSENT, "No Such Profile")[1] == 2, "--set with bogus profile should exit 2")
    need(run("--set", UUID_ABSENT, "Profile 4")[1] == 2, "--set with empty user_name should exit 2")
    need(run("--set", "not-a-uuid", "Default")[1] == 2, "--set with malformed UUID should exit 2")

    # --- names with characters that would break the layout ---
    special = "11111111-2222-3333-4444-555555555555"
    out, code = run("--set", special, "Profile 3")
    need(code == 0, "--set with special chars in gaia_name failed")
    need(len(out.strip().split("\t")) == 6, f"special char row should have 6 columns, got {out!r}")
    need(cache_json()[special]["gaia_name"] == SYNTHETIC_PROFILES["Profile 3"]["gaia_name"],
         "cache should keep the original name verbatim")
    out, _ = run()
    need("special@example.com" in out, "special char profile should appear in listing")
    need(all(len(l.split("\t")) == 6 for l in out.splitlines()),
         "special characters must not break the tab layout")

    # --- --forget ---
    need(UUID_DEFAULT in cache_json(), "before --forget, entry should exist")
    run("--forget", UUID_DEFAULT)
    need(UUID_DEFAULT not in cache_json(), "--forget should remove entry")
    need(isinstance(cache_json(), dict), "cache file is not valid JSON")

    # --- LevelDB auto-caching ---

    # Clear cache to start fresh
    CACHE_FILE.unlink(missing_ok=True)

    # Assertion 1: LevelDB discovery caches deviceId
    out, code = run()
    need(code == 0, "listing should exit 0")
    cache = cache_json()
    need(UUID_DEFAULT in cache, f"UUID_DEFAULT should be auto-cached after listing, got {list(cache.keys())}")
    entry = cache[UUID_DEFAULT]
    need(entry.get("profile") == "Default", f"Default deviceId should map to Default profile, got {entry}")
    need(entry.get("email") == "test@example.com", f"Default email should be cached, got {entry}")
    need(entry.get("gaia_id") == "123456789", f"Default gaia_id should be cached, got {entry}")

    # Assertion 2: Idempotency, second listing doesn't rewrite cache
    cache_bytes_1 = CACHE_FILE.read_bytes()
    cache_mtime_1 = CACHE_FILE.stat().st_mtime
    time.sleep(0.01)  # Ensure time has passed
    out2, code2 = run()
    need(code2 == 0, "second listing should exit 0")
    cache_bytes_2 = CACHE_FILE.read_bytes()
    cache_mtime_2 = CACHE_FILE.stat().st_mtime
    need(cache_bytes_1 == cache_bytes_2, "cache file bytes should be identical on second listing")
    need(cache_mtime_1 == cache_mtime_2, "cache file mtime should not change on second listing")

    # Assertion 3: Cache entry is not overwritten by LevelDB
    # Set a cache entry for UUID_P1 pointing to Profile 2 (different from LevelDB)
    run("--set", UUID_P1, "Profile 2")
    out3, _ = run()
    lines = [l for l in out3.splitlines() if l.startswith(UUID_P1)]
    need(len(lines) == 1, f"should show UUID_P1 once, got {len(lines)}: {lines}")
    need("Profile 2" in lines[0], f"cached entry should override LevelDB, got {lines[0]}")
    need(lines[0].split("\t")[5] == "cache", f"source should be cache, got {lines[0].split(chr(9))[5]}")
    cache = cache_json()
    need(cache[UUID_P1].get("profile") == "Profile 2", "cache should not be overwritten by LevelDB")

    # Assertion 4: Forget and re-learn from LevelDB
    need(UUID_P1 in cache_json(), "precondition: UUID_P1 should be in cache")
    run("--forget", UUID_P1)
    cache = cache_json()
    need(UUID_P1 not in cache, "after --forget, UUID_P1 should not be in cache")
    out4, _ = run()
    lines = [l for l in out4.splitlines() if l.startswith(UUID_P1)]
    need(len(lines) == 1, f"should re-learn from LevelDB, got {len(lines)}: {lines}")
    need(lines[0].split("\t")[5] == "leveldb", f"source should be leveldb after forget, got {lines[0].split(chr(9))[5]}")
    cache = cache_json()
    need(UUID_P1 in cache, "listing should re-cache the forgotten deviceId")
    need(cache[UUID_P1].get("profile") == "Profile 1", "re-learned entry should have correct profile")

    # Assertion 5: Unwritable cache path, listing still works
    saved_cache_env = env["CHROME_MAP_CACHE"]
    try:
        # Point cache to a path under a non-writable location
        env["CHROME_MAP_CACHE"] = "/dev/null/chrome-devices.json"
        out5, code5 = run()
        need(code5 == 0, f"listing should exit 0 even with unwritable cache, got {code5}")
        need(UUID_DEFAULT in out5 or UUID_P1 in out5, "listing should print rows even with unwritable cache")
    finally:
        # Restore env and reset CACHE_FILE
        env["CHROME_MAP_CACHE"] = saved_cache_env

    # Assertion 6: --device mode also auto-caches LevelDB discovery
    CACHE_FILE.unlink(missing_ok=True)
    cache = {} if not CACHE_FILE.exists() else cache_json()
    need(UUID_DEFAULT not in cache, "precondition: UUID_DEFAULT should not be in empty cache")
    out6, code6 = run("--device", UUID_DEFAULT)
    need(code6 == 0, f"--device should find UUID_DEFAULT, got {code6}")
    need(out6.strip() == "test@example.com", f"--device should print email, got {out6}")
    cache = cache_json()
    need(UUID_DEFAULT in cache, "--device should auto-cache the LevelDB-discovered deviceId")
    need(cache[UUID_DEFAULT].get("profile") == "Default", "auto-cached entry should have correct profile")

    # --- "~" in path overrides expands against HOME ---
    home_env = {**env, "HOME": str(tmp), "CHROME_ROOT": "~/chrome", "CHROME_MAP_CACHE": "~/cache/chrome-devices.json"}
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=home_env)
    need(r.returncode == 0 and UUID_DEFAULT in r.stdout, f"~ in CHROME_ROOT should expand, got {r.stderr!r}")


# --- dispatch --------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args:
        ensure_local_state()
        return list_profiles()

    cmd, rest = args[0], args[1:]

    if cmd == "--self-check":
        ok, error = self_check()
        print("ok" if ok else error)
        return 0 if ok else 1

    if cmd in ("-h", "--help"):
        usage()
        return 0

    # Every remaining mode needs at least one argument.
    modes = {
        "--device": (lookup_device, 1, True),
        "--resolve": (resolve_devices, 1, True),
        "--set": (set_device, 2, True),
        "--forget": (forget_device, 1, False),
        "--for-path": (for_path, 1, True),
        "--assign": (assign, 2, True),
    }
    if cmd not in modes or len(rest) < modes[cmd][1]:
        usage()
        return 2

    handler, arity, needs_chrome = modes[cmd]
    if needs_chrome:
        ensure_local_state()
    return handler(*rest) if cmd == "--resolve" else handler(*rest[:arity])


if __name__ == "__main__":
    sys.exit(main())
