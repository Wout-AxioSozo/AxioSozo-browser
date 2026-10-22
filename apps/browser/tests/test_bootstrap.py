"""Small security tests for the source overlay and build launch gate; no GUI claims."""
import importlib.util
import io
import tarfile
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('zen', ROOT / 'scripts/zen.py')
zen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zen)


class BootstrapTests(unittest.TestCase):
    def test_overlapping_import_is_verified_once_and_rejects_later_edits(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-import-test-') as directory:
            build = Path(directory)
            stage = build / 'source'
            engine = stage / 'engine'
            engine.mkdir(parents=True)
            source = stage / 'src'
            source.mkdir()
            env = zen.zen_toolchain.environment()
            for command in [['git', 'init', '-q'], ['git', 'config', 'gc.auto', '0']]:
                subprocess.run(command, cwd=engine, env=env, check=True, capture_output=True)
            target = engine / 'sample.txt'
            target.write_text('alpha\nbeta\ngamma\n')
            subprocess.run(['git', 'add', 'sample.txt'], cwd=engine, env=env, check=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                            '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                            'commit', '-qm', 'synthetic pristine source'], cwd=engine, env=env, check=True)
            for index, (old, new) in enumerate([('beta', 'first'), ('first', 'second')]):
                (source / f'{index}.patch').write_text(
                    'diff --git a/sample.txt b/sample.txt\n--- a/sample.txt\n+++ b/sample.txt\n'
                    f'@@ -1,3 +1,3 @@\n alpha\n-{old}\n+{new}\n gamma\n')
            target.write_text('alpha\nsecond\ngamma\n')
            _, expected, needs_import = zen.zen_import.prepare(stage, build, env)
            self.assertFalse(needs_import)
            self.assertEqual(expected['sample.txt'], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertFalse(zen.zen_import.prepare(stage, build, env)[2])
            target.write_text('alpha\nsecond\nreviewed comment\ngamma\n')
            post = [{'path': 'sample.txt', 'before_sha256': expected['sample.txt'],
                     'after_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}]
            with self.assertRaisesRegex(RuntimeError, 'IMPORTED_SOURCE_CHANGED'):
                zen.zen_import.prepare(stage, build, env)
            self.assertFalse(zen.zen_import.prepare(stage, build, env, post)[2])
            bad_base = [{**post[0], 'before_sha256': '0' * 64}]
            with self.assertRaisesRegex(RuntimeError, 'IMPORTED_SOURCE_CHANGED'):
                zen.zen_import.prepare(stage, build, env, bad_base)
            self.assertEqual(json.loads((build / 'zen-import.json').read_text())['files'], expected)
            target.write_text('user edit must survive\n')
            with self.assertRaisesRegex(RuntimeError, 'IMPORTED_SOURCE_CHANGED'):
                zen.zen_import.prepare(stage, build, env, post)
            self.assertEqual(target.read_text(), 'user edit must survive\n')

    def test_signed_search_dump_restore_accepts_only_pinned_zen_transform(self):
        relative = 'services/settings/dumps/main/search-config-v2.json'
        with tempfile.TemporaryDirectory(prefix='axiosozo-signed-dump-') as directory:
            engine = Path(directory)
            target = engine / relative
            target.parent.mkdir(parents=True)
            pristine = b'{"data":[{"identifier":"fixture"}],"timestamp":1}\n'
            transformed = b'{"data":[],"timestamp":1}'
            target.write_bytes(pristine)
            subprocess.run(['git', 'init', '-q'], cwd=engine, check=True, capture_output=True)
            subprocess.run(['git', 'add', relative], cwd=engine, check=True, capture_output=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                            '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                            'commit', '-qm', 'synthetic signed baseline'], cwd=engine,
                           check=True, capture_output=True)
            record = {'path': relative,
                      'mozilla_sha256': hashlib.sha256(pristine).hexdigest(),
                      'zen_filtered_sha256': hashlib.sha256(transformed).hexdigest()}
            self.assertFalse(zen.zen_import.restore_signed_search_dump(engine, record))
            target.write_bytes(b'user edit must survive')
            with self.assertRaisesRegex(RuntimeError, 'UNREVIEWED_SIGNED_SEARCH_DUMP_CHANGE'):
                zen.zen_import.restore_signed_search_dump(engine, record)
            self.assertEqual(target.read_bytes(), b'user edit must survive')
            target.write_bytes(transformed)
            self.assertTrue(zen.zen_import.restore_signed_search_dump(engine, record))
            self.assertEqual(target.read_bytes(), pristine)
            with self.assertRaisesRegex(RuntimeError, 'SIGNED_SEARCH_DUMP_BASELINE_MISMATCH'):
                zen.zen_import.restore_signed_search_dump(engine,
                    {**record, 'mozilla_sha256': '0' * 64})

    def test_zen_fluent_install_is_complete_and_preserves_unowned_edits(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-locales-') as directory:
            stage = Path(directory)
            source = stage / 'locales/en-US/browser/browser'
            source.mkdir(parents=True)
            (source / 'zen-general.ftl').write_text('zen-general = Fixture\n')
            (source / 'zen-library.ftl').write_text('zen-library = Fixture\n')
            # about:preferences requires this one; missing, the whole page loses its text.
            (source / 'preferences').mkdir()
            (source / 'preferences/zen-preferences.ftl').write_text('zen-preferences = Fixture\n')
            with self.assertRaisesRegex(RuntimeError, 'ZEN_LOCALE_INPUTS_CHANGED'):
                zen.zen_import.install_zen_locales(stage, expected_count=4)
            self.assertEqual(zen.zen_import.install_zen_locales(stage, expected_count=3), 3)
            self.assertTrue((stage / 'engine/browser/locales/en-US/browser/preferences/zen-preferences.ftl').is_file())
            self.assertEqual(zen.zen_import.install_zen_locales(stage, expected_count=3), 0)
            target = stage / 'engine/browser/locales/en-US/browser/zen-library.ftl'
            target.write_text('user edit must survive\n')
            with self.assertRaisesRegex(RuntimeError, 'ZEN_LOCALE_TARGET_MODIFIED'):
                zen.zen_import.install_zen_locales(stage, expected_count=3)
            self.assertEqual(target.read_text(), 'user edit must survive\n')

    def test_zen_child_resources_stay_inside_bundle_without_sandbox_exception(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-child-') as directory:
            stage = Path(directory) / 'stage'
            app = Path(directory) / 'AxioSozo Dev.app'
            links = []
            for relative, source_relative in zen.zen_import.ZEN_CONTENT_RESOURCES.items():
                source = stage / 'src' / source_relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('fixture resource: ' + source_relative)
                target = app / 'Contents/Resources' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source)
                links.append(target)
            self.assertEqual(len(links), 9)
            # Preflight must reject an unexpected target without rewriting any
            # earlier link in the manifest.
            links[-1].unlink()
            links[-1].write_text('unowned edit')
            with self.assertRaisesRegex(RuntimeError, 'ZEN_CONTENT_RESOURCE_MODIFIED'):
                zen.zen_import.materialize_zen_content_resources(stage, app)
            self.assertTrue(links[0].is_symlink())
            self.assertEqual(links[-1].read_text(), 'unowned edit')
            links[-1].unlink()
            last_source = stage / 'src' / list(zen.zen_import.ZEN_CONTENT_RESOURCES.values())[-1]
            links[-1].symlink_to(last_source)
            self.assertEqual(zen.zen_import.materialize_zen_content_resources(stage, app), 9)
            self.assertEqual(zen.zen_import.materialize_zen_content_resources(stage, app), 0)
            self.assertTrue(all(path.is_file() and not path.is_symlink() for path in links))
            self.assertTrue(all(path.read_bytes() == (stage / 'src' / source).read_bytes()
                                for path, source in zip(links, zen.zen_import.ZEN_CONTENT_RESOURCES.values())))

    def test_development_default_browser_prompt_is_off(self):
        defaults = (ROOT / 'apps/browser/chrome/defaults.yaml').read_text()
        self.assertIn('- name: browser.shell.checkDefaultBrowser\n  value: false', defaults)

    def test_build_environment_cannot_inherit_private_fetch_or_shell_hooks(self):
        unsafe = {'TASKCLUSTER_ROOT_URL': 'https://untrusted.invalid',
                  'TASKCLUSTER_ACCESS_TOKEN': 'synthetic-test-secret',
                  'TOOLTOOL_URL': 'https://untrusted.invalid',
                  'BASH_ENV': '/synthetic/hook', 'RUSTC_WRAPPER': '/synthetic/hook',
                  'MOZ_FETCHES_DIR': '/synthetic/artifacts', 'MOZCONFIG': '/synthetic/config'}
        with patch.dict(os.environ, unsafe):
            environment = zen.zen_toolchain.environment()
        for name in unsafe:
            self.assertNotIn(name, environment)
        self.assertEqual(environment['GIT_CONFIG_GLOBAL'], '/dev/null')
        self.assertEqual(environment['npm_config_userconfig'], '/dev/null')
        self.assertEqual(environment['CARGO_HOME'], str(zen.zen_toolchain.TOOLS / 'cargo-home'))
        self.assertTrue(zen.zen_toolchain.TOOLS.is_relative_to(zen.storage.VOLUME))

    def test_overlay_refuses_unknown_local_edit(self):
        record = json.loads((ROOT / 'patches/zen/overlay.json').read_text())[0]
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-security-') as directory:
            root = Path(directory)
            path = root / record['path']
            path.parent.mkdir(parents=True)
            path.write_text('existing work must survive')
            with self.assertRaisesRegex(RuntimeError, 'PATCH_CONFLICT'):
                zen.apply_records(root, [record])
            self.assertEqual(path.read_text(), 'existing work must survive')

    def test_retired_patch_restores_only_its_exact_result(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-retired-') as directory:
            root = Path(directory)
            env = zen.zen_toolchain.environment()
            path = root / 'prefs/sample.yaml'
            path.parent.mkdir()
            path.write_text('value: true\n')
            subprocess.run(['git', 'init', '-q'], cwd=root, env=env, check=True)
            subprocess.run(['git', 'add', '.'], cwd=root, env=env, check=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                            '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                            'commit', '-qm', 'synthetic upstream'], cwd=root, env=env, check=True)
            digest = lambda: hashlib.sha256(path.read_bytes()).hexdigest()
            stock = digest()
            path.write_text('value: false\n')
            record = {'path': 'prefs/sample.yaml', 'stock_sha256': stock, 'patched_sha256': [digest()]}
            with patch.object(zen, 'retired_records', return_value=[record]):
                with patch.object(zen, 'capture', side_effect=['prefs/sample.yaml\0', '']):
                    self.assertEqual(zen.unexpected_source_changes(root), [])
                zen.restore_retired(root)
                self.assertEqual(digest(), stock)
                path.write_text('local work must survive\n')
                zen.restore_retired(root)
                self.assertEqual(path.read_text(), 'local work must survive\n')
                with patch.object(zen, 'capture', side_effect=['prefs/sample.yaml\0', '']):
                    self.assertEqual(zen.unexpected_source_changes(root), ['prefs/sample.yaml'])

    def test_retired_records_leave_no_active_overlay_behind(self):
        active = json.loads((ROOT / 'patches/zen/overlay.json').read_text())
        for item in zen.retired_records():
            self.assertNotIn(item['stock_sha256'], item['patched_sha256'])
            # A superseded patch may only be replaced by a record that starts from
            # the pinned stock file, which restore_retired recreates first.
            chain = [record for record in active if record['path'] == item['path']]
            if chain:
                self.assertEqual(chain[0]['before_sha256'], item['stock_sha256'])
                self.assertFalse({record['after_sha256'] for record in chain} & set(item['patched_sha256']))

    def test_jar_overlay_only_includes_the_generated_manifest(self):
        records = [item for item in zen.overlay_records() if item['path'] == 'src/zen/common/jar.inc.mn']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['replacements'], [[
            '        content/browser/ZenStartup.mjs',
            '#include axiosozo/jar.inc.mn\n\n        content/browser/ZenStartup.mjs']])
        stock = zen.capture(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                             'show', 'HEAD:src/zen/common/jar.inc.mn'], zen.UPSTREAM)
        if stock is None:
            self.skipTest('upstream/zen absent; stock jar.inc.mn hash not re-derived')
        text = stock + '\n'
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), records[0]['before_sha256'])
        after = text.replace(*records[0]['replacements'][0], 1)
        self.assertEqual(hashlib.sha256(after.encode()).hexdigest(), records[0]['after_sha256'])

    def test_every_chrome_file_and_the_contexts_core_are_packaged(self):
        names = {name for name, _ in zen.packaged_files()}
        ignored = set(subprocess.run(['git', '--no-optional-locks', 'ls-files', '--others', '--ignored', '--exclude-standard', '-z',
                                      '--', 'apps/browser/chrome', 'packages/contexts/src'],
                                     cwd=ROOT, capture_output=True, check=True).stdout.decode().split('\0'))
        for path in (ROOT / 'apps/browser/chrome').rglob('*'):
            if path.relative_to(ROOT).as_posix() in ignored:
                continue
            self.assertFalse(path.is_symlink(), path)
            if path.is_file() and path.suffix in zen.PACKAGED_SUFFIXES and not any(
                    part.startswith('.') for part in path.relative_to(ROOT / 'apps/browser/chrome').parts):
                self.assertIn(path.relative_to(ROOT / 'apps/browser/chrome').as_posix(), names)
        for path in (ROOT / 'packages/contexts/src').glob('*.mjs'):
            if not path.name.startswith('.'):
                self.assertIn('contexts/' + path.name, names)
        for required in ['AxioSozoStartup.mjs', 'ZenWorkspaceAdapter.sys.mjs', 'JsonStore.sys.mjs',
                         'AxioSozoServices.sys.mjs', 'ContextMenuContexts.sys.mjs']:
            self.assertIn(required, names)
        manifest = zen.generated_jar_manifest().decode()
        for name in names:
            self.assertIn(f'        content/browser/axiosozo/{name} (../../zen/common/axiosozo/{name})\n', manifest)
        self.assertNotIn('defaults.yaml', manifest)

    def test_source_guard_rejects_unexported_native_file(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-source-') as directory:
            with patch.object(zen, 'capture', side_effect=['src/widget/local.cpp\0', '._sidecar\0']):
                self.assertEqual(zen.unexpected_source_changes(Path(directory)), ['src/widget/local.cpp'])

    def test_managed_mirror_refresh_distinguishes_user_edits(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-source-') as directory:
            root = Path(directory)
            relative = 'src/zen/common/axiosozo/GeckoEngineAdapter.sys.mjs'
            mirror = root / relative
            mirror.parent.mkdir(parents=True)
            mirror.write_text('previous generated module')
            digest = hashlib.sha256(mirror.read_bytes()).hexdigest()
            (root / '.axiosozo-overlay-state.json').write_text(json.dumps({'version': 1, 'files': {relative: digest}}))
            with patch.object(zen, 'capture', side_effect=['', relative + '\0.axiosozo-overlay-state.json\0']):
                self.assertEqual(zen.unexpected_source_changes(root), [])
            mirror.write_text('unexported local user edit')
            with patch.object(zen, 'capture', side_effect=['', relative + '\0.axiosozo-overlay-state.json\0']):
                self.assertEqual(zen.unexpected_source_changes(root), [relative])

    def test_source_guard_catches_ignored_mozconfig(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-zen-source-') as directory:
            root = Path(directory)
            (root / 'mozconfig').write_text('local config must survive')
            with patch.object(zen, 'capture', side_effect=['', '']):
                self.assertTrue(zen.unexpected_source_changes(root)[0].startswith('mozconfig '))
            self.assertEqual((root / 'mozconfig').read_text(), 'local config must survive')

    def test_toolchain_extraction_preserves_unowned_existing_directory(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-toolchain-guard-') as directory:
            root = Path(directory)
            archive = root / 'tool.tar'
            archive.write_bytes(b'fixture archive')
            destination = root / 'tool'
            destination.mkdir()
            existing = destination / 'user-file'
            existing.write_text('preserve me')
            with self.assertRaisesRegex(RuntimeError, 'UNOWNED_TOOLCHAIN_DESTINATION'):
                zen.zen_toolchain.extract(archive, destination)
            self.assertEqual(existing.read_text(), 'preserve me')

    def test_toolchain_pin_update_keeps_the_owned_previous_extraction(self):
        with tempfile.TemporaryDirectory(prefix='axiosozo-toolchain-update-') as directory:
            root = Path(directory)
            archive = root / 'tool.tar'
            with tarfile.open(archive, 'w') as package:
                info = tarfile.TarInfo('tool-2/bin/tool')
                info.size = 3
                package.addfile(info, io.BytesIO(b'new'))
            destination = root / 'tool'
            (destination / 'bin').mkdir(parents=True)
            (destination / 'bin/tool').write_text('old')
            old = 'a' * 64
            (destination / '.extracted').write_text(old + '\n')
            receipt = root / 'tool.extraction.json'
            with patch.object(zen.zen_toolchain, 'run', side_effect=lambda args, cwd=None: subprocess.run(args, check=True)):
                receipt.write_text(json.dumps({'sha256': 'b' * 64, 'destination': str(destination)}) + '\n')
                with self.assertRaisesRegex(RuntimeError, 'EXTRACTED_TOOLCHAIN_PIN_MISMATCH'):
                    zen.zen_toolchain.extract(archive, destination)
                self.assertEqual((destination / 'bin/tool').read_text(), 'old', 'an unowned install is never moved')
                receipt.write_text(json.dumps({'sha256': old, 'destination': str(destination)}) + '\n')
                zen.zen_toolchain.extract(archive, destination)
            kept = root / ('tool-' + old[:12])
            self.assertEqual((kept / 'bin/tool').read_text(), 'old')
            # A second retirement to the same names never replaces the kept copy or its receipt.
            with self.assertRaisesRegex(RuntimeError, 'RETIRED_TOOLCHAIN_EXISTS'):
                zen.zen_toolchain.retire(destination, old)
            self.assertEqual((destination / 'bin/tool').read_text(), 'new')
            self.assertTrue((root / (kept.name + '.extraction.json')).is_file())
            self.assertEqual((destination / 'bin/tool').read_text(), 'new')
            self.assertEqual((destination / '.extracted').read_text().strip(), zen.zen_toolchain.hash_file(archive))

    def test_missing_custom_build_does_not_substitute_installed_browser(self):
        original = zen.BUILD
        try:
            with tempfile.TemporaryDirectory(prefix='axiosozo-zen-gate-') as directory:
                zen.BUILD = Path(directory)
                self.assertEqual(zen.describe()['reason'], 'CUSTOM_ZEN_BUILD_MISSING')
        finally:
            zen.BUILD = original

    def test_changed_chrome_requires_controlled_rebuild(self):
        original = zen.BUILD
        try:
            with tempfile.TemporaryDirectory(prefix='axiosozo-zen-gate-') as directory:
                zen.BUILD = Path(directory)
                (zen.BUILD / 'build-stamp.json').write_text(json.dumps({'fingerprint': 'stale'}))
                with patch.object(zen, 'unexpected_source_changes', return_value=[]):
                    self.assertEqual(zen.describe()['reason'], 'NATIVE_REBUILD_REQUIRED')
        finally:
            zen.BUILD = original


PACKAGING_TMP = ROOT / '.local/tmp/zen-packaging-tests'


class PackagingTests(unittest.TestCase):
    """Generated JAR packaging against a synthetic upstream tree under the
    project's ignored .local/ directory; the real upstream is never touched."""

    def setUp(self):
        PACKAGING_TMP.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='overlay-', dir=PACKAGING_TMP)
        root = Path(self.directory.name)
        self.chrome = root / 'chrome'
        self.contexts = root / 'contexts-src'
        self.native = root / 'native'
        self.upstream = root / 'upstream'
        (self.chrome / 'overview').mkdir(parents=True)
        self.contexts.mkdir()
        (self.chrome / 'defaults.yaml').write_text('- name: axiosozo.contexts.enabled\n  value: true\n')
        (self.chrome / 'AxioSozoStartup.mjs').write_text('export {};\n')
        (self.chrome / 'Feature.sys.mjs').write_text('export const a = 1;\n')
        (self.chrome / 'notes.txt').write_text('not packaged\n')
        (self.chrome / 'overview/about-axiosozo.html').write_text('<!doctype html>\n')
        (self.chrome / 'overview/about-axiosozo-process.js').write_text('"use strict";\n')
        (self.chrome / 'overview/._about-axiosozo.html').write_text('AppleDouble sidecar')
        (self.contexts / 'index.mjs').write_text('export * from "./schema.mjs";\n')
        (self.contexts / 'schema.mjs').write_text('export const v = 1;\n')
        # Packaging lists sources through git (tracked + untracked-but-not-ignored),
        # so the synthetic sources live in their own repository under .local/tmp.
        subprocess.run(['git', 'init', '-q'], cwd=root, env=zen.zen_toolchain.environment(), check=True, capture_output=True)
        jar = self.upstream / 'src/zen/common/jar.inc.mn'
        jar.parent.mkdir(parents=True)
        (self.upstream / 'prefs').mkdir()
        (self.upstream / 'prefs/zen.yaml').write_text('- name: zen\n  value: true\n')
        (self.upstream / 'src/zen/moz.build').write_text('DIRS += [\n    "toolkit",\n    "window-drag",\n]\n')
        stock = '# header\n        content/browser/zen-sets.js (x)\n\n        content/browser/ZenStartup.mjs (y)\n'
        jar.write_text(stock)
        env = zen.zen_toolchain.environment()
        for command in [['git', 'init', '-q'], ['git', 'add', '.'],
                        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                         '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'stock']]:
            subprocess.run(command, cwd=self.upstream, env=env, check=True, capture_output=True)
        old = '        content/browser/ZenStartup.mjs'
        new = '#include axiosozo/jar.inc.mn\n\n' + old
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        self.record = {'path': 'src/zen/common/jar.inc.mn', 'before_sha256': digest(stock),
                       'after_sha256': digest(stock.replace(old, new, 1)), 'replacements': [[old, new]]}
        moz = (self.upstream / 'src/zen/moz.build').read_text()
        moz_old = '    "window-drag",\n]'
        moz_new = moz_old + '\nDIRS += ["axiosozo-native"]'
        self.native_record = {'path': 'src/zen/moz.build', 'when': 'native', 'before_sha256': digest(moz),
                              'after_sha256': digest(moz.replace(moz_old, moz_new, 1)), 'replacements': [[moz_old, moz_new]]}
        self.moz_stock = moz
        self.patches = [patch.object(zen, 'CHROME_SOURCE', self.chrome),
                        patch.object(zen, 'NATIVE_SOURCE', self.native),
                        patch.object(zen, 'CONTEXTS_SOURCE', self.contexts),
                        patch.object(zen, 'overlay_records', return_value=[self.record, self.native_record]),
                        patch.object(zen, 'retired_records', return_value=[])]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def mirror(self, name):
        return self.upstream / 'src/zen/common/axiosozo' / name

    def test_overlay_packages_subdirectories_and_contexts_core(self):
        zen.overlay(self.upstream)
        names = [name for name, _ in zen.packaged_files()]
        self.assertEqual(names, ['AxioSozoStartup.mjs', 'Feature.sys.mjs', 'contexts/index.mjs', 'contexts/schema.mjs',
                                 'overview/about-axiosozo-process.js', 'overview/about-axiosozo.html'])
        for name, source in zen.packaged_files():
            self.assertEqual(self.mirror(name).read_bytes(), source.read_bytes())
        self.assertFalse(self.mirror('notes.txt').exists())
        # exFAT may create its own ._ xattr sidecars; they are never packaged.
        self.assertNotIn('._', self.mirror('jar.inc.mn').read_text())
        jar = (self.upstream / 'src/zen/common/jar.inc.mn').read_text()
        self.assertEqual(jar.count('#include axiosozo/jar.inc.mn'), 1)
        generated = self.mirror('jar.inc.mn').read_text()
        self.assertIn('        content/browser/axiosozo/overview/about-axiosozo.html '
                      '(../../zen/common/axiosozo/overview/about-axiosozo.html)\n', generated)
        self.assertIn('        content/browser/axiosozo/contexts/schema.mjs (../../zen/common/axiosozo/contexts/schema.mjs)\n', generated)
        self.assertEqual((self.upstream / 'prefs/axiosozo.yaml').read_bytes(), (self.chrome / 'defaults.yaml').read_bytes())
        state = json.loads((self.upstream / '.axiosozo-overlay-state.json').read_text())['files']
        self.assertIn('src/zen/common/axiosozo/overview/about-axiosozo.html', state)
        self.assertIn('src/zen/common/axiosozo/jar.inc.mn', state)
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])
        zen.overlay(self.upstream)  # idempotent
        self.assertEqual((self.upstream / 'src/zen/common/jar.inc.mn').read_text(), jar)
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])

    def test_new_file_needs_no_overlay_edit_and_source_refresh_is_accepted(self):
        zen.overlay(self.upstream)
        (self.chrome / 'overview/overview.css').write_text(':root {}\n')
        (self.chrome / 'Feature.sys.mjs').write_text('export const a = 2;\n')
        # Until the next prepare the previous generation is still recognised.
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])
        zen.overlay(self.upstream)
        self.assertIn('overview/overview.css', self.mirror('jar.inc.mn').read_text())
        self.assertEqual(self.mirror('Feature.sys.mjs').read_text(), 'export const a = 2;\n')
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])

    def test_user_edits_in_mirror_subdirectories_are_reported(self):
        zen.overlay(self.upstream)
        self.mirror('overview/about-axiosozo.html').write_text('local edit must survive')
        (self.mirror('overview') / 'extra.html').write_text('unowned')
        self.assertEqual(zen.unexpected_source_changes(self.upstream),
                         ['src/zen/common/axiosozo/overview/about-axiosozo.html',
                          'src/zen/common/axiosozo/overview/extra.html'])
        self.mirror('jar.inc.mn').write_text('        content/browser/axiosozo/evil.mjs (/etc/passwd)\n')
        self.assertIn('src/zen/common/axiosozo/jar.inc.mn', zen.unexpected_source_changes(self.upstream))

    def test_removed_sources_leave_only_unmodified_mirrors_for_cleanup(self):
        zen.overlay(self.upstream)
        (self.chrome / 'overview/about-axiosozo-process.js').unlink()
        (self.chrome / 'overview/about-axiosozo.html').unlink()
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])
        zen.overlay(self.upstream)
        self.assertFalse(self.mirror('overview').exists())
        self.assertNotIn('overview/', self.mirror('jar.inc.mn').read_text())
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])
        (self.contexts / 'schema.mjs').unlink()
        self.mirror('contexts/schema.mjs').write_text('edited after generation')
        self.assertEqual(zen.unexpected_source_changes(self.upstream), ['src/zen/common/axiosozo/contexts/schema.mjs'])
        zen.overlay(self.upstream)
        self.assertEqual(self.mirror('contexts/schema.mjs').read_text(), 'edited after generation')

    def test_invalid_state_and_unpackageable_names_fail_closed(self):
        zen.overlay(self.upstream)
        state_path = self.upstream / '.axiosozo-overlay-state.json'
        state_path.write_text(json.dumps({'version': 1, 'files': {'src/widget/local.cpp': '0' * 64}}))
        self.assertEqual(zen.unexpected_source_changes(self.upstream), ['INVALID_OVERLAY_STATE'])
        with self.assertRaisesRegex(RuntimeError, 'INVALID_OVERLAY_STATE'):
            zen.overlay(self.upstream)
        state_path.unlink()
        (self.chrome / 'bad name.mjs').write_text('')
        with self.assertRaisesRegex(RuntimeError, 'UNPACKAGEABLE_CHROME_FILE'):
            zen.packaged_files()
        self.assertEqual(zen.unexpected_source_changes(self.upstream)[0], 'UNPACKAGEABLE_CHROME_FILE: bad name.mjs')
        (self.chrome / 'bad name.mjs').unlink()
        (self.chrome / 'contexts').mkdir()
        (self.chrome / 'contexts/index.mjs').write_text('')
        with self.assertRaisesRegex(RuntimeError, 'RESERVED_CHROME_PATH'):
            zen.packaged_files()

    def test_ignored_files_are_never_packaged(self):
        root = Path(self.directory.name)
        (root / '.gitignore').write_text('chrome/Local.sys.mjs\nchrome/overview/build/\ncontexts-src/secret.mjs\n')
        (self.chrome / 'Local.sys.mjs').write_text('export const local = "ignored build output";\n')
        (self.chrome / 'overview/build').mkdir()
        (self.chrome / 'overview/build/bundle.js').write_text('ignored\n')
        (self.contexts / 'secret.mjs').write_text('export const s = "ignored";\n')
        names = [name for name, _ in zen.packaged_files()]
        self.assertEqual(names, ['AxioSozoStartup.mjs', 'Feature.sys.mjs', 'contexts/index.mjs', 'contexts/schema.mjs',
                                 'overview/about-axiosozo-process.js', 'overview/about-axiosozo.html'])
        zen.overlay(self.upstream)
        self.assertFalse(self.mirror('Local.sys.mjs').exists())
        self.assertFalse(self.mirror('contexts/secret.mjs').exists())
        self.assertNotIn('Local.sys.mjs', self.mirror('jar.inc.mn').read_text())
        # Tracked files are packaged too, and a tracked file deleted from the worktree is simply absent.
        env = zen.zen_toolchain.environment()
        subprocess.run(['git', 'add', 'chrome', 'contexts-src'], cwd=root, env=env, check=True, capture_output=True)
        (self.chrome / 'Feature.sys.mjs').unlink()
        self.assertNotIn('Feature.sys.mjs', [name for name, _ in zen.packaged_files()])

    def test_symlinked_sources_fail_closed(self):
        outside = Path(self.directory.name) / 'outside'
        outside.mkdir()
        (outside / 'secret.mjs').write_text('export const secret = 1;\n')
        cases = [(self.chrome / 'Linked.sys.mjs', outside / 'secret.mjs'),
                 (self.chrome / 'linked-dir', outside),
                 (self.chrome / 'overview/about-axiosozo.css', outside / 'secret.mjs'),
                 (self.contexts / 'rules.mjs', outside / 'secret.mjs')]
        for link, target in cases:
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'SYMLINKED_CHROME_FILE'):
                zen.packaged_files()
            self.assertEqual(zen.unexpected_source_changes(self.upstream)[0].split(':')[0], 'SYMLINKED_CHROME_FILE')
            link.unlink()
        # A tracked file whose directory was later replaced by a link is refused, not followed.
        env = zen.zen_toolchain.environment()
        subprocess.run(['git', 'add', 'chrome/overview'], cwd=Path(self.directory.name), env=env, check=True, capture_output=True)
        overview = self.chrome / 'overview'
        overview.rename(Path(self.directory.name) / 'moved-overview')
        overview.symlink_to(Path(self.directory.name) / 'moved-overview')
        with self.assertRaisesRegex(RuntimeError, 'SYMLINKED_CHROME_FILE'):
            zen.packaged_files()
        self.assertEqual((outside / 'secret.mjs').read_text(), 'export const secret = 1;\n')

    def write_native(self, files):
        for name, text in files.items():
            (self.native / name).parent.mkdir(parents=True, exist_ok=True)
            (self.native / name).write_text(text)

    def native_mirror(self, name):
        return self.upstream / 'src/zen/axiosozo-native' / name

    def test_native_sources_mirror_with_generated_moz_build_and_conditional_dirs(self):
        moz = self.upstream / 'src/zen/moz.build'
        zen.overlay(self.upstream)  # no native source: nothing mirrored, moz.build untouched
        self.assertEqual(moz.read_text(), self.moz_stock)
        self.assertFalse((self.upstream / 'src/zen/axiosozo-native').exists())
        self.write_native({'zeta/moz.build': 'Library("zeta")\n', 'zeta/Zeta.cpp': 'int z;\n', 'alpha/moz.build': '',
                           'alpha/nsIAlpha.idl': 'interface X {};\n', 'alpha/Alpha.h': '#pragma once\n',
                           'alpha/Alpha.mm': '', 'alpha/notes.txt': 'not native', 'alpha/.hidden.cpp': '',
                           'alpha/deep/Impl.hpp': '', 'orphan/Only.cpp': '', 'Root.h': ''})
        (self.native / 'alpha/._Alpha.mm').write_text('AppleDouble sidecar')
        changed = zen.overlay(self.upstream)
        self.assertEqual(moz.read_text().count('DIRS += ["axiosozo-native"]'), 1)
        generated = self.native_mirror('moz.build').read_text()
        self.assertIn('DIRS += [\n    "alpha",\n    "zeta",\n]\n', generated)  # sorted, orphan has no moz.build
        self.assertNotIn('orphan', generated)
        for name in ['zeta/Zeta.cpp', 'alpha/nsIAlpha.idl', 'alpha/Alpha.h', 'alpha/Alpha.mm', 'alpha/deep/Impl.hpp', 'orphan/Only.cpp', 'Root.h']:
            self.assertEqual(self.native_mirror(name).read_bytes(), (self.native / name).read_bytes())
        for name in ['alpha/notes.txt', 'alpha/.hidden.cpp']:
            self.assertFalse(self.native_mirror(name).exists(), name)
        # exFAT may create its own ._ sidecars next to mirrors; sources are never mirrored from them.
        self.assertNotIn('alpha/._Alpha.mm', [name for name, _ in zen.native_files()])
        self.assertIn('src/zen/axiosozo-native/moz.build', changed)
        state = json.loads((self.upstream / '.axiosozo-overlay-state.json').read_text())['files']
        self.assertIn('src/zen/axiosozo-native/alpha/Alpha.h', state)
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])
        after = moz.read_text()
        self.assertEqual(zen.overlay(self.upstream), [])  # idempotent, nothing rewritten
        self.assertEqual(moz.read_text(), after)
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])

    def test_native_dirs_absent_without_subdirectory_moz_build(self):
        self.write_native({'Lonely.cpp': ''})
        zen.overlay(self.upstream)
        self.assertNotIn('DIRS', self.native_mirror('moz.build').read_text())

    def test_stale_native_files_are_removed_and_dirs_reverted(self):
        moz = self.upstream / 'src/zen/moz.build'
        self.write_native({'alpha/moz.build': '', 'alpha/A.cpp': 'a', 'beta/moz.build': '', 'beta/B.cpp': 'b'})
        zen.overlay(self.upstream)
        (self.native / 'beta/B.cpp').unlink()
        (self.native / 'beta/moz.build').unlink()
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])
        changed = zen.overlay(self.upstream)
        self.assertIn('src/zen/axiosozo-native/beta/B.cpp', changed)
        self.assertFalse(self.native_mirror('beta').exists())
        self.assertNotIn('beta', self.native_mirror('moz.build').read_text())
        # A locally edited stale mirror survives and is reported, like the chrome mirror.
        self.native_mirror('alpha/A.cpp').write_text('edited')
        (self.native / 'alpha/A.cpp').unlink()
        self.assertEqual(zen.unexpected_source_changes(self.upstream), ['src/zen/axiosozo-native/alpha/A.cpp'])
        # Removing every native source removes the mirror and reverts the DIRS line.
        (self.native / 'alpha/moz.build').unlink()
        self.native_mirror('alpha/A.cpp').write_text('a')
        zen.overlay(self.upstream)
        self.assertFalse((self.upstream / 'src/zen/axiosozo-native').exists())
        self.assertEqual(moz.read_text(), self.moz_stock)
        self.assertEqual(zen.unexpected_source_changes(self.upstream), [])

    def test_native_paths_are_validated_and_symlinks_refused(self):
        outside = Path(self.directory.name) / 'outside-native'
        outside.mkdir()
        (outside / 'Secret.cpp').write_text('secret')
        self.write_native({'alpha/moz.build': ''})
        (self.native / 'alpha/bad name.cpp').write_text('')
        with self.assertRaisesRegex(RuntimeError, 'UNPACKAGEABLE_NATIVE_FILE'):
            zen.native_files()
        (self.native / 'alpha/bad name.cpp').unlink()
        (self.native / 'moz.build').write_text('')
        with self.assertRaisesRegex(RuntimeError, 'RESERVED_NATIVE_PATH'):
            zen.native_files()
        (self.native / 'moz.build').unlink()
        for link, target in [(self.native / 'alpha/Linked.cpp', outside / 'Secret.cpp'), (self.native / 'linked-dir', outside)]:
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'SYMLINKED_CHROME_FILE'):
                zen.native_files()
            self.assertEqual(zen.unexpected_source_changes(self.upstream)[0].split(':')[0], 'SYMLINKED_CHROME_FILE')
            link.unlink()
        root = Path(self.directory.name)
        (root / '.gitignore').write_text('native/alpha/Ignored.cpp\n')
        (self.native / 'alpha/Ignored.cpp').write_text('ignored')
        self.assertEqual([name for name, _ in zen.native_files()], ['alpha/moz.build'])
        self.assertEqual((outside / 'Secret.cpp').read_text(), 'secret')
        self.assertTrue(zen.owned_output('src/zen/axiosozo-native/alpha/A.cpp'))
        self.assertFalse(zen.owned_output('src/zen/axiosozo-nativeX/A.cpp'))

    def test_native_changes_alter_the_build_fingerprint_and_digests(self):
        self.write_native({'alpha/moz.build': '', 'alpha/A.cpp': 'a', 'beta/moz.build': '', 'beta/B.cpp': 'b'})
        base, full, digests = zen.fingerprint(native=False), zen.fingerprint(), zen.native_digests()
        self.assertEqual(sorted(digests), ['.', 'alpha', 'beta'])
        self.assertEqual(zen.fingerprint(), full)
        (self.native / 'alpha/A.cpp').write_text('changed')
        self.assertEqual(zen.fingerprint(native=False), base)
        self.assertNotEqual(zen.fingerprint(), full)
        changed = zen.native_digests()
        self.assertEqual([name for name in digests if digests[name] != changed[name]], ['alpha'])
        (self.native / 'alpha/notes.txt').write_text('not native')
        self.assertEqual(zen.native_digests(), changed)

    def test_build_jobs_default_is_memory_bounded_and_overridable(self):
        with patch.dict(os.environ):
            os.environ.pop('AXIOSOZO_BUILD_JOBS', None)
            gib = 2**30
            with patch.object(zen.os, 'cpu_count', return_value=10), \
                 patch.object(zen.os, 'sysconf', side_effect=lambda name: {'SC_PAGE_SIZE': 1, 'SC_PHYS_PAGES': 16 * gib}[name]):
                self.assertEqual(zen.build_jobs(), 6)
            with patch.object(zen.os, 'cpu_count', return_value=4), \
                 patch.object(zen.os, 'sysconf', side_effect=lambda name: {'SC_PAGE_SIZE': 1, 'SC_PHYS_PAGES': 64 * gib}[name]):
                self.assertEqual(zen.build_jobs(), 4)
            os.environ['AXIOSOZO_BUILD_JOBS'] = '3'
            self.assertEqual(zen.build_jobs(), 3)
            for bad in ['0', '-1', 'x', '2 ', '']:
                os.environ['AXIOSOZO_BUILD_JOBS'] = bad
                with self.assertRaisesRegex(RuntimeError, 'INVALID_AXIOSOZO_BUILD_JOBS'):
                    zen.build_jobs()

    def test_native_dirs_overlay_record_matches_pinned_moz_build(self):
        record = [item for item in json.loads((ROOT / 'patches/zen/overlay.json').read_text())
                  if item['path'] == 'src/zen/moz.build']
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]['when'], 'native')
        stock = zen.capture(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                             'show', 'HEAD:src/zen/moz.build'], zen.UPSTREAM)
        if stock is None:
            self.skipTest('upstream/zen absent; stock moz.build hash not re-derived')
        text = stock + '\n'
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), record[0]['before_sha256'])
        after = text.replace(*record[0]['replacements'][0], 1)
        self.assertEqual(hashlib.sha256(after.encode()).hexdigest(), record[0]['after_sha256'])
        self.assertIn('DIRS += ["axiosozo-native"]', after)

    def test_symlinked_mirror_entry_is_refused(self):
        zen.overlay(self.upstream)
        target = self.mirror('Feature.sys.mjs')
        target.unlink()
        outside = Path(self.directory.name) / 'outside.mjs'
        outside.write_text('outside')
        target.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'UNEXPECTED_MIRROR_ENTRY'):
            zen.overlay(self.upstream)
        self.assertEqual(outside.read_text(), 'outside')

    def test_materialization_copies_every_packaged_file_into_the_bundle(self):
        zen.overlay(self.upstream)
        app = Path(self.directory.name) / 'AxioSozo Dev.app'
        bundle = app / 'Contents/Resources/browser/chrome/browser/content/browser/axiosozo'
        links = []
        for name, _ in zen.packaged_files():
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(self.mirror(name))
            links.append(target)
        missing = links.pop()
        missing.unlink()
        with self.assertRaisesRegex(RuntimeError, 'AXIOSOZO_RESOURCE_NOT_PACKAGED'):
            zen.materialize_axiosozo_resources(self.upstream, app)
        self.assertTrue(all(path.is_symlink() for path in links), 'preflight rewrites nothing')
        missing.symlink_to(self.mirror(missing.relative_to(bundle).as_posix()))
        links.append(missing)
        self.assertEqual(zen.materialize_axiosozo_resources(self.upstream, app), len(links))
        self.assertEqual(zen.materialize_axiosozo_resources(self.upstream, app), 0)
        self.assertTrue(all(path.is_file() and not path.is_symlink() for path in links))
        links[0].write_text('modified in bundle')
        with self.assertRaisesRegex(RuntimeError, 'AXIOSOZO_RESOURCE_MODIFIED'):
            zen.materialize_axiosozo_resources(self.upstream, app)
        links[0].unlink()
        links[0].symlink_to(Path(self.directory.name) / 'chrome/Feature.sys.mjs')
        with self.assertRaisesRegex(RuntimeError, 'AXIOSOZO_UNEXPECTED_LINK'):
            zen.materialize_axiosozo_resources(self.upstream, app)


if __name__ == '__main__':
    unittest.main()
