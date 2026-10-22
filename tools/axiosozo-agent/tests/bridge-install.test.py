"""Synthetic filesystem tests only: no native process, Node, bridge or MCP runs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

STAGE = Path(__file__).resolve().parent
REPOSITORY = STAGE.parents[2]
SYNTHETIC_PARENT = Path('/Volumes/AxioSozoBuild/workstation/bridge-installer-synthetic-tests')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load('storage', REPOSITORY / 'scripts/storage.py')
installer = load('step8_bridge_installer', REPOSITORY / 'scripts/agent_bridge_install.py')


class FakeStorage:
    def __init__(self, root, mounted=True):
        self.BUILD_ROOT, self.active = root, mounted

    def build_root(self, value):
        if str(value) != str(self.BUILD_ROOT):
            raise AssertionError('unexpected storage spelling')
        return self.BUILD_ROOT

    def mounted(self):
        return self.active


class HookIO:
    def __init__(self, hook):
        self.hook = hook
        self.supports_dir_fd = {*os.supports_dir_fd, self.open, self.stat, self.mkdir}
        self.supports_follow_symlinks = {*os.supports_follow_symlinks, self.stat}
        self.supports_fd = {*os.supports_fd, self.listdir}

    def __getattr__(self, key):
        return getattr(os, key)

    def operation(self, name, args, kwargs):
        self.hook(name, args, kwargs)
        return getattr(os, name)(*args, **kwargs)

    def open(self, *args, **kwargs):
        return self.operation('open', args, kwargs)

    def stat(self, *args, **kwargs):
        return self.operation('stat', args, kwargs)

    def mkdir(self, *args, **kwargs):
        return self.operation('mkdir', args, kwargs)

    def read(self, *args, **kwargs):
        return self.operation('read', args, kwargs)

    def write(self, *args, **kwargs):
        return self.operation('write', args, kwargs)

    def listdir(self, *args, **kwargs):
        return self.operation('listdir', args, kwargs)


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SYNTHETIC_PARENT.parent.is_dir() or SYNTHETIC_PARENT.parent.is_symlink():
            raise RuntimeError('owned workstation root unavailable')
        SYNTHETIC_PARENT.mkdir(mode=0o700, exist_ok=True)
        if SYNTHETIC_PARENT.is_symlink() or SYNTHETIC_PARENT.resolve() != SYNTHETIC_PARENT:
            raise RuntimeError('synthetic parent containment unavailable')
        info = SYNTHETIC_PARENT.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
            raise RuntimeError('synthetic parent identity unavailable')
        cls.run_root = Path(tempfile.mkdtemp(prefix='owned-run-', dir=SYNTHETIC_PARENT))
        cls.executions = 0

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='case-', dir=self.run_root))
        self.source = self.base / 'source'
        self.build = self.base / 'build'
        self.source.mkdir(mode=0o700)
        self.build.mkdir(mode=0o700)
        self.source_bundle = self.source / 'packages/agent-bridge'
        self.source_bundle.mkdir(mode=0o700, parents=True)
        for relative, _ in installer.SOURCE_PINS:
            target = self.source_bundle / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes((REPOSITORY / 'packages/agent-bridge' / relative).read_bytes())
            target.chmod(0o600)
        self.node = self.base / 'synthetic-node-data'
        self.node_bytes = b'SYNTHETIC PINNED NODE DATA; NOT EXECUTABLE CODE\n'
        self.node.write_bytes(self.node_bytes)
        self.node.chmod(0o755)
        self.node_pin = hashlib.sha256(self.node_bytes).hexdigest()
        self.digest = hashlib.sha256(installer.manifest_bytes(self.node_pin)).hexdigest()
        self.target = self.build / 'agent-bridge' / ('bridge-' + self.digest)
        self.emitted = []
        # Any attempted process launch is a test failure, even a version query.
        self.guards = patch.multiple(os, system=self.no_execution, popen=self.no_execution,
                                     execv=self.no_execution, execve=self.no_execution,
                                     posix_spawn=self.no_execution, posix_spawnp=self.no_execution)
        self.guards.start()

    def tearDown(self):
        self.guards.stop()
        # Retain each exact own case as evidence. No sweeps or deletion.

    def no_execution(self, *args, **kwargs):
        type(self).executions += 1
        raise AssertionError('native execution forbidden')

    def run_installer(self, command='setup', **kwargs):
        return installer.run(command, source_root=self.source,
                             storage_api=FakeStorage(self.build), emit=self.emitted.append,
                             fixture_node=(self.node, self.node_pin), **kwargs)

    def snapshot(self):
        result = {}
        if self.target.exists():
            for path in self.target.rglob('*'):
                info = path.lstat()
                result[str(path.relative_to(self.target))] = (info.st_ino, info.st_mode,
                    path.read_bytes() if path.is_file() and not path.is_symlink() else None)
        return result

    def assert_refused(self, command='check', code=None, **kwargs):
        if code:
            with self.assertRaisesRegex(RuntimeError, code):
                self.run_installer(command, **kwargs)
        else:
            with self.assertRaises((RuntimeError, OSError)):
                self.run_installer(command, **kwargs)

    def test_manifest_policy_known_production_digest(self):
        self.assertEqual(hashlib.sha256(installer.manifest_bytes()).hexdigest(),
                         '7e3bc2f4aa1c034dacd1bb847461b01c2ef535ff1da4c8f2175e31261838bfda')
        self.assertTrue(installer.manifest_bytes().endswith((installer.NODE_SHA256 + '  node\n').encode()))

    def test_setup_check_and_verified_reuse(self):
        self.assertEqual(self.run_installer(), 0)
        original = self.snapshot()
        self.assertEqual(self.run_installer('check'), 0)
        self.assertEqual(self.run_installer('setup'), 0)
        self.assertEqual(original, self.snapshot())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        for relative, digest in installer.SOURCE_PINS:
            path = self.target / relative
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual((self.target / 'node').read_bytes(), self.node_bytes)
        self.assertEqual((self.target / 'node').stat().st_mode & 0o777, 0o500)
        receipt = json.loads(self.emitted[-1])
        self.assertEqual(receipt['node'], str(self.target / 'node'))
        self.assertEqual(receipt['bridge'], str(self.target / 'bin/axiosozo-agent-bridge.mjs'))
        self.assertEqual(receipt['bundle_sha256'], self.digest)
        self.assertEqual(type(self).executions, 0)

    def test_check_missing_has_no_writes(self):
        self.assert_refused()
        self.assertEqual(list(self.build.iterdir()), [])
        self.assertEqual(self.emitted, [])

    def test_source_hash_changed_before_install(self):
        (self.source_bundle / 'src/server.mjs').write_bytes(b'changed\n')
        self.assert_refused('setup', 'AGENT_BRIDGE_HASH_MISMATCH')
        self.assertEqual(list(self.build.iterdir()), [])

    def test_source_missing_before_install(self):
        (self.source_bundle / 'src/jsonl.mjs').unlink()
        self.assert_refused('setup')
        self.assertEqual(list(self.build.iterdir()), [])

    def test_source_oversize_before_install(self):
        (self.source_bundle / 'src/server.mjs').write_bytes(b'x' * (installer.MAX_SOURCE_BYTES + 1))
        self.assert_refused('setup', 'AGENT_BRIDGE_FILE_TOO_LARGE')

    def test_node_hash_changed_before_install(self):
        self.node.write_bytes(b'altered synthetic bytes\n')
        self.assert_refused('setup', 'AGENT_BRIDGE_HASH_MISMATCH')
        self.assertEqual(list(self.build.iterdir()), [])

    def test_node_wrong_mode_before_install(self):
        self.node.chmod(0o700)
        self.assert_refused('setup', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_node_symlink_before_install(self):
        actual = self.base / 'actual-node-data'
        self.node.rename(actual)
        self.node.symlink_to(actual)
        self.assert_refused('setup', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_source_hardlink_before_install(self):
        os.link(self.source_bundle / 'package.json', self.base / 'linked-package')
        self.assert_refused('setup', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_source_parent_symlink_before_install(self):
        original = self.source_bundle / 'src'
        actual = self.base / 'actual-src'
        original.rename(actual)
        original.symlink_to(actual, target_is_directory=True)
        self.assert_refused('setup', 'AGENT_BRIDGE_DIRECTORY_REFUSED')

    def test_existing_hash_mismatch_never_overwritten(self):
        self.run_installer()
        path = self.target / 'src/channel.mjs'
        path.chmod(0o600)
        path.write_bytes(b'altered\n')
        path.chmod(0o400)
        original = self.snapshot()
        self.assert_refused('setup', 'AGENT_BRIDGE_HASH_MISMATCH')
        self.assertEqual(original, self.snapshot())

    def test_existing_unknown_file_never_removed(self):
        self.run_installer()
        extra = self.target / 'src/unknown.mjs'
        extra.write_bytes(b'untouched\n')
        extra.chmod(0o400)
        original = self.snapshot()
        self.assert_refused('setup', 'AGENT_BRIDGE_INVENTORY_REFUSED')
        self.assertEqual(original, self.snapshot())

    def test_existing_missing_file_never_filled(self):
        self.run_installer()
        (self.target / 'src/tools.mjs').unlink()
        original = self.snapshot()
        self.assert_refused('setup', 'AGENT_BRIDGE_INVENTORY_REFUSED')
        self.assertEqual(original, self.snapshot())

    def test_existing_file_mode_refused(self):
        self.run_installer()
        (self.target / 'src/tools.mjs').chmod(0o600)
        self.assert_refused('check', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_existing_node_mode_refused(self):
        self.run_installer()
        (self.target / 'node').chmod(0o755)
        self.assert_refused('check', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_existing_hardlink_refused(self):
        self.run_installer()
        os.link(self.target / 'node', self.base / 'linked-node')
        self.assert_refused('check', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_existing_file_symlink_refused(self):
        self.run_installer()
        path = self.target / 'src/server.mjs'
        path.unlink()
        path.symlink_to(self.source_bundle / 'src/server.mjs')
        self.assert_refused('check', 'AGENT_BRIDGE_FILE_REFUSED')

    def test_existing_namespace_symlink_refused(self):
        self.run_installer()
        actual = self.base / 'actual-bundle'
        self.target.rename(actual)
        self.target.symlink_to(actual, target_is_directory=True)
        self.assert_refused('check', 'AGENT_BRIDGE_DIRECTORY_REFUSED')

    def test_existing_directory_mode_refused(self):
        self.run_installer()
        (self.target / 'bin').chmod(0o755)
        self.assert_refused('check', 'AGENT_BRIDGE_DIRECTORY_REFUSED')

    def test_build_root_writable_mode_refused(self):
        self.build.chmod(0o777)
        self.assert_refused('setup', 'AGENT_BRIDGE_DIRECTORY_REFUSED')
        self.assertEqual(list(self.build.iterdir()), [])

    def test_unmounted_storage_never_installs(self):
        with self.assertRaisesRegex(RuntimeError, 'PROJECT_STORAGE_NOT_MOUNTED'):
            installer.run('setup', source_root=self.source, storage_api=FakeStorage(self.build, False),
                          fixture_node=(self.node, self.node_pin), emit=self.emitted.append)
        self.assertEqual(list(self.build.iterdir()), [])

    def test_bad_command_never_installs(self):
        self.assert_refused('start', 'AGENT_BRIDGE_COMMAND_INVALID')
        self.assertEqual(list(self.build.iterdir()), [])

    def test_bad_canonical_spelling_refused(self):
        for spelling in (str(self.build) + '/', str(self.build) + '/../build',
                         str(self.build) + '//child', str(self.build) + '/bad\nname'):
            with self.subTest(spelling=repr(spelling)):
                with self.assertRaisesRegex(RuntimeError, 'AGENT_BRIDGE_PATH_REFUSED'):
                    installer.run('setup', source_root=self.source, storage_api=FakeStorage(spelling),
                                  fixture_node=(self.node, self.node_pin), emit=self.emitted.append)
        self.assertEqual(list(self.build.iterdir()), [])

    def test_source_file_substitution_during_read_refused(self):
        path = self.source_bundle / 'package.json'
        original_id = installer.identity(path.stat())
        fired = False
        def hook(name, args, kwargs):
            nonlocal fired
            if name == 'read' and not fired and installer.identity(os.fstat(args[0])) == original_id:
                fired = True
                data = path.read_bytes()
                path.rename(self.base / 'old-package')
                path.write_bytes(data)
                path.chmod(0o600)
        self.assert_refused('setup', 'AGENT_BRIDGE_IDENTITY_CHANGED', io=HookIO(hook))
        self.assertTrue(fired)
        self.assertEqual(list(self.build.iterdir()), [])

    def test_source_full_ancestor_substitution_refused(self):
        first_id = installer.identity((self.source_bundle / 'package.json').stat())
        fired = False
        def hook(name, args, kwargs):
            nonlocal fired
            if name == 'read' and not fired and installer.identity(os.fstat(args[0])) == first_id:
                fired = True
                self.source.rename(self.base / 'retained-source')
                self.source.mkdir(mode=0o700)
        self.assert_refused('setup', 'AGENT_BRIDGE_IDENTITY_CHANGED', io=HookIO(hook))
        self.assertTrue(fired)

    def test_build_full_ancestor_substitution_during_write_refused(self):
        fired = False
        def hook(name, args, kwargs):
            nonlocal fired
            if name == 'write' and not fired:
                fired = True
                self.build.rename(self.base / 'retained-build')
                self.build.mkdir(mode=0o700)
        self.assert_refused('setup', 'AGENT_BRIDGE_IDENTITY_CHANGED', io=HookIO(hook))
        self.assertTrue(fired)
        self.assertEqual(list(self.build.iterdir()), [])

    def test_write_failure_leaves_exact_partial_namespace_and_no_reuse(self):
        def hook(name, args, kwargs):
            if name == 'write':
                raise OSError('injected write interruption')
        self.assert_refused('setup', io=HookIO(hook))
        self.assertTrue(self.target.is_dir())
        self.assertEqual(self.emitted, [])
        original = self.snapshot()
        self.assert_refused('setup', 'AGENT_BRIDGE_INVENTORY_REFUSED')
        self.assertEqual(original, self.snapshot())

    def test_partial_writes_complete_pinned_bytes(self):
        io = HookIO(lambda *args: None)
        io.write = lambda fd, view: os.write(fd, view[:7])
        self.assertEqual(self.run_installer(io=io), 0)
        self.assertEqual(self.run_installer('check'), 0)

    def test_node_change_after_source_verification_refused(self):
        fired = False
        def hook(name, args, kwargs):
            nonlocal fired
            if name == 'mkdir' and args[0] == 'agent-bridge' and not fired:
                fired = True
                self.node.write_bytes(self.node_bytes)
                self.node.chmod(0o755)
        self.assert_refused('setup', 'AGENT_BRIDGE_IDENTITY_CHANGED', io=HookIO(hook))
        self.assertTrue(fired)

    def test_unknown_inventory_inserted_during_final_listing_refused(self):
        self.run_installer()
        src_id = installer.identity((self.target / 'src').stat())
        io = HookIO(lambda *args: None)
        fired = False
        def listing(fd):
            nonlocal fired
            result = os.listdir(fd)
            if installer.identity(os.fstat(fd)) == src_id and not fired:
                fired = True
                (self.target / 'src/late-unknown').write_bytes(b'unknown')
            return result
        io.listdir = listing
        io.supports_fd.add(io.listdir)
        self.assert_refused('check', 'AGENT_BRIDGE_IDENTITY_CHANGED', io=io)
        self.assertTrue(fired)

    def test_written_file_substitution_before_readback_refused(self):
        calls, fired = 0, False
        path = self.target / 'package.json'
        def hook(name, args, kwargs):
            nonlocal calls, fired
            if name == 'stat' and args[0] == 'package.json' and path.exists():
                parent = kwargs.get('dir_fd')
                if parent is not None and installer.identity(os.fstat(parent)) == installer.identity(self.target.stat()):
                    calls += 1
                    if calls == 2:
                        fired = True
                        data = path.read_bytes()
                        path.rename(self.base / 'original-written-package')
                        path.write_bytes(data)
                        path.chmod(0o400)
        self.assert_refused('setup', 'AGENT_BRIDGE_IDENTITY_CHANGED', io=HookIO(hook))
        self.assertTrue(fired)
        self.assertEqual(self.emitted, [])

    def test_zero_write_refused_and_no_receipt(self):
        io = HookIO(lambda *args: None)
        io.write = lambda *args: 0
        self.assert_refused('setup', 'AGENT_BRIDGE_WRITE_FAILED', io=io)
        self.assertEqual(self.emitted, [])

    def test_node_size_bound_refused_without_large_fixture(self):
        with patch.object(installer, 'MAX_NODE_BYTES', len(self.node_bytes) - 1):
            self.assert_refused('setup', 'AGENT_BRIDGE_FILE_TOO_LARGE')
        self.assertEqual(list(self.build.iterdir()), [])

    def test_containment_capability_missing_refused(self):
        io = HookIO(lambda *args: None)
        io.O_NOFOLLOW = 0
        self.assert_refused('setup', 'AGENT_BRIDGE_CONTAINMENT_UNAVAILABLE', io=io)
        self.assertEqual(list(self.build.iterdir()), [])

    def test_owner_mismatch_refused_without_chown(self):
        io = HookIO(lambda *args: None)
        original_stat = io.stat
        def altered(*args, **kwargs):
            found = original_stat(*args, **kwargs)
            if args[0] == self.node.name:
                values = {name: getattr(found, name) for name in dir(found) if name.startswith('st_')}
                values['st_uid'] = os.getuid() + 1
                return types.SimpleNamespace(**values)
            return found
        io.stat = altered
        io.supports_dir_fd.add(io.stat)
        io.supports_follow_symlinks.add(io.stat)
        self.assert_refused('setup', 'AGENT_BRIDGE_FILE_REFUSED', io=io)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstallerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'synthetic_run_root': str(InstallerTests.run_root),
                      'tests': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'native_executions': InstallerTests.executions}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
