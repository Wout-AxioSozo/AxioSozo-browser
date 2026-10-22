#!/usr/bin/env python3
"""Fixed offline Understand fixture launcher. No arbitrary code, path or CLI."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

BUILD = Path("/Volumes/AxioSozoBuild/workstation")
BASE = BUILD / "gui-fixtures"
PROFILE_BASE = BUILD / "runtime/e626697ad91fe95c"
PYTHON = Path("/Volumes/AxioSozoBuild/toolchains/zen/python/bin/python3.11")
NODE = Path("/Volumes/AxioSozoBuild/toolchains/zen/node/bin/node")
POLICY_SHA = "9d18d84c61457a9e97fd5548a32542dc0427bd63af817bdbf4114f3e013d0c32"
NODE_SHA = "2cc28a1880d0774616959f2210ee739bffa6ac695c887e8988a496a21271f6c0"
PYTHON_SHA = "67909d536cd240fcf4b1125a9ff691ad473ad23156fa4f45611da52233959071"
FILE_PINS = {'packages/provider-host/cli.mjs': 'b9997e1ee509341a3e9f96fa6221d90e369d457d29e57a5838989ad58a9ac65c', 'packages/provider-host/fixtures/understand/common.mjs': 'a7970176dff6e3f6305331e5a25ac4128d9fd10ec97f4ff326102c3463a37936', 'packages/provider-host/fixtures/understand/fake-cli.mjs': 'bb9bc0cf0fcaab18f1fef054a992cf75e267f600c8eee3e486bf64e557160eac', 'packages/provider-host/src/discovery.mjs': 'e53c1f8daa20ea3d9162f8b418fe196bd2b3175da3b636e36e674b74b8af4c9a', 'packages/provider-host/src/understand.mjs': '6f070833e58709c9a64345a19791c7f61f17baec2db7e58a9538c3eb92f69ef7', 'packages/provider-host/src/validation.mjs': 'f9bc96f48c21c473061c8ab0fea425879a7d7279554542ccaa81134470f81365', 'packages/provider-host/vendor/t3/version.ts': 'd335a12481cab591cf84abc98b1becdec6229674e9e59fd0d8067cf204e8f92f'}


def refuse(value):
    if not value:
        raise RuntimeError("UNDERSTAND_FIXTURE_UNAVAILABLE")


def identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Directory:
    def __init__(self, path, private=False):
        self.path = Path(path)
        self.fds = []
        self.bindings = []
        self.private = private
        refuse(self.path.is_absolute() and str(self.path) == os.path.normpath(str(self.path))
               and all(part not in ("", ".", "..") for part in str(self.path).split("/")[1:]))
        try:
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.fds.append(parent)
            for part in self.path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self.fds.append(child)
                self.bindings.append((parent, part, child))
                parent = child
            self.fd = parent
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        for fd in self.fds:
            info = os.fstat(fd)
            refuse(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid())
                   and (not stat.S_IMODE(info.st_mode) & 0o022 or bool(info.st_mode & 0o1000)))
        for parent, name, fd in self.bindings:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(fd)
            refuse(stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino))
        if self.private:
            info = os.fstat(self.fd)
            refuse(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_pinned(path, digest, max_bytes=65536, mode=0o400, executable=False):
    with Directory(Path(path).parent) as parent:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent.fd)
        try:
            before = os.fstat(fd)
            refuse(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                   and before.st_uid in ((0, os.getuid()) if executable else (os.getuid(),))
                   and 0 < before.st_size <= max_bytes and not stat.S_IMODE(before.st_mode) & 0o022)
            if mode is not None:
                refuse(stat.S_IMODE(before.st_mode) == mode)
            if executable:
                refuse(bool(before.st_mode & 0o111))
            value = bytearray()
            while len(value) <= max_bytes:
                chunk = os.read(fd, min(65536, max_bytes + 1 - len(value)))
                if not chunk:
                    break
                value.extend(chunk)
            after = os.fstat(fd)
            named = os.stat(Path(path).name, dir_fd=parent.fd, follow_symlinks=False)
            refuse(len(value) == before.st_size and identity(before) == identity(after)
                   and identity(before) == identity(named) and hashlib.sha256(value).hexdigest() == digest)
            parent.check()
            return bytes(value), identity(before)
        finally:
            os.close(fd)


def paths(root, profile):
    root = Path(root)
    match = re.fullmatch(r"understand-([0-9a-f]{32})", root.name)
    refuse(match is not None and root.parent == BASE and str(root) == os.path.normpath(str(root)))
    expected = PROFILE_BASE / ("plan4-understand-" + match.group(1)) / "gecko"
    refuse(Path(profile) == expected and str(profile) == str(expected))
    if sys.argv[1] == "cli":
        # UnderstandRunner deliberately gives its child only PATH/HOME/LANG/TERM.
        # This fixed fake constructor carries root/profile via its immutable
        # prefix, not caller environment or discovery. Never widen Runner's env.
        refuse(os.environ.get("HOME") == str(root / "home")
               and os.environ.get("PATH") == "/usr/bin:/bin" and os.environ.get("TERM") == "dumb"
               and Path.cwd() in (root / "projects/harbor", root / "projects/inkline"))
    else:
        refuse(os.environ.get("AXIOSOZO_SYNTHETIC_TEST") == "1"
               and os.environ.get("AXIOSOZO_UNDERSTAND_GUI_FIXTURE") == "1"
               and os.environ.get("AXIOSOZO_UNDERSTAND_GUI_FIXTURE_ROOT") == str(root))
    refuse(Path(__file__).absolute() == root / "understand_fixture.py"
           and Path(sys.executable).resolve() == PYTHON)
    return root, expected


def admission(root, profile, stack):
    directories = [stack.enter_context(Directory(full, private=True)) for full in
                   [BASE, root, profile, root / "home", root / "projects", root / "projects/harbor", root / "projects/inkline"]]
    _, python_identity = read_pinned(PYTHON, PYTHON_SHA, 32 * 1024 * 1024, mode=None, executable=True)
    _, node_identity = read_pinned(NODE, NODE_SHA, 128 * 1024 * 1024, mode=None, executable=True)
    raw, policy_identity = read_pinned(root / "policy.json", POLICY_SHA, 32768)
    policy = json.loads(raw)
    refuse(policy.get("version") == 1 and policy.get("fixture_only") is True
           and policy.get("project_names") == ["harbor", "inkline"]
           and policy.get("files") == FILE_PINS and policy.get("cli_timeout_ms") == 5000
           and policy.get("host_timeout_ms") == 60000 and policy.get("node_sha256") == NODE_SHA
           and policy.get("python_sha256") == PYTHON_SHA)
    records = {str(PYTHON): python_identity, str(NODE): node_identity, str(root / "policy.json"): policy_identity}
    for relative, digest in FILE_PINS.items():
        _, snapshot = read_pinned(root / relative, digest)
        records[str(root / relative)] = snapshot
    def guard():
        for directory in directories:
            directory.check()
        for full, snapshot in records.items():
            refuse(identity(os.lstat(full)) == snapshot)
    guard()
    return guard


def receipt(root, child, mode):
    with Directory(root, private=True) as directory:
        fd = os.open("process-receipts.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory.fd)
        try:
            info = os.fstat(fd)
            refuse(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                   and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 65536)
            data = (json.dumps({"launcher_pid": os.getpid(), "child_pid": child.pid, "mode": mode}, sort_keys=True) + "\n").encode()
            refuse(len(data) <= 256 and os.write(fd, data) == len(data))
        finally:
            os.close(fd)


def launch(mode, root, profile, extra):
    child = None
    # The CLI wrapper is a detached group leader; the host wrapper belongs to
    # its browser/test owner. Capture that owner before admission and refuse/reap
    # when reparented after abrupt owner death. No repo process discovery or
    # process-tree enumeration is needed for this closed fixture.
    owner_pid = os.getppid()
    refuse(owner_pid > 1)
    def owner_alive():
        refuse(os.getppid() == owner_pid)
    with ExitStack() as stack:
        guard = admission(root, profile, stack)
        if mode == "host":
            refuse(not extra)
            command = [str(NODE), str(root / "packages/provider-host/cli.mjs"), str(root), str(profile)]
            timeout = 60
            environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "AXIOSOZO_SYNTHETIC_TEST": "1",
                           "AXIOSOZO_UNDERSTAND_GUI_FIXTURE": "1", "AXIOSOZO_UNDERSTAND_GUI_FIXTURE_ROOT": str(root)}
        else:
            refuse(mode == "cli" and len(extra) >= 1 and extra[0] in ("claude-code", "codex"))
            refuse(len(extra) <= 80 and all(len(value) <= 16384 and "\x00" not in value for value in extra))
            command = [str(NODE), str(root / "packages/provider-host/fixtures/understand/fake-cli.mjs"), *extra]
            timeout = 5
            environment = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"), "LANG": "C", "TERM": "dumb"}
        stopped = False
        crash_requested = False
        def stop(*_):
            nonlocal stopped
            # Signal handlers never call Popen: waitpid can be reentrant here.
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        if mode == "host":
            # Fixed test diagnostic: the owning Popen object kills its unreaped
            # child. Never signal a PID read from a receipt or enumerate processes.
            def crash_own_host(*_):
                nonlocal crash_requested
                crash_requested = True
            signal.signal(signal.SIGUSR1, crash_own_host)
        guard()
        owner_alive()
        refuse(not stopped)
        child = subprocess.Popen(command, shell=False, cwd="/" if mode == "host" else os.getcwd(), env=environment,
                                 stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL,
                                 start_new_session=mode == "host", close_fds=True)
        deadline = time.monotonic() + timeout
        try:
            owner_alive()
            refuse(not stopped)
            receipt(root, child, mode)
            while child.poll() is None:
                guard()
                owner_alive()
                if crash_requested:
                    crash_requested = False
                    child.send_signal(signal.SIGKILL)
                if stopped or time.monotonic() >= deadline:
                    raise RuntimeError("UNDERSTAND_FIXTURE_UNAVAILABLE")
                try:
                    child.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            return child.returncode
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if mode == "host":
                        os.killpg(child.pid, signal.SIGKILL)
                    else:
                        child.kill()
                    child.wait(timeout=0.5)


def main():
    refuse(len(sys.argv) >= 4 and sys.argv[1] in ("check", "host", "cli"))
    mode = sys.argv[1]
    root, profile = paths(sys.argv[2], sys.argv[3])
    if mode == "check":
        refuse(len(sys.argv) == 4)
        with ExitStack() as stack:
            admission(root, profile, stack)()
        print(json.dumps({"version": 1, "fixture_only": True, "policy_sha256": POLICY_SHA, "input_count": len(FILE_PINS)}))
        return 0
    return launch(mode, root, profile, sys.argv[4:])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        print("UNDERSTAND_FIXTURE_UNAVAILABLE", file=sys.stderr)
        raise SystemExit(78)
