#!/usr/bin/env python3
"""Install one new hash-pinned synthetic presence fixture for an owned GUI run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess

WORKTREE = Path("/Volumes/T9/Code/AxioSozo-browser-workstation")
VOLUME = Path("/Volumes/AxioSozoBuild")
BUILD = VOLUME / "workstation"
T9 = Path("/Volumes/T9")  # its APFS container holds the AxioSozoBuild volume
BASE = BUILD / "gui-fixtures"
PROFILE_BASE = BUILD / "runtime/e626697ad91fe95c"
PYTHON = VOLUME / "toolchains/zen/python/bin/python3.11"
SOURCE = WORKTREE / "tools/axiosozo-key-fixture/key_fixture.py"
SHA = "82e11f794ab48cd0b29a28e65a560e876dca88406c3fc8d96fc851c300365d71"
MAX_SOURCE = 65536
NAME = "key-helper-" + SHA + ".py"


def require(value):
    if not value:
        raise RuntimeError("KEY_FIXTURE_INSTALL_REFUSED")


def layout(run_id):
    require(isinstance(run_id, str) and re.fullmatch(r"[a-f0-9]{32}", run_id))
    return {"run_id": run_id, "root": BASE / ("keys-" + run_id),
            "profile": PROFILE_BASE / ("plan4-keys-" + run_id) / "gecko"}


def disk_info(path):
    result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(path)], capture_output=True,
                            timeout=5, check=True, env={"LANG": "C", "LC_ALL": "C"})
    return plistlib.loads(result.stdout)


def mounted_volume():
    # Since October 2026 the build volume is an APFS volume of T9's own
    # container (formerly inside /Volumes/T9/Dev.sparsebundle).
    require(VOLUME.is_mount() and VOLUME.resolve() == VOLUME and not VOLUME.is_symlink() and T9.is_mount())
    volume, t9 = disk_info(VOLUME), disk_info(T9)
    require(volume.get("MountPoint") == str(VOLUME) and volume.get("VolumeName") == "AxioSozoBuild"
            and str(volume.get("FilesystemType", "")).lower() == "apfs")
    require(t9.get("MountPoint") == str(T9) and str(t9.get("FilesystemType", "")).lower() == "apfs"
            and isinstance(t9.get("APFSContainerReference"), str) and t9["APFSContainerReference"] != ""
            and volume.get("APFSContainerReference") == t9["APFSContainerReference"])


def directory_fd(path, private=False):
    require(path.is_absolute() and str(path) == os.path.normpath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            require(info.st_uid in (0, os.getuid()))
            require(not stat.S_IMODE(info.st_mode) & 0o022 or bool(info.st_mode & 0o1000))
        if private:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
        result = fd
        fd = -1
        return result
    finally:
        if fd != -1:
            os.close(fd)


def verify_interpreter():
    fd = directory_fd(PYTHON.parent)
    try:
        child = os.open(PYTHON.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(child)
            require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                    and not info.st_mode & 0o022 and bool(info.st_mode & 0o111))
            return {"device": str(info.st_dev), "inode": str(info.st_ino)}
        finally:
            os.close(child)
    finally:
        os.close(fd)


def source_bytes():
    # Source is public invented fixture code on exFAT: descriptor containment and
    # pinned content hash provide admission; private APFS modes do not apply here.
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in SOURCE.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(SOURCE.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_uid == os.getuid() and 0 < before.st_size <= MAX_SOURCE)
        data = bytearray()
        while len(data) <= MAX_SOURCE:
            chunk = os.read(fd, min(8192, MAX_SOURCE + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        require(len(data) == before.st_size and hashlib.sha256(data).hexdigest() == SHA)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        return bytes(data)
    finally:
        os.close(fd)


def check_root(root):
    require(root.parent == BASE and re.fullmatch(r"keys-[a-f0-9]{32}", root.name))
    fd = directory_fd(root, private=True)
    try:
        child = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(child)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o400 and 0 < info.st_size <= MAX_SOURCE)
            raw = os.read(child, MAX_SOURCE + 1)
            after = os.fstat(child)
            require(len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == SHA)
            require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        finally:
            os.close(child)
        return {"installed": True, "helper_sha256": SHA, "root": str(root),
                "helper": str(root / NAME), "interpreter": str(PYTHON)}
    finally:
        os.close(fd)


def install(run_id):
    require(Path.cwd().resolve() == WORKTREE and os.environ.get("AXIOSOZO_BUILD_ROOT") == str(BUILD))
    spec = layout(run_id)
    mounted_volume()
    interpreter = verify_interpreter()
    data = source_bytes()
    parent = directory_fd(BASE, private=True)
    child = None
    try:
        # No exist_ok, overwrites, repair, migration or cleanup of previous runs.
        os.mkdir(spec["root"].name, 0o700, dir_fd=parent)
        os.fsync(parent)
        child = os.open(spec["root"].name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        require(os.fstat(child).st_uid == os.getuid() and stat.S_IMODE(os.fstat(child).st_mode) == 0o700)
        destination = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=child)
        try:
            info = os.fstat(destination)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid())
            view = memoryview(data)
            while view:
                written = os.write(destination, view)
                require(written > 0)
                view = view[written:]
            os.fchmod(destination, 0o400)
            os.fsync(destination)
        finally:
            os.close(destination)
        os.fsync(child)
    finally:
        if child is not None:
            os.close(child)
        os.close(parent)
    result = check_root(spec["root"])
    result.update({"run_id": run_id, "expected_profile": str(spec["profile"]),
        "profile_created": False, "interpreter_identity": interpreter,
        "keychain": "NOT_USED", "providers": "NOT_AUTHORIZED"})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "check"))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    run_id = args.run_id if args.run_id is not None else secrets.token_hex(16)
    require(Path.cwd().resolve() == WORKTREE and os.environ.get("AXIOSOZO_BUILD_ROOT") == str(BUILD))
    if args.operation == "install":
        return install(run_id)
    require(args.run_id is not None)
    mounted_volume()
    interpreter = verify_interpreter()
    result = check_root(layout(run_id)["root"])
    result.update({"interpreter_identity": interpreter, "native_gecko": "NOT_RUN"})
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        print(json.dumps({"status": "KEY_FIXTURE_INSTALL_REFUSED"}))
        raise SystemExit(1)
