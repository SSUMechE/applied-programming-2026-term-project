"""Apply official supplied-file releases without replacing student work.

This maintenance command uses the existing Python and Git only. It never imports
student code, Genesis or PyTorch, installs packages, or changes the team's Git
history/index/remotes. Stop running project commands before updating.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

OFFICIAL_REPOSITORY = "https://github.com/SSUMechE/applied-programming-2026-term-project"
LEADERBOARD_URL = "https://phoniness-reenact-veggie.ngrok-free.dev"
LEGACY_TAG = "term-v3-student-20261114"
LEGACY_COMMIT = "b12401c71981b1e9192263697612c8bebc6263fa"
MANIFEST = "course_release.json"
SCHEMA = "ap-student-release-v1"
FIXED_RUNTIME = {"torch": "2.14.0+cu126", "genesis-world": "1.3.2"}
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 250 * 1024 * 1024
MAX_FILES = 2000
PRESERVED = {"student", "output", ".git"}
ENVIRONMENT_FILES = {"requirements.txt", "environment_reference.json", "environment_sdk_pins.json"}
ID_RE = r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"
TAG_RE = r"term-v[0-9]+-[A-Za-z0-9][A-Za-z0-9._-]{0,95}"

class UpdateError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def require(condition, code, message):
    if not condition:
        raise UpdateError(code, message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")

def decode(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            require(k not in result, "INVALID_JSON", "Duplicate JSON key.")
            result[k] = v
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("INVALID_JSON", "Invalid release or backup JSON.") from exc

def safe_name(name, *, internal=False):
    require(type(name) is str and 0 < len(name) <= 240, "UNSAFE_PATH", "Invalid relative path.")
    p = PurePosixPath(name)
    require(not p.is_absolute() and p.as_posix() == name and name != "." and
            all(part not in ("", ".", "..") for part in p.parts),
            "UNSAFE_PATH", "Paths must remain inside the project.")
    for part in p.parts:
        require(not re.search(r'[<>:"\\|?*\x00-\x1f]', part) and
                part == part.rstrip(" .") and
                not re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I),
                "UNSAFE_PATH", "Unsupported Windows path.")
    if not internal:
        require(p.parts[0].casefold() not in PRESERVED and
                not any(part.casefold().startswith(".env") or part.casefold().endswith(".token")
                        for part in p.parts),
                "PRESERVED_PATH", "A release cannot manage student work, output, Git data or credentials.")
    return p.parts

def checked_path(root, name, *, internal=False):
    parts = safe_name(name, internal=internal)
    path = root
    for part in parts:
        path = path / part
        if os.path.lexists(path):
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and
                    not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "UNSAFE_LINK", "Symbolic links and reparse points are not update targets.")
    return path

def file_bytes(root, name, *, internal=False):
    path = checked_path(root, name, internal=internal)
    if not path.exists():
        return None
    require(path.is_file(), "PATH_COLLISION", "Expected a regular file: " + name)
    require(path.stat().st_size <= MAX_FILE, "FILE_TOO_LARGE", "File too large: " + name)
    return path.read_bytes()

def atomic_write(root, name, data, *, internal=False):
    path = checked_path(root, name, internal=internal)
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_path(root, name, internal=internal)
    fd, tmp = tempfile.mkstemp(prefix=".ap-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(root, name, internal=internal)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def write_json(root, name, value, *, internal=True):
    atomic_write(root, name, json.dumps(value, indent=2, ensure_ascii=True).encode() + b"\n",
                 internal=internal)

def delete_file(root, name, *, internal=False):
    path = checked_path(root, name, internal=internal)
    if path.exists():
        require(path.is_file(), "PATH_COLLISION", "Expected a regular file: " + name)
        path.unlink()

def workspace(root):
    root = Path(root).absolute()
    require(root.is_dir() and (root / "student").is_dir() and (root / "project_run.py").is_file(),
            "PROJECT_ROOT_REQUIRED", "Run this command from your cloned project repository root.")
    require(not root.is_symlink() and not (getattr(root.stat(), "st_file_attributes", 0) & 0x400),
            "UNSAFE_LINK", "The project root must not be a reparse point.")
    checked_path(root, "student", internal=True)
    checked_path(root, "output/project_updates", internal=True).mkdir(parents=True, exist_ok=True)
    return root

@contextmanager
def update_lock(root):
    path = checked_path(root, "output/project_updates/update.lock", internal=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise UpdateError("UPDATE_BUSY", "Another update command is running.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

@contextmanager
def activity_lock(root, *, exclusive=False):
    """Shared for provided runs, exclusive for maintenance, released on process exit."""
    path = checked_path(root, "output/project_updates/execution.lock", internal=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                            ("hEvent", wintypes.HANDLE)]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            overlapped = Overlapped()
            acquired = kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0,
                                         ctypes.byref(overlapped))
            require(acquired, "UPDATE_BUSY", "A supplied project run or update is active. Stop it before maintenance.")
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                raise UpdateError("UPDATE_BUSY", "A supplied project run or update is active. Stop it before maintenance.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

def git(directory, *arguments, input_bytes=None):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        run = subprocess.run(["git", "-c", "core.longpaths=true", "-c", "core.autocrlf=false",
                              "-c", "credential.helper=", "-c", "http.extraheader=",
                              "-c", "core.hooksPath=/dev/null", "-C", str(directory), *arguments],
                             input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("RELEASE_FETCH_FAILED", "Git is unavailable or the release request timed out. Nothing was installed.") from exc
    require(run.returncode == 0, "RELEASE_FETCH_FAILED",
            "Git could not read the official release. Check connectivity and the announced release tag.")
    return run.stdout

class ReleaseSource:
    """Read official Git objects only. No checkout, hooks or downloaded imports."""
    def __init__(self, root):
        self.root = root
        self.cache = checked_path(root, "output/project_updates/cache.git", internal=True)
        if not self.cache.exists():
            self.cache.mkdir()
            empty = checked_path(root, "output/project_updates/empty-template", internal=True)
            empty.mkdir(exist_ok=True)
            git(self.cache, "init", "--bare", "--template=" + str(empty))

    def read(self, tag):
        require(type(tag) is str and re.fullmatch(TAG_RE, tag), "INVALID_RELEASE", "Use the instructor's exact release tag.")
        git(self.cache, "fetch", "--no-tags", "--no-recurse-submodules", OFFICIAL_REPOSITORY + ".git",
            "refs/tags/" + tag)
        commit = git(self.cache, "rev-parse", "FETCH_HEAD^{commit}").decode().strip()
        require(re.fullmatch(r"[0-9a-f]{40}", commit), "INVALID_RELEASE", "Release commit is invalid.")
        pin_name = "output/project_updates/tag-pins.json"
        pin_raw = file_bytes(self.root, pin_name, internal=True)
        pins = decode(pin_raw) if pin_raw else {}
        require(type(pins) is dict and (tag not in pins or pins[tag] == commit),
                "RELEASE_TAG_CHANGED", "An already observed release tag moved. Contact the instructor.")
        pins[tag] = commit
        write_json(self.root, pin_name, pins)
        tree = git(self.cache, "ls-tree", "-rz", "--full-tree", commit)
        entries = []
        folded = set()
        directory_cases = {}
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8")
            safe_name(name, internal=True)
            require(mode == "100644" and kind == "blob", "UNSAFE_RELEASE",
                    "Release contains a non-regular file.")
            require(name.casefold() not in folded, "UNSAFE_RELEASE", "Case-colliding release paths.")
            parts = PurePosixPath(name).parts
            for count in range(1, len(parts)):
                directory = "/".join(parts[:count])
                require(directory_cases.get(directory.casefold(), directory) == directory,
                        "UNSAFE_RELEASE", "Case-colliding release directories.")
                directory_cases[directory.casefold()] = directory
            folded.add(name.casefold())
            entries.append((name, oid))
        require(0 < len(entries) <= MAX_FILES, "UNSAFE_RELEASE", "Unexpected release size.")
        # Read all required blobs through one data-only Git process.
        data = git(self.cache, "cat-file", "--batch",
                   input_bytes=("".join(oid + "\n" for _, oid in entries)).encode())
        files = {}
        offset = total = 0
        for name, oid in entries:
            end = data.index(b"\n", offset)
            head = data[offset:end].decode("ascii").split()
            require(len(head) == 3 and head[0] == oid and head[1] == "blob",
                    "INVALID_RELEASE", "Git returned an unexpected object.")
            size = int(head[2])
            total += size
            require(0 <= size <= MAX_FILE and total <= MAX_TOTAL, "UNSAFE_RELEASE", "Release exceeds size limits.")
            offset = end + 1
            blob = data[offset:offset + size]
            require(len(blob) == size and data[offset + size:offset + size + 1] == b"\n",
                    "INVALID_RELEASE", "Truncated Git object.")
            files[name] = blob
            offset += size + 1
        require(offset == len(data), "INVALID_RELEASE", "Unexpected Git object data.")
        return {"tag": tag, "commit": commit, "files": files}

def managed_names(files):
    names = {}
    for name, data in files.items():
        if name == MANIFEST or PurePosixPath(name).parts[0].casefold() in PRESERVED:
            continue
        safe_name(name)
        names[name] = sha(data)
    return names

def validate_release(release, *, legacy=False):
    files = release["files"]
    if legacy:
        require(release["tag"] == LEGACY_TAG and release["commit"] == LEGACY_COMMIT and MANIFEST not in files,
                "LEGACY_VERSION_MISMATCH", "The legacy release does not match the frozen bootstrap version.")
        return {"release_tag": LEGACY_TAG, "managed_files": managed_names(files),
                "rules": extract_rules(files), "runtime_versions": FIXED_RUNTIME.copy()}
    require(MANIFEST in files, "RELEASE_MANIFEST_MISSING", "The announced release has no update manifest.")
    manifest = decode(files[MANIFEST])
    required = {"schema_version", "release_tag", "repository", "managed_files", "runtime_versions", "rules", "preserved_paths"}
    require(type(manifest) is dict and set(manifest) == required,
            "INVALID_MANIFEST", "Unexpected update manifest fields.")
    require(manifest["schema_version"] == SCHEMA and manifest["release_tag"] == release["tag"]
            and manifest["repository"] == OFFICIAL_REPOSITORY,
            "INVALID_MANIFEST", "Release identity does not match the official tag.")
    require(manifest["runtime_versions"] == FIXED_RUNTIME, "ENVIRONMENT_CHANGE_REFUSED",
            "This updater preserves the existing PyTorch and Genesis environment.")
    require(manifest["preserved_paths"] == ["student/", "output/", ".git/"], "INVALID_MANIFEST",
            "Student work, output and Git data must remain preserved.")
    require(manifest["managed_files"] == managed_names(files), "RELEASE_HASH_MISMATCH",
            "The supplied-file inventory does not match the official release.")
    require(manifest["rules"] == extract_rules(files), "RULE_SOURCE_MISMATCH",
            "The release's rules or protected sources do not match.")
    return manifest

def extract_rules(files):
    rules = {}
    for name, raw in files.items():
        if not name.startswith("leaderboard/rules/") or not name.endswith(".json"):
            continue
        rule = decode(raw)
        require(type(rule) is dict and rule.get("schema_version") == "ap-leaderboard-rule-v3"
                and rule.get("material") == "beads" and rule.get("test_only") is False,
                "RULE_VERSION_REFUSED", "Updates must contain production bead rules.")
        rid = rule.get("rule_id")
        require(type(rid) is str and re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", rid) and rid not in rules,
                "INVALID_RULE", "Rule identity is invalid or repeated.")
        expected = rule.get("rule_digest")
        require(expected == sha(canonical({k: v for k, v in rule.items() if k != "rule_digest"})),
                "RULE_DIGEST_MISMATCH", "A frozen rule was changed.")
        sources = rule.get("evaluation_sources")
        require(type(sources) is dict and sources, "INVALID_RULE", "Protected source inventory is missing.")
        for source, digest in sources.items():
            safe_name(source)
            require(source in files and sha(files[source]) == digest,
                    "RULE_SOURCE_MISMATCH", "Protected source mismatch: " + source)
        runtime = rule.get("runtime_versions", {})
        require(all(runtime.get(k) == v for k, v in FIXED_RUNTIME.items()),
                "ENVIRONMENT_CHANGE_REFUSED", "The rule changes the existing PyTorch or Genesis version.")
        rules[rid] = {"path": name, "digest": expected}
    require(len(rules) == 3, "RULE_SET_MISMATCH", "A release must contain all three bead complexity rules.")
    return rules

def verify_transition(old, old_release, new, new_release):
    old_files, new_files = old_release["files"], new_release["files"]
    for name in ENVIRONMENT_FILES:
        require(old_files.get(name) == new_files.get(name), "ENVIRONMENT_CHANGE_REFUSED",
                "Environment inventories changed. This updater does not install or modify packages.")
    for rid, info in new["rules"].items():
        require(rid not in old["rules"] or old["rules"][rid] == info, "FROZEN_RULE_CHANGED",
                "An existing rule ID changed. The instructor must release and qualify a new rule version.")
    old_runtimes = {canonical(decode(old_files[x["path"]])["runtime_versions"]) for x in old["rules"].values()}
    new_runtimes = {canonical(decode(new_files[x["path"]])["runtime_versions"]) for x in new["rules"].values()}
    require(old_runtimes == new_runtimes, "ENVIRONMENT_CHANGE_REFUSED", "Required runtime versions changed.")
    added_rules = set(new["rules"]) - set(old["rules"])
    if added_rules:
        try:
            request = urllib.request.Request(LEADERBOARD_URL + "/api/rules",
                headers={"ngrok-skip-browser-warning": "1"})
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            require(len(raw) <= 2 * 1024 * 1024, "RULE_REGISTRATION_REQUIRED", "Rule service response is too large.")
            registered = decode(raw)["rules"]
            active = {(r["rule_id"], r["rule_digest"]) for r in registered
                      if r.get("scoring_enabled") is True}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UpdateError("RULE_REGISTRATION_REQUIRED",
                              "New rule versions must be activated on the class server before installation.") from exc
        require(all((rid, new["rules"][rid]["digest"]) in active for rid in added_rules),
                "RULE_REGISTRATION_REQUIRED", "New rules are not active at the class server.")
    return bool(added_rules)

def current_release(root, source):
    raw = file_bytes(root, MANIFEST)
    if raw is None:
        release = source.read(LEGACY_TAG)
        manifest = validate_release(release, legacy=True)
    else:
        local = decode(raw)
        require(type(local) is dict and type(local.get("release_tag")) is str,
                "INVALID_MANIFEST", "Local release manifest is unreadable.")
        release = source.read(local["release_tag"])
        manifest = validate_release(release)
        require(raw == release["files"][MANIFEST], "LOCAL_SUPPLIED_FILE_CHANGED",
                "The local release manifest differs from the official version.")
    return release, manifest

def validate_local(root, files):
    conflicts = []
    for name, expected in files.items():
        data = file_bytes(root, name)
        if data is None or sha(data) != expected:
            conflicts.append(name)
    require(not conflicts, "LOCAL_SUPPLIED_FILE_CHANGED",
            "Preserved local changes. Restore the announced supplied version before updating: " +
            ", ".join(conflicts[:12]))
    return True

def plan_update(root, source, tag):
    target = source.read(tag)
    new = validate_release(target)
    previous, old = current_release(root, source)
    validate_local(root, old["managed_files"])
    new_rules = verify_transition(old, previous, new, target)
    old_hashes = dict(old["managed_files"])
    new_hashes = dict(new["managed_files"])
    if MANIFEST in previous["files"]:
        old_hashes[MANIFEST] = sha(previous["files"][MANIFEST])
    new_hashes[MANIFEST] = sha(target["files"][MANIFEST])
    changes = {}
    for name in sorted(set(old_hashes) | set(new_hashes)):
        before = old_hashes.get(name)
        after = new_hashes.get(name)
        if before == after:
            continue
        current = file_bytes(root, name)
        if before is None:
            require(current is None, "UNMANAGED_FILE_COLLISION",
                    "A new supplied file would overwrite an existing file: " + name)
        require((None if current is None else sha(current)) == before,
                "LOCAL_SUPPLIED_FILE_CHANGED", "Local file changed during verification: " + name)
        changes[name] = {"before": before, "after": after}
    return {"old": old, "new": new, "previous": previous, "target": target,
            "changes": changes, "new_rule_version": new_rules}

def backup_path(identifier):
    require(type(identifier) is str and re.fullmatch(ID_RE, identifier),
            "INVALID_BACKUP", "Use the exact backup ID printed by the updater.")
    return "output/project_updates/backups/" + identifier

def load_backup(root, identifier):
    prefix = backup_path(identifier)
    raw = file_bytes(root, prefix + "/record.json", internal=True)
    require(raw is not None, "BACKUP_MISSING", "That update backup was not found.")
    record = decode(raw)
    fields = {"schema_version", "backup_id", "state", "from_release", "to_release", "target_commit", "files"}
    require(type(record) is dict and set(record) == fields and record.get("backup_id") == identifier and
            record.get("schema_version") == "ap-update-backup-v1" and
            record.get("state") in ("prepared", "applied", "recovered", "rolled_back") and
            all(type(record.get(k)) is str and re.fullmatch(TAG_RE, record[k])
                for k in ("from_release", "to_release")) and
            type(record.get("target_commit")) is str and re.fullmatch(r"[0-9a-f]{40}", record["target_commit"]) and
            type(record.get("files")) is dict and 0 < len(record["files"]) <= MAX_FILES,
            "INVALID_BACKUP", "Invalid update backup.")
    for name, identities in record["files"].items():
        safe_name(name)
        require(type(identities) is dict and set(identities) == {"before", "after"},
                "INVALID_BACKUP", "Invalid backup file identity.")
        for label, digest in identities.items():
            require(digest is None or type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest),
                    "INVALID_BACKUP", "Invalid backup hash.")
            if digest is not None:
                data = file_bytes(root, prefix + "/" + label + "/" + name, internal=True)
                require(data is not None and sha(data) == digest,
                        "BACKUP_CORRUPT", "Backup file hash differs: " + name)
    return record

def restore_backup(root, identifier, *, recovery=False):
    prefix = backup_path(identifier)
    record = load_backup(root, identifier)
    # Validate the entire restoration before changing any supplied file.
    for name, identities in record["files"].items():
        data = file_bytes(root, name)
        actual = None if data is None else sha(data)
        allowed = {identities["before"], identities["after"]} if recovery else {identities["after"]}
        require(actual in allowed, "ROLLBACK_LOCAL_CHANGE",
                "A supplied file changed after update. Nothing was overwritten: " + name)
    journal = {"backup_id": identifier, "action": "rollback"}
    write_json(root, "output/project_updates/transaction.json", journal)
    restoration_order = sorted(record["files"], key=lambda name: (
        3 if name == "update_project.py" and record["files"][name]["before"] is None
        else 2 if name == "project_run.py"
        else 1 if name == "update_project.py" else 0, name))
    for name in restoration_order:
        identities = record["files"][name]
        current = file_bytes(root, name)
        actual = None if current is None else sha(current)
        require(actual in {identities["before"], identities["after"]}, "ROLLBACK_LOCAL_CHANGE",
                "A supplied file changed during rollback: " + name)
        if identities["before"] is None:
            delete_file(root, name)
        else:
            atomic_write(root, name, file_bytes(root, prefix + "/before/" + name, internal=True))
    for name, identities in record["files"].items():
        data = file_bytes(root, name)
        require((None if data is None else sha(data)) == identities["before"],
                "ROLLBACK_VERIFY_FAILED", "Rollback did not restore the exact prior file.")
    record["state"] = "recovered" if recovery else "rolled_back"
    write_json(root, prefix + "/record.json", record)
    delete_file(root, "output/project_updates/transaction.json", internal=True)
    return record

def recover_interrupted(root):
    raw = file_bytes(root, "output/project_updates/transaction.json", internal=True)
    if raw is None:
        return None
    journal = decode(raw)
    require(type(journal) is dict and set(journal) == {"backup_id", "action"} and
            journal["action"] in ("apply", "rollback"), "INVALID_JOURNAL", "An interrupted update journal is invalid.")
    record = restore_backup(root, journal["backup_id"], recovery=True)
    return record["backup_id"]

def apply_plan(root, plan):
    if not plan["changes"]:
        return {"status": "UPDATE_ALREADY_CURRENT", "release": plan["new"]["release_tag"]}
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    prefix = backup_path(identifier)
    record = {"schema_version": "ap-update-backup-v1", "backup_id": identifier, "state": "prepared",
              "from_release": plan["old"]["release_tag"], "to_release": plan["new"]["release_tag"],
              "target_commit": plan["target"]["commit"], "files": plan["changes"]}
    for name, identities in plan["changes"].items():
        current = file_bytes(root, name)
        require((None if current is None else sha(current)) == identities["before"],
                "LOCAL_SUPPLIED_FILE_CHANGED", "A file changed before backup: " + name)
        if current is not None:
            atomic_write(root, prefix + "/before/" + name, current, internal=True)
        if identities["after"] is not None:
            atomic_write(root, prefix + "/after/" + name, plan["target"]["files"][name], internal=True)
    write_json(root, prefix + "/record.json", record)
    load_backup(root, identifier)
    write_json(root, "output/project_updates/transaction.json", {"backup_id": identifier, "action": "apply"})
    try:
        installation_order = sorted(plan["changes"], key=lambda name: (
            0 if name == "update_project.py" else 1 if name == "project_run.py" else 2, name))
        for name in installation_order:
            identities = plan["changes"][name]
            current = file_bytes(root, name)
            require((None if current is None else sha(current)) == identities["before"],
                    "LOCAL_SUPPLIED_FILE_CHANGED", "A file changed during update: " + name)
            if identities["after"] is None:
                delete_file(root, name)
            else:
                atomic_write(root, name, file_bytes(root, prefix + "/after/" + name, internal=True))
        validate_local(root, plan["new"]["managed_files"])
        require(file_bytes(root, MANIFEST) == plan["target"]["files"][MANIFEST],
                "UPDATE_VERIFY_FAILED", "Release manifest did not install exactly.")
        record["state"] = "applied"
        write_json(root, prefix + "/record.json", record)
        delete_file(root, "output/project_updates/transaction.json", internal=True)
    except BaseException:
        # KeyboardInterrupt and ordinary I/O failures both retain recoverable state.
        restore_backup(root, identifier, recovery=True)
        raise
    return {"status": "UPDATE_APPLIED", "release": plan["new"]["release_tag"],
            "commit": plan["target"]["commit"], "backup_id": identifier,
            "changed_supplied_files": len(plan["changes"]), "student_files_preserved": True,
            "git_history_and_index_preserved": True, "environment_changed": False,
            "new_rule_version": plan["new_rule_version"],
            "next_action": "Check the existing environment, run the supplied tests, and export a fresh CPU plan before physical replay."}

def status(root):
    manifest = file_bytes(root, MANIFEST)
    local = decode(manifest) if manifest else None
    require(local is None or type(local) is dict and type(local.get("release_tag")) is str,
            "INVALID_MANIFEST", "The local release manifest is unreadable.")
    backups = checked_path(root, "output/project_updates/backups", internal=True)
    items = []
    if backups.exists():
        for path in sorted(backups.iterdir()):
            if re.fullmatch(ID_RE, path.name):
                entry = load_backup(root, path.name)
                items.append({k: entry[k] for k in ("backup_id", "state", "from_release", "to_release")})
    return {"status": "UPDATE_STATUS", "local_release": local.get("release_tag") if local else LEGACY_TAG,
            "verification": "Local state only. Use update check --release TAG for official-source verification.",
            "backups": items, "environment_changed": False}

def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("check", "apply"):
        command = sub.add_parser(name, help="Verify an official release" if name == "check" else "Back up and apply supplied files")
        command.add_argument("--release", required=True, help="Exact release tag announced by the instructor")
    sub.add_parser("status", help="Show local release and retained update backups")
    command = sub.add_parser("rollback", help="Restore supplied files from one retained backup")
    command.add_argument("--backup", required=True)
    args = parser.parse_args(argv)
    try:
        project = workspace(Path.cwd() if root is None else root)
        with update_lock(project), activity_lock(project, exclusive=True):
            recovered = recover_interrupted(project)
            if args.action == "status":
                answer = status(project)
            elif args.action == "rollback":
                record = restore_backup(project, args.backup)
                answer = {"status": "UPDATE_ROLLED_BACK", "backup_id": args.backup,
                          "release": record["from_release"], "student_files_preserved": True,
                          "environment_changed": False}
            else:
                plan = plan_update(project, ReleaseSource(project), args.release)
                if args.action == "check":
                    answer = {"status": "UPDATE_CHECK_PASS", "current_release": plan["old"]["release_tag"],
                              "release": plan["new"]["release_tag"], "commit": plan["target"]["commit"],
                              "changed_supplied_files": len(plan["changes"]),
                              "changed_paths": sorted(plan["changes"]),
                              "new_rule_version": plan["new_rule_version"], "files_applied": False,
                              "environment_changed": False}
                else:
                    answer = apply_plan(project, plan)
            if recovered:
                answer["interrupted_update_recovered"] = recovered
            print(json.dumps(answer, ensure_ascii=True))
            return 0
    except (UpdateError, OSError, ValueError) as exc:
        print(json.dumps({"status": "UPDATE_REFUSED", "code": getattr(exc, "code", "FILESYSTEM_ERROR"),
                          "message": str(exc), "environment_changed": False}, ensure_ascii=True),
              file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())

