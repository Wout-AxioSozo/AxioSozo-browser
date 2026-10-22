"""Isolated filesystem/mocked-OS tests. No real fixture install or process run."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest.mock import patch

WORKTREE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("understand_fixture_install", WORKTREE / "scripts/understand_fixture_install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
ORIGINAL_INPUTS = installer.INPUTS
ORIGINAL_SOURCE = installer.SOURCE
RUN_ID = "0123456789abcdef0123456789abcdef"


class StagedPinsTests(unittest.TestCase):
    def test_closed_staged_inputs_match_native_pins_and_their_public_bytes(self):
        pin_source = (WORKTREE / "apps/browser/chrome/UnderstandFixturePins.sys.mjs").read_text()
        array = re.search(r"UNDERSTAND_FIXTURE_INPUTS = Object.freeze\((\[.*?\])\.map", pin_source, re.S)
        self.assertIsNotNone(array)
        native_pins = {item["relative"]: (item["sha256"], item["maxBytes"]) for item in json.loads(array[1])}
        self.assertEqual(len(native_pins), 9)
        self.assertEqual(native_pins, {relative: (digest, maximum) for relative, _source, digest, maximum in ORIGINAL_INPUTS})
        for _relative, source, digest, maximum in ORIGINAL_INPUTS:
            raw = (ORIGINAL_SOURCE / source).read_bytes()
            self.assertTrue(0 < len(raw) <= maximum)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        # TMPDIR is set by the reviewed external-storage command wrapper.
        self.temporary = tempfile.TemporaryDirectory(prefix="understand-installer-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.worktree = self.base / "worktree"
        self.worktree.mkdir(mode=0o700)
        self.source = self.worktree / "staged"
        self.source.mkdir(mode=0o700)
        self.volume = self.base / "volume"
        self.volume.mkdir(mode=0o700)
        self.build = self.volume / "workstation"
        self.build.mkdir(mode=0o700)
        self.fixtures = self.build / "gui-fixtures"
        self.fixtures.mkdir(mode=0o700)
        self.profile_base = self.build / "runtime/fixed-build"
        self.profile = self.profile_base / ("plan4-understand-" + RUN_ID) / "gecko"
        self.profile.mkdir(parents=True, mode=0o700)
        self.root = self.fixtures / ("understand-" + RUN_ID)
        self.image = self.base / "fake.sparsebundle"
        self.image.mkdir(mode=0o700)
        self.python = self.volume / "python"
        self.node = self.volume / "node"
        self.python.write_bytes(b"fake-python-only-bytes")
        self.node.write_bytes(b"fake-node-only-bytes")
        self.python.chmod(0o755)
        self.node.chmod(0o755)
        inputs = []
        for index, (relative, source, _digest, _maximum) in enumerate(ORIGINAL_INPUTS):
            raw = ("fixed synthetic input " + str(index)).encode()
            path = self.source / source
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(raw)
            inputs.append((relative, source, hashlib.sha256(raw).hexdigest(), 256))
        replacements = {"WORKTREE": self.worktree, "SOURCE": self.source, "VOLUME": self.volume,
                        "BUILD": self.build, "T9": self.image, "BASE": self.fixtures,
                        "PROFILE_BASE": self.profile_base, "PYTHON": self.python, "NODE": self.node,
                        "INPUTS": tuple(inputs), "BINARY_PINS": tuple((path, hashlib.sha256(path.read_bytes()).hexdigest(), 256)
                                                                      for path in (self.python, self.node))}
        for name, value in replacements.items():
            active = patch.object(installer, name, value)
            active.start()
            self.addCleanup(active.stop)
        environment = {"AXIOSOZO_BUILD_ROOT": str(self.build), "AXIOSOZO_SYNTHETIC_TEST": "1",
                       "AXIOSOZO_UNDERSTAND_GUI_FIXTURE": "1", "AXIOSOZO_UNDERSTAND_GUI_FIXTURE_ROOT": str(self.root)}
        active = patch.dict(os.environ, environment, clear=True)
        active.start()
        self.addCleanup(active.stop)
        self.old_cwd = Path.cwd()
        os.chdir(self.worktree)
        self.addCleanup(os.chdir, self.old_cwd)
        active = patch.object(installer, "mounted_volume")
        self.mount = active.start()
        self.addCleanup(active.stop)

    def refuse(self, callback):
        with self.assertRaises((RuntimeError, OSError)):
            callback()

    def test_layout_accepts_only_fixed_lowercase_32_hex_ids(self):
        self.assertEqual(installer.layout(RUN_ID), (self.root, self.profile))
        for value in (None, "", RUN_ID.upper(), "a" * 31, "a" * 33, "../" + RUN_ID, "/" + RUN_ID):
            self.refuse(lambda: installer.layout(value))

    def test_missing_or_wrong_gate_refuses_before_mount_or_creation(self):
        for name in ("AXIOSOZO_BUILD_ROOT", "AXIOSOZO_SYNTHETIC_TEST", "AXIOSOZO_UNDERSTAND_GUI_FIXTURE", "AXIOSOZO_UNDERSTAND_GUI_FIXTURE_ROOT"):
            with patch.dict(os.environ, {name: "wrong"}):
                self.refuse(lambda: installer.install(RUN_ID))
            self.assertFalse(self.root.exists())
        self.mount.assert_not_called()

    def test_wrong_worktree_refuses_before_mount(self):
        os.chdir(self.base)
        self.refuse(lambda: installer.check(RUN_ID))
        self.mount.assert_not_called()

    def test_profile_must_preexist_and_is_never_created(self):
        self.profile.rmdir()
        self.refuse(lambda: installer.install(RUN_ID))
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.root.exists())

    def test_profile_private_mode_and_no_symlink_are_required(self):
        self.profile.chmod(0o755)
        self.refuse(lambda: installer.install(RUN_ID))
        self.profile.chmod(0o700)
        moved = self.profile.with_name("owned-original")
        self.profile.rename(moved)
        self.profile.symlink_to(moved, target_is_directory=True)
        self.refuse(lambda: installer.install(RUN_ID))
        self.assertFalse(self.root.exists())

    def test_profile_contents_are_not_enumerated_or_opened(self):
        real_open = os.open
        profile_fd_opens = []
        def audited_open(path, flags, *args, **kwargs):
            if str(path) == "gecko":
                profile_fd_opens.append(flags)
                self.assertTrue(flags & os.O_DIRECTORY)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(Path, "iterdir", side_effect=AssertionError("no directory enumeration")), patch.object(os, "listdir", side_effect=AssertionError("no directory enumeration")), patch.object(os, "scandir", side_effect=AssertionError("no directory enumeration")), patch.object(os, "open", audited_open):
            result = installer.install(RUN_ID)
        self.assertGreater(len(profile_fd_opens), 0)
        self.assertFalse(result["profile_created"])
        self.assertFalse(result["profile_contents_read"])

    def test_install_and_check_only_create_private_readonly_inputs(self):
        result = installer.install(RUN_ID)
        self.assertEqual(result["providers"], "NOT_AUTHORIZED")
        self.assertEqual(result["input_count"], 9)
        for full in (self.root, *(self.root / name for name in installer.DIRECTORIES)):
            self.assertEqual(stat.S_IMODE(full.stat().st_mode), 0o700)
        for relative, source, digest, _maximum in installer.INPUTS:
            full = self.root / relative
            self.assertEqual(stat.S_IMODE(full.stat().st_mode), 0o400)
            self.assertEqual(full.stat().st_nlink, 1)
            self.assertEqual(hashlib.sha256(full.read_bytes()).hexdigest(), digest)
        self.assertEqual(installer.check(RUN_ID), result)

    def test_second_install_refuses_without_overwrite(self):
        installer.install(RUN_ID)
        before = (self.root / "policy.json").stat()
        self.refuse(lambda: installer.install(RUN_ID))
        after = (self.root / "policy.json").stat()
        self.assertEqual(installer.file_identity(before), installer.file_identity(after))

    def test_check_requires_existing_install(self):
        self.refuse(lambda: installer.check(RUN_ID))
        self.assertFalse(self.root.exists())

    def test_source_wrong_hash_refuses_before_root_creation(self):
        (self.source / installer.INPUTS[0][1]).write_bytes(b"different")
        self.refuse(lambda: installer.install(RUN_ID))
        self.assertFalse(self.root.exists())

    def test_source_symlink_and_hardlink_refuse(self):
        source = self.source / installer.INPUTS[0][1]
        moved = source.with_name("other")
        source.rename(moved)
        source.symlink_to(moved)
        self.refuse(lambda: installer.install(RUN_ID))
        source.unlink()
        os.link(moved, source)
        self.refuse(lambda: installer.install(RUN_ID))
        self.assertFalse(self.root.exists())

    def test_exfat_public_source_mode_is_not_private_admission(self):
        for _relative, source, _digest, _maximum in installer.INPUTS:
            (self.source / source).chmod(0o777)
        self.source.chmod(0o777)
        self.assertEqual(installer.install(RUN_ID)["status"], "ok")

    def test_binary_hash_and_exact_executable_mode_are_required(self):
        self.node.chmod(0o775)
        self.refuse(lambda: installer.install(RUN_ID))
        self.node.chmod(0o755)
        self.node.write_bytes(b"other-node")
        self.refuse(lambda: installer.install(RUN_ID))
        self.assertFalse(self.root.exists())

    def test_binary_symlink_refuses(self):
        moved = self.node.with_name("other-node")
        self.node.rename(moved)
        self.node.symlink_to(moved)
        self.refuse(lambda: installer.install(RUN_ID))
        self.assertFalse(self.root.exists())

    def test_installed_hash_mode_and_link_count_are_rechecked(self):
        installer.install(RUN_ID)
        full = self.root / "policy.json"
        full.chmod(0o600)
        self.refuse(lambda: installer.check(RUN_ID))
        full.write_bytes(b"tampered")
        full.chmod(0o400)
        self.refuse(lambda: installer.check(RUN_ID))
        os.link(full, self.root / "policy-copy.json")
        self.refuse(lambda: installer.check(RUN_ID))

    def test_installed_symlink_is_never_followed(self):
        installer.install(RUN_ID)
        full = self.root / "policy.json"
        moved = self.root / "policy-original.json"
        full.rename(moved)
        full.symlink_to(moved)
        self.refuse(lambda: installer.check(RUN_ID))

    def test_retained_directory_detects_namespace_replacement(self):
        full = self.base / "directory"
        full.mkdir(mode=0o700)
        with installer.Directory(full, private=True) as held:
            full.rename(self.base / "old-directory")
            full.mkdir(mode=0o700)
            self.refuse(held.check)

    def test_retained_file_detects_same_bytes_named_replacement(self):
        source = self.source / installer.INPUTS[0][1]
        digest, maximum = installer.INPUTS[0][2:]
        with installer.PinnedFile(source, digest, maximum, source=True) as held:
            raw = source.read_bytes()
            source.rename(source.with_name("old-input"))
            source.write_bytes(raw)
            self.refuse(held.check)

    def test_pinned_file_refuses_byte_bound(self):
        source = self.source / installer.INPUTS[0][1]
        digest = installer.INPUTS[0][2]
        self.refuse(lambda: installer.PinnedFile(source, digest, 1, source=True))

    def test_file_mutation_during_bounded_hash_is_detected(self):
        full = self.source / "large-synthetic-input"
        raw = b"a" * 131072
        full.write_bytes(raw)
        real_read = os.read
        mutated = False
        def change_after_first_read(fd, count):
            nonlocal mutated
            value = real_read(fd, count)
            if not mutated and value:
                mutated = True
                full.write_bytes(b"b" * len(raw))
            return value
        with patch.object(os, "read", change_after_first_read):
            self.refuse(lambda: installer.PinnedFile(full, hashlib.sha256(raw).hexdigest(), len(raw), source=True))
        self.assertTrue(mutated)

    def test_exclusive_write_does_not_replace_existing_input(self):
        full = self.base / "destination"
        full.mkdir(mode=0o700)
        with installer.Directory(full, private=True) as parent:
            installer.write_input(parent, "fixed", b"first")
            self.refuse(lambda: installer.write_input(parent, "fixed", b"second"))
        self.assertEqual((full / "fixed").read_bytes(), b"first")

    def test_write_uses_held_parent_if_namespace_changes_at_open(self):
        full = self.base / "destination"
        full.mkdir(mode=0o700)
        moved = self.base / "old-destination"
        real_open = os.open
        with installer.Directory(full, private=True) as parent:
            def replace_before_open(path, flags, *args, **kwargs):
                if str(path) == "fixed":
                    full.rename(moved)
                    full.mkdir(mode=0o700)
                return real_open(path, flags, *args, **kwargs)
            with patch.object(os, "open", replace_before_open):
                self.refuse(lambda: installer.write_input(parent, "fixed", b"synthetic"))
        self.assertFalse((full / "fixed").exists())
        self.assertTrue((moved / "fixed").exists())

    def test_failed_install_keeps_its_partial_root_and_never_repairs(self):
        real_write = installer.write_input
        calls = 0
        def fail_second(parent, name, raw):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("unit failure")
            return real_write(parent, name, raw)
        with patch.object(installer, "write_input", fail_second):
            self.refuse(lambda: installer.install(RUN_ID))
        self.assertTrue(self.root.exists())
        self.assertTrue((self.root / installer.INPUTS[0][0]).exists())
        self.refuse(lambda: installer.install(RUN_ID))

    def test_installer_exposes_no_arbitrary_source_or_command_argument(self):
        with self.assertRaises(SystemExit):
            installer.main(["install", "--run-id", RUN_ID, "--command", "/bin/sh"])
        self.assertFalse(self.root.exists())
        self.refuse(lambda: installer.os_query("arbitrary"))



class MountMetadataTests(unittest.TestCase):
    def test_mount_metadata_accepts_only_the_build_volume_of_the_t9_container(self):
        class MetadataDirectory:
            def __init__(self, *_args, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
        good_disk = {"MountPoint": str(installer.VOLUME), "VolumeName": installer.VOLUME.name, "FilesystemType": "apfs",
                     "APFSContainerReference": "disk5"}
        good_t9 = {"MountPoint": str(installer.T9), "FilesystemType": "apfs", "APFSContainerReference": "disk5"}
        with patch.object(installer, "Directory", MetadataDirectory), patch.object(Path, "is_mount", return_value=True), patch.object(installer, "os_query", side_effect=[good_disk, good_t9]) as query:
            installer.mounted_volume()
            self.assertEqual([call.args for call in query.call_args_list], [("disk",), ("t9",)])
        for disk, t9 in ((dict(good_disk, FilesystemType="exfat"), good_t9), (dict(good_disk, MountPoint="/other"), good_t9),
                         (dict(good_disk, VolumeName="AxioSozo"), good_t9), (dict(good_disk, APFSContainerReference="disk7"), good_t9),
                         (good_disk, dict(good_t9, FilesystemType="exfat")), (good_disk, dict(good_t9, APFSContainerReference="")),
                         (good_disk, dict(good_t9, MountPoint="/elsewhere")), (good_disk, None)):
            with patch.object(installer, "Directory", MetadataDirectory), patch.object(Path, "is_mount", return_value=True), patch.object(installer, "os_query", side_effect=[disk, t9]):
                with self.assertRaises(RuntimeError):
                    installer.mounted_volume()

if __name__ == "__main__":
    unittest.main(verbosity=2)
