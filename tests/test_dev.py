"""Entrypoint failure gates; these tests do not launch or simulate a browser."""
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dev
import browser_probe


class EntrypointGates(unittest.TestCase):
    def test_component_environment_does_not_inherit_daily_engine_switch(self):
        with patch.dict(os.environ, {"AXIOSOZO_ENGINE_SWITCHING": "1"}), \
             patch.object(dev, "provider_node", return_value="/invented/pinned/node"):
            self.assertEqual(dev.browser_environment()["AXIOSOZO_ENGINE_SWITCHING"], "0")

    def test_browser_uses_volume_root_cef_from_any_build_root(self):
        with patch.object(dev, "provider_node", return_value="/invented/pinned/node"):
            environment = dev.browser_environment()
        self.assertEqual(environment["AXIOSOZO_CEF_ROOT"], str(dev.storage.VOLUME))
        self.assertEqual(environment["AXIOSOZO_CEF_BINARY"],
                         str(dev.storage.VOLUME / "cef/AxioCEFProbe.app/Contents/MacOS/AxioCEFProbe"))

    def test_provider_node_never_falls_back_to_global_node(self):
        with tempfile.TemporaryDirectory(prefix="axiosozo-node-test-") as temporary:
            volume, bin_dir = Path(temporary) / "volume", Path(temporary) / "bin"
            bin_dir.mkdir()
            (bin_dir / "node").write_text("#!/bin/sh\n")
            (bin_dir / "node").chmod(0o700)
            pinned = volume / "toolchains/zen/node/bin/node"
            with patch.object(dev.storage, "VOLUME", volume), patch.dict(os.environ, {"PATH": str(bin_dir)}):
                with self.assertRaisesRegex(RuntimeError, r"pinned Node .* missing; run ./dev setup"):
                    dev.provider_node()
                pinned.parent.mkdir(parents=True)
                pinned.write_text("")
                with self.assertRaisesRegex(RuntimeError, r"not the current pin"):
                    dev.provider_node()
                pin = json.loads((dev.ROOT / "patches/zen/toolchains.json").read_text())["node"]["sha256"]
                (pinned.parents[1] / ".extracted").write_text("0" * 64 + "\n")
                with self.assertRaisesRegex(RuntimeError, r"not the current pin"):
                    dev.provider_node()
                (pinned.parents[1] / ".extracted").write_text(pin + "\n")
                self.assertEqual(dev.provider_node(), str(pinned))

    def test_missing_pinned_node_blocks_provider_without_running_it(self):
        with patch.object(dev, "provider_node", side_effect=RuntimeError("pinned Node missing")), \
             patch.object(dev, "run") as run, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(dev.component(dev.PROVIDER, "check"), 2)
        run.assert_not_called()
        self.assertIn("BLOCKED_ENV: pinned Node missing", output.getvalue())

    def test_missing_pinned_node_starts_no_daily_session(self):
        with patch.object(dev, "provider_node", side_effect=RuntimeError("pinned Node missing")), \
             patch.object(dev.storage, "ensure"), patch.object(dev, "core_ready", return_value=True), \
             patch.object(dev, "component", return_value=0), patch.object(dev, "OwnedSession") as session, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(dev.daily("development"), 2)
        session.assert_not_called()

    def test_commands_never_inherit_build_hooks_but_keep_proxy_and_ca(self):
        process = Mock(returncode=0, pid=1)
        process.wait.return_value = 0
        inherited = {"NODE_OPTIONS": "--require=/invented/hook.js", "RUSTC_WRAPPER": "/invented/hook",
                     "DYLD_INSERT_LIBRARIES": "/invented/hook.dylib", "npm_config_registry": "https://invented.invalid",
                     "HTTPS_PROXY": "http://proxy.invalid:3128", "NODE_EXTRA_CA_CERTS": "/invented/ca.pem"}
        with patch.dict(os.environ, inherited), patch.object(dev.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(dev.run(["invented"]), 0)
        env = popen.call_args.kwargs["env"]
        for name in ("NODE_OPTIONS", "RUSTC_WRAPPER", "DYLD_INSERT_LIBRARIES", "npm_config_registry"):
            self.assertNotIn(name, env)
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.invalid:3128")
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], "/invented/ca.pem")
        with patch.dict(os.environ, {"NODE_TLS_REJECT_UNAUTHORIZED": "0"}), \
             self.assertRaisesRegex(RuntimeError, "UNSAFE_BUILD_ENVIRONMENT"):
            dev.run(["invented"])

    def test_compiler_change_invalidates_coordinator_build(self):
        with patch.object(dev, "rustc_version", return_value=b"rustc 1.99.1\n"):
            before = dev.core_fingerprint()
        with patch.object(dev, "rustc_version", return_value=b"rustc 1.100.0\n"):
            self.assertNotEqual(dev.core_fingerprint(), before)

    def test_failed_build_does_not_test_stale_binary(self):
        with patch.object(dev, "run", return_value=0) as run, \
             patch.object(dev, "build_core", return_value=1), \
             patch.object(dev, "core_ready", return_value=True), \
             patch.object(dev, "component", return_value=0):
            self.assertEqual(dev.test(), 1)
        commands = [list(map(str, call.args[0])) for call in run.call_args_list]
        self.assertFalse(any("unittest" in command for command in commands))
        self.assertFalse(any(any("coordinator.test.mjs" in arg for arg in command) for command in commands))

    def test_failed_e0_never_launches_zen_inspection(self):
        with patch.object(dev, "component", return_value=2) as component, \
             patch.object(dev, "core_ready") as ready, \
             patch.object(dev.storage, "ensure") as ensure:
            self.assertEqual(dev.browser_probe("engine-probe"), 2)
        component.assert_called_once_with(dev.CEF, "run")
        ready.assert_not_called()
        ensure.assert_not_called()

    def test_web_probe_also_requires_actual_native_e0(self):
        with patch.object(dev, "component", return_value=2) as component, \
             patch.object(dev, "core_ready") as ready:
            self.assertEqual(dev.browser_probe("web-probe"), 2)
        component.assert_called_once_with(dev.CEF, "run")
        ready.assert_not_called()

    def test_missing_custom_zen_never_creates_probe_profile(self):
        with patch.object(dev, "component", side_effect=[0, 20]) as component, \
             patch.object(dev, "core_ready", return_value=True), \
             patch.object(dev.storage, "ensure") as ensure:
            self.assertEqual(dev.browser_probe("engine-probe"), 2)
        self.assertEqual(component.call_args_list[-1].args, (dev.ZEN, "ready"))
        ensure.assert_not_called()

    def test_fixture_session_name_matches_native_profile_guard(self):
        with tempfile.TemporaryDirectory(prefix="axiosozo-probe-test-") as temporary:
            root = Path(temporary)
            (root / "docs/evidence").mkdir(parents=True)
            evidence = browser_probe.new_evidence_directory(root, "smoke")
            self.assertRegex(evidence.name, re.compile(r"^smoke-[a-f0-9]{16}$"))
            self.assertTrue(evidence.is_dir())

    def test_cef_profile_audit_ignores_links_and_unrelated_directories(self):
        with tempfile.TemporaryDirectory(prefix="axiosozo-profile-audit-") as temporary:
            root = Path(temporary)
            owned = root / "cef-01234567-89ab-cdef-0123-456789abcdef"
            owned.mkdir()
            outside = root / "outside"
            outside.mkdir()
            os.symlink(outside, root / "cef-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            (root / "cef-invalid").mkdir()
            self.assertEqual(browser_probe.cef_profile_names(root), {owned.name})
            os.symlink(root, root / "linked-root")
            self.assertEqual(browser_probe.cef_profile_names(root / "linked-root"), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
