#!/usr/bin/env python3
"""Install/check one new, fixed-path, offline Understand fixture; no provider run."""
from contextlib import ExitStack
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess

WORKTREE = Path("/Volumes/T9/Code/AxioSozo-browser-workstation")
SOURCE = WORKTREE
VOLUME = Path("/Volumes/AxioSozoBuild")
BUILD = VOLUME / "workstation"
T9 = Path("/Volumes/T9")  # its APFS container holds the AxioSozoBuild volume
BASE = BUILD / "gui-fixtures"
PROFILE_BASE = BUILD / "runtime/e626697ad91fe95c"
PYTHON = VOLUME / "toolchains/zen/python/bin/python3.11"
NODE = VOLUME / "toolchains/zen/node/bin/node"
BINARY_PINS = (
    (PYTHON, "67909d536cd240fcf4b1125a9ff691ad473ad23156fa4f45611da52233959071", 32 * 1024 * 1024),
    (NODE, "2cc28a1880d0774616959f2210ee739bffa6ac695c887e8988a496a21271f6c0", 128 * 1024 * 1024),
)
# Closed mapping: no caller-selected paths, manifests, commands or source bytes.
INPUTS = (
    ("packages/provider-host/cli.mjs", "tools/axiosozo-understand/fixture-host.mjs", "b9997e1ee509341a3e9f96fa6221d90e369d457d29e57a5838989ad58a9ac65c", 65536),
    ("packages/provider-host/fixtures/understand/common.mjs", "packages/provider-host/fixtures/understand/common.mjs", "a7970176dff6e3f6305331e5a25ac4128d9fd10ec97f4ff326102c3463a37936", 65536),
    ("packages/provider-host/fixtures/understand/fake-cli.mjs", "tools/axiosozo-understand/fake-cli.mjs", "bb9bc0cf0fcaab18f1fef054a992cf75e267f600c8eee3e486bf64e557160eac", 65536),
    ("packages/provider-host/src/discovery.mjs", "packages/provider-host/src/discovery.mjs", "e53c1f8daa20ea3d9162f8b418fe196bd2b3175da3b636e36e674b74b8af4c9a", 65536),
    ("packages/provider-host/src/understand.mjs", "packages/provider-host/src/understand.mjs", "6f070833e58709c9a64345a19791c7f61f17baec2db7e58a9538c3eb92f69ef7", 65536),
    ("packages/provider-host/src/validation.mjs", "packages/provider-host/src/validation.mjs", "f9bc96f48c21c473061c8ab0fea425879a7d7279554542ccaa81134470f81365", 65536),
    ("packages/provider-host/vendor/t3/version.ts", "packages/provider-host/vendor/t3/version.ts", "d335a12481cab591cf84abc98b1becdec6229674e9e59fd0d8067cf204e8f92f", 65536),
    ("policy.json", "tools/axiosozo-understand/policy.json", "9d18d84c61457a9e97fd5548a32542dc0427bd63af817bdbf4114f3e013d0c32", 32768),
    ("understand_fixture.py", "tools/axiosozo-understand/understand_fixture.py", "3f9d4ac05a87f7c9aa694eaa57bbbca648338e5be36e136d6d03c87ca91017d8", 65536),
)
DIRECTORIES = (
    "home", "projects", "projects/harbor", "projects/inkline", "packages", "packages/provider-host",
    "packages/provider-host/src", "packages/provider-host/vendor", "packages/provider-host/vendor/t3",
    "packages/provider-host/fixtures", "packages/provider-host/fixtures/understand",
)


def require(value):
    if not value:
        raise RuntimeError("UNDERSTAND_FIXTURE_INSTALL_REFUSED")


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def directory_identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


class Directory:
    """Retain every ancestor and its named binding throughout the operation."""
    def __init__(self, path, *, private=False, public_source=False):
        self.path = Path(path)
        self.fds = []
        self.bindings = []
        self.private = private
        self.public_source = public_source
        require(self.path.is_absolute() and str(self.path) == os.path.normpath(str(self.path))
                and all(part not in ("", ".", "..") for part in str(self.path).split("/")[1:]))
        try:
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.fds.append(parent)
            for name in self.path.parts[1:]:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self.fds.append(child)
                self.bindings.append((parent, name, child))
                parent = child
            self.fd = parent
            self.snapshots = [directory_identity(os.fstat(fd)) for fd in self.fds]
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        for index, fd in enumerate(self.fds):
            info = os.fstat(fd)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid())
                    and directory_identity(info) == self.snapshots[index])
            # Public pinned source resides on exFAT, whose synthetic POSIX modes
            # are not a confidentiality boundary. Its full bytes are pinned.
            if not self.public_source:
                require(not stat.S_IMODE(info.st_mode) & 0o022 or bool(info.st_mode & 0o1000))
        for parent, name, fd in self.bindings:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISDIR(named.st_mode)
                    and directory_identity(named) == directory_identity(os.fstat(fd)))
        if self.private:
            info = os.fstat(self.fd)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PinnedFile:
    """Bounded hash plus a retained descriptor and stable named identity."""
    def __init__(self, path, digest, maximum, *, source=False, executable=False, keep_bytes=False):
        self.path = Path(path)
        self.fd = None
        self.directory = Directory(self.path.parent, public_source=source)
        try:
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=self.directory.fd)
            before = os.fstat(self.fd)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    and before.st_uid in ((0, os.getuid()) if executable else (os.getuid(),))
                    and 0 < before.st_size <= maximum)
            if executable:
                require(stat.S_IMODE(before.st_mode) == 0o755)
            elif not source:
                require(stat.S_IMODE(before.st_mode) == 0o400)
            self.snapshot = file_identity(before)
            hasher = hashlib.sha256()
            count = 0
            data = bytearray() if keep_bytes else None
            while count <= maximum:
                chunk = os.read(self.fd, min(65536, maximum + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                hasher.update(chunk)
                if data is not None:
                    data.extend(chunk)
            require(count == before.st_size and hasher.hexdigest() == digest)
            self.bytes = None if data is None else bytes(data)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        self.directory.check()
        named = os.stat(self.path.name, dir_fd=self.directory.fd, follow_symlinks=False)
        require(self.snapshot == file_identity(os.fstat(self.fd)) == file_identity(named))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.directory.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def layout(run_id):
    require(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id) is not None)
    return BASE / ("understand-" + run_id), PROFILE_BASE / ("plan4-understand-" + run_id) / "gecko"


def flags(root):
    require(Path.cwd() == WORKTREE and os.environ.get("AXIOSOZO_BUILD_ROOT") == str(BUILD)
            and os.environ.get("AXIOSOZO_SYNTHETIC_TEST") == "1"
            and os.environ.get("AXIOSOZO_UNDERSTAND_GUI_FIXTURE") == "1"
            and os.environ.get("AXIOSOZO_UNDERSTAND_GUI_FIXTURE_ROOT") == str(root))


def os_query(kind):
    # Protected OS metadata tools only. This never invokes fixture/provider code.
    commands = {
        "disk": ["/usr/sbin/diskutil", "info", "-plist", str(VOLUME)],
        "t9": ["/usr/sbin/diskutil", "info", "-plist", str(T9)],
    }
    require(kind in commands)
    command = commands[kind]
    with Directory(Path(command[0]).parent) as directory:
        fd = os.open(Path(command[0]).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                    and not stat.S_IMODE(before.st_mode) & 0o022 and bool(before.st_mode & 0o111))
            snapshot = file_identity(before)
            directory.check()
            result = subprocess.run(command, capture_output=True, check=True, timeout=5,
                                    env={"LANG": "C", "LC_ALL": "C"}, cwd="/", close_fds=True)
            require(len(result.stdout) <= 2 * 1024 * 1024 and len(result.stderr) <= 65536)
            named = os.stat(Path(command[0]).name, dir_fd=directory.fd, follow_symlinks=False)
            require(snapshot == file_identity(os.fstat(fd)) == file_identity(named))
            directory.check()
            return plistlib.loads(result.stdout)
        finally:
            os.close(fd)


def mounted_volume():
    # Since October 2026 the build volume is an APFS volume of T9's own
    # container (formerly inside /Volumes/T9/Dev.sparsebundle).
    with Directory(VOLUME):
        require(VOLUME.is_mount() and T9.is_mount())
        disk = os_query("disk")
        require(isinstance(disk, dict) and disk.get("MountPoint") == str(VOLUME)
                and disk.get("VolumeName") == VOLUME.name
                and str(disk.get("FilesystemType", "")).lower() == "apfs")
        t9 = os_query("t9")
        require(isinstance(t9, dict) and t9.get("MountPoint") == str(T9)
                and str(t9.get("FilesystemType", "")).lower() == "apfs"
                and isinstance(t9.get("APFSContainerReference"), str) and t9["APFSContainerReference"] != ""
                and disk.get("APFSContainerReference") == t9["APFSContainerReference"])


def admission(run_id, stack):
    root, profile = layout(run_id)
    flags(root)
    mounted_volume()
    # Profile contents are never enumerated or opened. The GUI owner creates it.
    directories = [stack.enter_context(Directory(full, private=True))
                   for full in (BASE, profile)]
    binaries = [stack.enter_context(PinnedFile(full, digest, maximum, executable=True))
                for full, digest, maximum in BINARY_PINS]
    def guard():
        flags(root)
        for directory in directories:
            directory.check()
        for binary in binaries:
            binary.check()
    guard()
    return root, profile, guard


def write_input(parent, name, raw):
    require(Path(name).name == name and name not in ("", ".", ".."))
    parent.check()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o400, dir_fd=parent.fd)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            require(count > 0)
            view = view[count:]
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        named = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        require(file_identity(named) == file_identity(os.fstat(fd))
                and stat.S_IMODE(named.st_mode) == 0o400 and named.st_size == len(raw))
        parent.check()
    finally:
        os.close(fd)
    os.fsync(parent.fd)
    parent.check()


def check_inputs(root, stack):
    directories = [stack.enter_context(Directory(full, private=True))
                   for full in [root, *(root / relative for relative in DIRECTORIES)]]
    files = [stack.enter_context(PinnedFile(root / relative, digest, maximum))
             for relative, _source, digest, maximum in INPUTS]
    def guard():
        for directory in directories:
            directory.check()
        for pinned in files:
            pinned.check()
    guard()
    return guard


def report(run_id, root, profile):
    return {"version": 1, "status": "ok", "fixture_only": True, "run_id": run_id,
            "root": str(root), "expected_profile": str(profile), "profile_created": False,
            "profile_contents_read": False, "input_count": len(INPUTS),
            "helper_sha256": next(digest for relative, _source, digest, _maximum in INPUTS
                                  if relative == "understand_fixture.py"),
            "python": str(PYTHON), "node": str(NODE), "providers": "NOT_AUTHORIZED", "keychain": "NOT_USED"}


def install(run_id):
    with ExitStack() as stack:
        root, profile, guard = admission(run_id, stack)
        sources = [stack.enter_context(PinnedFile(SOURCE / source, digest, maximum, source=True, keep_bytes=True))
                   for _relative, source, digest, maximum in INPUTS]
        def source_guard():
            for source in sources:
                source.check()
        created = {}
        def created_guard():
            for directory in created.values():
                directory.check()
        guard()
        source_guard()
        with Directory(BASE, private=True) as parent:
            # O_EXCL-style directory creation: never overwrite, reuse or clean.
            os.mkdir(root.name, 0o700, dir_fd=parent.fd)
            os.fsync(parent.fd)
            snapshot = directory_identity(os.stat(root.name, dir_fd=parent.fd, follow_symlinks=False))
            created[""] = stack.enter_context(Directory(root, private=True))
            require(snapshot == directory_identity(os.fstat(created[""].fd)))
            parent.check()
        for relative in DIRECTORIES:
            guard()
            source_guard()
            created_guard()
            parent_key = str(Path(relative).parent)
            parent = created["" if parent_key == "." else parent_key]
            name = Path(relative).name
            os.mkdir(name, 0o700, dir_fd=parent.fd)
            os.fsync(parent.fd)
            snapshot = directory_identity(os.stat(name, dir_fd=parent.fd, follow_symlinks=False))
            created[relative] = stack.enter_context(Directory(root / relative, private=True))
            require(snapshot == directory_identity(os.fstat(created[relative].fd)))
            created_guard()
        for (relative, _source, _digest, _maximum), source in zip(INPUTS, sources):
            guard()
            source_guard()
            created_guard()
            parent_key = str(Path(relative).parent)
            parent = created["" if parent_key == "." else parent_key]
            write_input(parent, Path(relative).name, source.bytes)
            created_guard()
        installed_guard = check_inputs(root, stack)
        guard()
        source_guard()
        created_guard()
        installed_guard()
        return report(run_id, root, profile)


def check(run_id):
    with ExitStack() as stack:
        root, profile, guard = admission(run_id, stack)
        installed_guard = check_inputs(root, stack)
        guard()
        installed_guard()
        return report(run_id, root, profile)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "check"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    return install(args.run_id) if args.operation == "install" else check(args.run_id)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, TypeError):
        print(json.dumps({"status": "UNDERSTAND_FIXTURE_INSTALL_REFUSED"}))
        raise SystemExit(1)
