"""Build-volume setup logic with fake helpers, a temporary lock and a virtual
clock. No real disk image, volume, mount or system helper is touched."""
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import storage  # noqa: E402

# Like the real helpers, it tries MOUNT_LOCK without waiting. It never creates
# the lock file, so the tests see the mode that storage.py creates it with.
HELPER = '''import fcntl, os, sys
state = 'lock-free'
try:
    fd = os.open({lock!r}, os.O_RDWR)
except FileNotFoundError:
    fd = None
if fd is not None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        state = 'lock-held'
with open({log!r}, 'a') as log:
    log.write(state + '\\n')
if {say}:
    print('helper stdout')
sys.exit({status})
'''


class Clock:
    """Virtual time for the polling loops; sleep() only advances it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='storage-unit-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.lock = self.base / 'Caches' / 'axiosozo-mount.lock'
        self.lock.parent.mkdir()
        self.log = self.base / 'helper.log'
        self.clock = Clock()
        for name, value in {'MOUNT_LOCK': self.lock, 'MOUNT_SHARED': self.helper('shared', 0),
                            'MOUNT_HELPER': self.helper('helper', 0), 'time': self.clock}.items():
            active = patch.object(storage, name, value)
            active.start()
            self.addCleanup(active.stop)

    def helper(self, name, status, say=False):
        """A fake helper that records whether MOUNT_LOCK was free while it ran."""
        path = self.base / name
        path.write_text('#!' + sys.executable + '\n'
                        + HELPER.format(log=str(self.log), lock=str(self.lock), status=status, say=say))
        path.chmod(0o700)
        return path

    def runs(self):
        return self.log.read_text().split() if self.log.exists() else []


class Paths(unittest.TestCase):
    def test_helpers_and_lock_follow_the_home_directory(self):
        home = Path.home()
        self.assertEqual(storage.MOUNT_SHARED, home / '.local/bin/mount-dev-storage')
        self.assertEqual(storage.MOUNT_HELPER, home / '.local/bin/mount-axiosozo')
        self.assertEqual(storage.MOUNT_LOCK, home / 'Library/Caches/axiosozo-mount.lock')
        # A public repository: no account-specific absolute path in the script.
        self.assertNotIn('/Users/', Path(storage.__file__).read_text())


class HelperPolling(StorageTestCase):
    def test_failing_helper_exit_is_ignored_once_the_volume_is_verified(self):
        storage.MOUNT_HELPER = self.helper('helper', 1)
        with patch.object(storage, 'mounted', return_value=True):
            self.assertTrue(storage.wait_for_helper())
        self.assertEqual(self.runs(), ['lock-free'])

    def test_helper_runs_without_any_lock_held_and_is_retried_at_most_every_interval(self):
        started = []
        real = storage.run_helper

        def recorded(helper):
            started.append(self.clock.now)
            return real(helper)
        with patch.object(storage, 'run_helper', side_effect=recorded), \
             patch.object(storage, 'mounted', return_value=False):
            self.assertFalse(storage.wait_for_helper())
        self.assertGreaterEqual(self.clock.now - started[0], storage.HELPER_WAIT)
        self.assertLessEqual(len(started), storage.HELPER_WAIT // storage.HELPER_RETRY + 1)
        self.assertGreater(len(started), 1)
        self.assertTrue(all(later - earlier >= storage.HELPER_RETRY
                            for earlier, later in zip(started, started[1:])))
        self.assertEqual(set(self.runs()), {'lock-free'})

    def test_volume_mounted_by_another_run_ends_the_wait_without_a_rerun(self):
        # The helper returns at once (global lock busy); a concurrent run mounts.
        with patch.object(storage, 'mounted', side_effect=[False, False, False, True]):
            self.assertTrue(storage.wait_for_helper())
        self.assertEqual(len(self.runs()), 1)

    def test_helper_output_never_reaches_stdout(self):
        saved = os.dup(2)
        try:
            with tempfile.TemporaryFile() as captured:
                os.dup2(captured.fileno(), 2)
                try:
                    with tempfile.TemporaryFile() as out:
                        saved_out = os.dup(1)
                        os.dup2(out.fileno(), 1)
                        try:
                            self.assertEqual(storage.run_helper(self.helper('talker', 0, say=True)), 0)
                        finally:
                            os.dup2(saved_out, 1)
                            os.close(saved_out)
                        out.seek(0)
                        self.assertEqual(out.read(), b'')
                finally:
                    os.dup2(saved, 2)
                captured.seek(0)
                self.assertIn(b'helper stdout', captured.read())
        finally:
            os.close(saved)


class Ensure(StorageTestCase):
    def setUp(self):
        super().setUp()
        for name in ('preflight', 'attach_and_mount'):
            active = patch.object(storage, name)
            setattr(self, name, active.start())
            self.addCleanup(active.stop)
        # The image layout, where the helper mounts the build volume.
        for active in (patch.object(storage, 'report', return_value={'mounted': True}),
                       patch.object(storage, 't9_container', return_value=None)):
            active.start()
            self.addCleanup(active.stop)

    def test_native_layout_mounts_from_the_container_without_the_image_helper(self):
        with patch.object(storage, 'IMAGE', self.base / 'Dev.sparsebundle'), \
             patch.object(storage, 't9_container', return_value='disk5'), \
             patch.object(storage, 'mounted', side_effect=[False, False, True]):
            self.assertEqual(storage.ensure(), {'mounted': True})
        self.attach_and_mount.assert_called_once_with()
        self.assertEqual(self.runs(), ['lock-free'], 'only the DevStorage helper ran')

    def test_devstorage_failure_stops_before_the_build_volume(self):
        storage.MOUNT_SHARED = self.helper('shared', 1)
        with patch.object(storage, 'mounted') as mounted, self.assertRaises(RuntimeError) as raised:
            storage.ensure()
        self.assertIn('DevStorage', str(raised.exception))
        self.assertIn('exited 1', str(raised.exception))
        mounted.assert_not_called()
        self.assertEqual(len(self.runs()), 1)

    def test_already_mounted_runs_no_build_helper(self):
        with patch.object(storage, 'mounted', return_value=True):
            self.assertEqual(storage.ensure(), {'mounted': True})
        self.assertEqual(len(self.runs()), 1)  # mount-dev-storage only
        self.preflight.assert_not_called()

    def test_helper_exit_status_does_not_decide_setup(self):
        storage.MOUNT_HELPER = self.helper('helper', 1)
        with patch.object(storage, 'mounted', side_effect=[False, True]):
            self.assertEqual(storage.ensure(), {'mounted': True})
        self.attach_and_mount.assert_not_called()
        self.assertEqual(self.runs(), ['lock-free', 'lock-free'])

    def test_unverified_volume_after_the_wait_is_reported(self):
        with patch.object(storage, 'mounted', return_value=False), self.assertRaises(RuntimeError) as raised:
            storage.ensure()
        self.assertIn('still not verified', str(raised.exception))
        self.attach_and_mount.assert_not_called()

    def test_fallback_mounts_only_while_holding_the_global_lock(self):
        storage.MOUNT_HELPER = self.base / 'missing-helper'

        def attach():
            with open(self.lock, 'a') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.attach_and_mount.side_effect = attach
        with patch.object(storage, 'mounted', side_effect=[False, False, True]):
            self.assertEqual(storage.ensure(), {'mounted': True})
        self.attach_and_mount.assert_called_once_with()
        self.assertEqual(self.preflight.call_count, 2)
        with open(self.lock, 'a') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released afterwards
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o600)

    def test_fallback_skips_mounting_if_a_helper_mounted_during_the_lock_wait(self):
        storage.MOUNT_HELPER = self.base / 'missing-helper'
        with patch.object(storage, 'mounted', side_effect=[False, True, True]):
            self.assertEqual(storage.ensure(), {'mounted': True})
        self.attach_and_mount.assert_not_called()

    def test_fallback_gives_up_on_a_busy_global_lock(self):
        storage.MOUNT_HELPER = self.base / 'missing-helper'
        with open(self.lock, 'a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with patch.object(storage, 'mounted', return_value=False), self.assertRaises(RuntimeError) as raised:
                storage.ensure()
        self.assertIn('Another mount still holds', str(raised.exception))
        self.assertGreaterEqual(self.clock.now - 1000.0, storage.MOUNT_LOCK_WAIT)
        self.attach_and_mount.assert_not_called()

    def test_global_lock_path_is_never_followed_as_a_symlink(self):
        target = self.base / 'elsewhere'
        target.write_text('')
        self.lock.symlink_to(target)
        with self.assertRaises(OSError) as raised:
            with storage.mount_lock():
                pass
        self.assertEqual(raised.exception.errno, errno.ELOOP)


class Preflight(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='storage-preflight-')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.volume, self.image = base / 'AxioSozoBuild', base / 'Dev.sparsebundle'
        self.image.mkdir()
        for name, value in (('VOLUME', self.volume), ('IMAGE', self.image)):
            active = patch.object(storage, name, value)
            active.start()
            self.addCleanup(active.stop)
        # Only T9 counts as mounted; the temporary mountpoint never is. The
        # image layout: T9 itself is no APFS container.
        for active in (patch.object(Path, 'is_mount', lambda path: str(path) == '/Volumes/T9'),
                       patch.object(storage, 't9_container', return_value=None)):
            active.start()
            self.addCleanup(active.stop)

    def test_absent_or_empty_mountpoint_passes(self):
        storage.preflight()
        self.volume.mkdir()
        storage.preflight()

    def test_missing_image_is_never_replaced(self):
        self.image.rmdir()
        with self.assertRaisesRegex(RuntimeError, 'never creates disk images'):
            storage.preflight()

    def test_files_under_the_mountpoint_are_never_hidden(self):
        self.volume.mkdir()
        (self.volume / 'stray').write_text('')
        with self.assertRaisesRegex(RuntimeError, 'obscure'):
            storage.preflight()

    def test_unreadable_leftover_mountpoint_names_the_fix(self):
        self.volume.mkdir(mode=0o111)
        self.addCleanup(self.volume.chmod, 0o700)
        if os.access(self.volume, os.R_OK):
            self.skipTest('running with privileges that ignore directory modes')
        with self.assertRaisesRegex(RuntimeError, 'sudo rmdir'):
            storage.preflight()


class NativeLayout(unittest.TestCase):
    """Since October 2026 the build volume is a volume of T9's own APFS container."""
    def disks(self, build):
        t9 = {'VolumeName': 'T9', 'MountPoint': '/Volumes/T9', 'FilesystemType': 'apfs', 'APFSContainerReference': 'disk5'}
        return lambda target: t9 if str(target) == '/Volumes/T9' else build

    def verified(self, build, images=()):
        with patch.object(Path, 'is_mount', lambda path: str(path) in ('/Volumes/T9', str(storage.VOLUME))), \
             patch.object(storage, 'disk_info', side_effect=self.disks(build)), \
             patch.object(storage, 'image_mounts', return_value=list(images)):
            return storage.volume_verified()

    def test_build_volume_in_the_t9_container_is_verified(self):
        build = {'VolumeName': 'AxioSozoBuild', 'MountPoint': str(storage.VOLUME), 'FilesystemType': 'apfs',
                 'APFSContainerReference': 'disk5'}
        self.assertTrue(self.verified(build))
        self.assertFalse(self.verified({**build, 'APFSContainerReference': 'disk7'}), 'another container')
        self.assertTrue(self.verified({**build, 'APFSContainerReference': 'disk7'}, [str(storage.VOLUME)]), 'legacy image')
        self.assertFalse(self.verified({**build, 'VolumeName': 'Other'}))
        self.assertFalse(self.verified({**build, 'FilesystemType': 'exfat'}))

    def test_fallback_mounts_only_the_unmounted_build_volume_of_the_container(self):
        calls = []
        volumes = [('disk5s1', 'T9', '/Volumes/T9'), ('disk5s2', 'DevStorage', '/Volumes/DevStorage'),
                   ('disk5s3', 'AxioSozoBuild', None)]
        with patch.object(storage, 'IMAGE', Path('/nonexistent/Dev.sparsebundle')), \
             patch.object(storage, 't9_container', return_value='disk5'), \
             patch.object(storage, 'container_volumes', return_value=volumes), \
             patch.object(storage, 'image_entities', side_effect=AssertionError('no image attach')), \
             patch.object(storage.subprocess, 'run', side_effect=lambda argv, **_: calls.append(argv)):
            storage.attach_and_mount()
            self.assertEqual(calls, [['/usr/sbin/diskutil', 'mount', 'nobrowse', 'disk5s3']])
            with patch.object(storage, 'container_volumes', return_value=volumes[:2]):
                with self.assertRaisesRegex(RuntimeError, 'Expected exactly one'):
                    storage.attach_and_mount()

    def test_preflight_needs_no_image_in_the_native_layout(self):
        with tempfile.TemporaryDirectory(prefix='storage-native-') as directory, \
             patch.object(storage, 'VOLUME', Path(directory) / 'AxioSozoBuild'), \
             patch.object(storage, 'IMAGE', Path(directory) / 'Dev.sparsebundle'), \
             patch.object(Path, 'is_mount', lambda path: str(path) == '/Volumes/T9'), \
             patch.object(storage, 't9_container', return_value='disk5'):
            storage.preflight()


class AttachAndMount(unittest.TestCase):
    def test_only_attaches_without_mounting_and_mounts_the_build_volume(self):
        calls = []
        entities = [{'dev-entry': '/dev/disk9s1', 'mount-point': '/elsewhere'},
                    {'dev-entry': '/dev/disk9s2'}, {'dev-entry': '/dev/disk9s3'}]
        names = {'/dev/disk9s2': 'DevStorage', '/dev/disk9s3': 'AxioSozoBuild'}
        with patch.object(storage, 't9_container', return_value=None), \
             patch.object(storage, 'image_entities', side_effect=[None, entities]), \
             patch.object(storage, 'disk_info', side_effect=lambda device: {
                 'VolumeName': names[device], 'FilesystemType': 'apfs'}), \
             patch.object(storage.subprocess, 'run', side_effect=lambda argv, **_: calls.append(argv)):
            storage.attach_and_mount()
        self.assertEqual(calls, [['/usr/bin/hdiutil', 'attach', '-nomount', str(storage.IMAGE)],
                                 ['/usr/sbin/diskutil', 'mount', 'nobrowse', '/dev/disk9s3']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
