#!/usr/bin/env python3
"""Install/check one reviewed, checksum-addressed bridge bundle; never execute it.

Intended destination: scripts/agent_bridge_install.py. CLI accepts setup/check
only. All source and Node paths are fixed; no PATH, clients, settings or MCP.
"""
from contextlib import ExitStack
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import storage

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = Path('packages/agent-bridge')
SOURCE_PINS = (
    ('package.json', '38aecdf39990e070506ac1a2064a9f2897f6df1dc5a4df2eff4ba469bb608506'),
    ('bin/axiosozo-agent-bridge.mjs', '3efd57e4e38b0b23b3acf9a2e01a55b860e181e464b836ec599c9368857a4d05'),
    ('src/server.mjs', 'c96b7336ff6a8fc9552f2335b34578faafb5b16fb08668367a0c7be7b9c2aca0'),
    ('src/channel.mjs', 'ebdf3af693d5c0f2e8fc6391fab497521a1d51b594d236fcb9e1df436b0fe7ea'),
    ('src/jsonl.mjs', 'c08f37ba38b9c389b199005266e5eca28fb074cd8d70286c87317563eefe3125'),
    ('src/tools.mjs', 'ba8f64908ff3f80d3f3e0b60ea26ac77fbbd65d9ede43b59f0a51491fbdb5e0c'),
)
NODE_SOURCE = Path('/Volumes/AxioSozoBuild/toolchains/zen/node/bin/node')
NODE_SHA256 = '2cc28a1880d0774616959f2210ee739bffa6ac695c887e8988a496a21271f6c0'
BUNDLE_SHA256 = '7e3bc2f4aa1c034dacd1bb847461b01c2ef535ff1da4c8f2175e31261838bfda'
MAX_SOURCE_BYTES = 64 * 1024
MAX_NODE_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


def require(value, code):
    if not value:
        raise RuntimeError(code)


def identity(info):
    return (info.st_dev, info.st_ino)


def version(info):
    return (identity(info), info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def absolute(path):
    value = str(path)
    require(value.startswith('/') and value != '/' and not value.endswith('/')
            and '//' not in value and '\\' not in value
            and not any(p in {'.', '..'} for p in value.split('/'))
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
            'AGENT_BRIDGE_PATH_REFUSED')
    return value


def manifest_bytes(node_sha256=NODE_SHA256):
    require(isinstance(node_sha256, str) and re.fullmatch('[0-9a-f]{64}', node_sha256),
            'AGENT_BRIDGE_PIN_INVALID')
    pins = (*SOURCE_PINS, ('node', node_sha256))
    return ('axiosozo-agent-bridge-bundle-v1\n' +
            ''.join(f'{digest}  {name}\n' for name, digest in pins)).encode('utf-8')


def capabilities(io):
    require(io.name == 'posix'
            and all(isinstance(getattr(io, key, None), int) and getattr(io, key)
                    for key in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_CLOEXEC', 'O_NONBLOCK',
                                'O_CREAT', 'O_EXCL', 'O_WRONLY'))
            and all(fn in io.supports_dir_fd for fn in (io.open, io.stat, io.mkdir))
            and io.stat in io.supports_follow_symlinks and io.listdir in io.supports_fd
            and all(callable(getattr(io, key, None))
                    for key in ('fstat', 'read', 'write', 'fchmod', 'fsync', 'lseek', 'close')),
            'AGENT_BRIDGE_CONTAINMENT_UNAVAILABLE')


def checked_directory(info, io, *, private=False, owned=False):
    require(stat.S_ISDIR(info.st_mode), 'AGENT_BRIDGE_DIRECTORY_REFUSED')
    if owned or private:
        require(info.st_uid == io.getuid() and not info.st_mode & 0o022
                and not info.st_mode & 0o7000, 'AGENT_BRIDGE_DIRECTORY_REFUSED')
    if private:
        require(stat.S_IMODE(info.st_mode) == 0o700, 'AGENT_BRIDGE_DIRECTORY_REFUSED')


def checked_file(info, io, mode=None):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == io.getuid()
            and info.st_nlink == 1 and not info.st_mode & 0o022
            and not info.st_mode & 0o7000, 'AGENT_BRIDGE_FILE_REFUSED')
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, 'AGENT_BRIDGE_FILE_REFUSED')


class RetainedTree:
    """Keep all ancestor descriptors and recheck every named edge before success."""
    def __init__(self, io, stack):
        self.io, self.stack = io, stack
        self.edges, self.files = [], []
        self.flags = io.O_RDONLY | io.O_DIRECTORY | io.O_NOFOLLOW | io.O_CLOEXEC
        self.root = io.open('/', self.flags)
        stack.callback(io.close, self.root)
        self.root_id = identity(io.fstat(self.root))

    def child(self, parent, leaf, *, private=False, owned=False):
        io = self.io
        before = io.stat(leaf, dir_fd=parent, follow_symlinks=False)
        checked_directory(before, io, private=private, owned=owned)
        fd = io.open(leaf, self.flags, dir_fd=parent)
        self.stack.callback(io.close, fd)
        after = io.fstat(fd)
        checked_directory(after, io, private=private, owned=owned)
        require(identity(before) == identity(after), 'AGENT_BRIDGE_IDENTITY_CHANGED')
        self.edges.append((parent, leaf, fd, identity(after), private, owned))
        return fd

    def directory(self, path, *, owned=False):
        parts = absolute(path).split('/')[1:]
        current = self.root
        for index, leaf in enumerate(parts):
            current = self.child(current, leaf, owned=owned and index == len(parts) - 1)
        return current

    def recheck(self):
        io = self.io
        require(identity(io.fstat(self.root)) == self.root_id, 'AGENT_BRIDGE_IDENTITY_CHANGED')
        for parent, leaf, fd, found_id, private, owned in self.edges:
            opened = io.fstat(fd)
            named = io.stat(leaf, dir_fd=parent, follow_symlinks=False)
            checked_directory(opened, io, private=private, owned=owned)
            checked_directory(named, io, private=private, owned=owned)
            require(identity(opened) == identity(named) == found_id, 'AGENT_BRIDGE_IDENTITY_CHANGED')
        for parent, leaf, fd, original, mode in self.files:
            opened = io.fstat(fd)
            named = io.stat(leaf, dir_fd=parent, follow_symlinks=False)
            checked_file(opened, io, mode)
            checked_file(named, io, mode)
            require(version(opened) == version(named) == original, 'AGENT_BRIDGE_IDENTITY_CHANGED')

    def file(self, parent, leaf, digest, limit, *, mode=None, content=False):
        io = self.io
        before = io.stat(leaf, dir_fd=parent, follow_symlinks=False)
        checked_file(before, io, mode)
        require(0 < before.st_size <= limit, 'AGENT_BRIDGE_FILE_TOO_LARGE')
        fd = io.open(leaf, io.O_RDONLY | io.O_NOFOLLOW | io.O_CLOEXEC | io.O_NONBLOCK, dir_fd=parent)
        self.stack.callback(io.close, fd)
        opened = io.fstat(fd)
        checked_file(opened, io, mode)
        require(version(before) == version(opened), 'AGENT_BRIDGE_IDENTITY_CHANGED')
        chunks, count, hasher = [], 0, hashlib.sha256()
        while True:
            chunk = io.read(fd, min(CHUNK_BYTES, limit + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            require(count <= limit, 'AGENT_BRIDGE_FILE_TOO_LARGE')
            hasher.update(chunk)
            if content:
                chunks.append(chunk)
        require(count == opened.st_size, 'AGENT_BRIDGE_IDENTITY_CHANGED')
        final = io.fstat(fd)
        checked_file(final, io, mode)
        require(version(opened) == version(final)
                == version(io.stat(leaf, dir_fd=parent, follow_symlinks=False)),
                'AGENT_BRIDGE_IDENTITY_CHANGED')
        require(hasher.hexdigest() == digest, 'AGENT_BRIDGE_HASH_MISMATCH')
        self.files.append((parent, leaf, fd, version(final), mode))
        self.recheck()
        return fd, b''.join(chunks) if content else None


def maybe_stat(parent, leaf, io):
    try:
        return io.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def make_directory(tree, parent, leaf):
    tree.recheck()
    tree.io.mkdir(leaf, mode=0o700, dir_fd=parent)
    child = tree.child(parent, leaf, private=True)
    tree.recheck()
    return child


def write_file(tree, parent, leaf, content, digest, limit, mode, *, source_fd=None):
    io = tree.io
    tree.recheck()
    fd = io.open(leaf, io.O_WRONLY | io.O_CREAT | io.O_EXCL | io.O_NOFOLLOW | io.O_CLOEXEC,
                 mode, dir_fd=parent)
    tree.stack.callback(io.close, fd)
    own_id = identity(io.fstat(fd))
    if source_fd is not None:
        require(io.lseek(source_fd, 0, io.SEEK_SET) == 0, 'AGENT_BRIDGE_READ_FAILED')
    written, hasher = 0, hashlib.sha256()
    while True:
        if source_fd is None:
            chunk, content = content[:CHUNK_BYTES], content[CHUNK_BYTES:]
        else:
            chunk = io.read(source_fd, CHUNK_BYTES)
        if not chunk:
            break
        written += len(chunk)
        require(written <= limit, 'AGENT_BRIDGE_FILE_TOO_LARGE')
        hasher.update(chunk)
        view = memoryview(chunk)
        while view:
            count = io.write(fd, view)
            require(type(count) is int and 0 < count <= len(view), 'AGENT_BRIDGE_WRITE_FAILED')
            view = view[count:]
    require(0 < written <= limit and hasher.hexdigest() == digest, 'AGENT_BRIDGE_HASH_MISMATCH')
    io.fchmod(fd, mode)
    io.fsync(fd)
    final = io.fstat(fd)
    checked_file(final, io, mode)
    tree.files.append((parent, leaf, fd, version(final), mode))
    require(identity(io.stat(leaf, dir_fd=parent, follow_symlinks=False)) == own_id,
            'AGENT_BRIDGE_IDENTITY_CHANGED')
    tree.recheck()
    tree.file(parent, leaf, digest, limit, mode=mode)


def checked_inventory(tree, bundle, bin_fd, src_fd):
    io = tree.io
    before = tuple(version(io.fstat(fd)) for fd in (bundle, bin_fd, src_fd))
    require(set(io.listdir(bundle)) == {'package.json', 'node', 'bin', 'src'}
            and set(io.listdir(bin_fd)) == {'axiosozo-agent-bridge.mjs'}
            and set(io.listdir(src_fd)) == {'server.mjs', 'channel.mjs', 'jsonl.mjs', 'tools.mjs'},
            'AGENT_BRIDGE_INVENTORY_REFUSED')
    tree.recheck()
    require(before == tuple(version(io.fstat(fd)) for fd in (bundle, bin_fd, src_fd)),
            'AGENT_BRIDGE_IDENTITY_CHANGED')


def run(command, *, source_root=None, storage_api=None, io=None, emit=print, fixture_node=None):
    # Keyword-only seams serve synthetic filesystem tests; none are CLI options.
    # fixture_node is (absolute synthetic source, digest); never an executable launch.
    require(command in {'setup', 'check'}, 'AGENT_BRIDGE_COMMAND_INVALID')
    io = os if io is None else io
    store = storage if storage_api is None else storage_api
    source_root = ROOT if source_root is None else Path(source_root)
    capabilities(io)
    root = Path(absolute(store.build_root(str(store.BUILD_ROOT))))
    require(store.mounted() is True, 'PROJECT_STORAGE_NOT_MOUNTED')
    node_path, node_digest = (NODE_SOURCE, NODE_SHA256) if fixture_node is None else fixture_node
    node_path = Path(absolute(node_path))
    bundle_digest = hashlib.sha256(manifest_bytes(node_digest)).hexdigest()
    if fixture_node is None:
        require(bundle_digest == BUNDLE_SHA256, 'AGENT_BRIDGE_PIN_INVALID')
    leaf = 'bridge-' + bundle_digest
    target = root / 'agent-bridge' / leaf
    with ExitStack() as stack:
        tree = RetainedTree(io, stack)
        source_fd = tree.directory(source_root / SOURCE_DIRECTORY)
        source_dirs = {'': source_fd,
                       'bin': tree.child(source_fd, 'bin'),
                       'src': tree.child(source_fd, 'src')}
        contents = {}
        for name, digest in SOURCE_PINS:
            relative = Path(name)
            _, contents[name] = tree.file(source_dirs[str(relative.parent) if relative.parent != Path('.') else ''],
                                         relative.name, digest, MAX_SOURCE_BYTES, content=True)
        node_parent = tree.directory(node_path.parent)
        node_fd, _ = tree.file(node_parent, node_path.name, node_digest, MAX_NODE_BYTES, mode=0o755)
        root_fd = tree.directory(root, owned=True)
        if maybe_stat(root_fd, 'agent-bridge', io) is None and command == 'setup':
            install_fd = make_directory(tree, root_fd, 'agent-bridge')
        else:
            install_fd = tree.child(root_fd, 'agent-bridge', private=True)
        existing = maybe_stat(install_fd, leaf, io)
        if existing is None and command == 'setup':
            bundle_fd = make_directory(tree, install_fd, leaf)
            bin_fd = make_directory(tree, bundle_fd, 'bin')
            src_fd = make_directory(tree, bundle_fd, 'src')
            destination_dirs = {'': bundle_fd, 'bin': bin_fd, 'src': src_fd}
            for name, digest in SOURCE_PINS:
                relative = Path(name)
                write_file(tree, destination_dirs[str(relative.parent) if relative.parent != Path('.') else ''],
                           relative.name, contents[name], digest, MAX_SOURCE_BYTES, 0o400)
            write_file(tree, bundle_fd, 'node', b'', node_digest, MAX_NODE_BYTES, 0o500, source_fd=node_fd)
            for fd in (bin_fd, src_fd, bundle_fd, install_fd, root_fd):
                io.fsync(fd)
        else:
            bundle_fd = tree.child(install_fd, leaf, private=True)
            bin_fd = tree.child(bundle_fd, 'bin', private=True)
            src_fd = tree.child(bundle_fd, 'src', private=True)
            destination_dirs = {'': bundle_fd, 'bin': bin_fd, 'src': src_fd}
            checked_inventory(tree, bundle_fd, bin_fd, src_fd)
            for name, digest in SOURCE_PINS:
                relative = Path(name)
                tree.file(destination_dirs[str(relative.parent) if relative.parent != Path('.') else ''],
                          relative.name, digest, MAX_SOURCE_BYTES, mode=0o400)
            tree.file(bundle_fd, 'node', node_digest, MAX_NODE_BYTES, mode=0o500)
        checked_inventory(tree, bundle_fd, bin_fd, src_fd)
        tree.recheck()
    emit(json.dumps({'status': 'PASS', 'build_root': str(root), 'bundle': str(target),
                     'bridge': str(target / 'bin/axiosozo-agent-bridge.mjs'),
                     'node': str(target / 'node'), 'bundle_sha256': bundle_digest,
                     'node_sha256': node_digest}, indent=2))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('setup', 'check'))
    try:
        raise SystemExit(run(parser.parse_args().command))
    except (RuntimeError, OSError) as cause:
        print(json.dumps({'status': 'BLOCKED_ENV', 'error': str(cause)}))
        raise SystemExit(20)
