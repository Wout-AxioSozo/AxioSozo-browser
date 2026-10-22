"""F7 Preview release pipeline gates on synthetic bundles.

Fake .app trees, git repositories and stage roots live only under the project's
ignored `.local/release-tests/`, never /tmp or home. No real signing identity,
keychain, network or Apple service is used; Apple tools are replaced by a
recorder except local ditto/xattr on the synthetic staging copy.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
import artifacts  # noqa: E402
import bundle_checks  # noqa: E402
import preview  # noqa: E402
import signing_plan  # noqa: E402
import storage  # noqa: E402

APFS_TEST_ROOT = storage.BUILD_ROOT / 'tmp' / 'release-tests'

MACHO = b'\xcf\xfa\xed\xfe' + b'\x00' * 60
TEST_ROOT = ROOT / '.local' / 'release-tests'
SIGN_ENV = (preview.IDENTITY_ENV, preview.TEAM_ENV, preview.PROFILE_ENV)
IDENTITY = 'Developer ID Application: Example Person (ABCDE12345)'


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def plist(path, **values):
    write(path, plistlib.dumps(values))


def app_bundle(path, executable, identifier, **extra):
    plist(Path(path) / 'Contents/Info.plist', CFBundleExecutable=executable, CFBundleIdentifier=identifier,
          CFBundleName=executable, CFBundlePackageType='APPL', **extra)
    write(Path(path) / 'Contents/MacOS' / executable, MACHO)


def make_app(base, name='AxioSozo.app', packaged=False, **overrides):
    """A synthetic Gecko-shaped .app; `packaged` puts resources into omni.ja like `mach package`."""
    app = Path(base) / name
    info = dict(CFBundleIdentifier='nl.axiosozo.browser', CFBundleName='AxioSozo', CFBundleExecutable='axiosozo',
                CFBundleShortVersionString='0.2', CFBundleIconFile='firefox.icns',
                NSCameraUsageDescription='AxioSozo uses the camera on sites you allow.')
    info.update(overrides.pop('info', {}))
    plist(app / 'Contents/Info.plist', **info)
    write(app / 'Contents/MacOS/axiosozo', MACHO)
    write(app / 'Contents/MacOS/XUL', MACHO)
    write(app / 'Contents/MacOS/libmozglue.dylib', MACHO)
    write(app / 'Contents/MacOS/pingsender', MACHO)
    for helper in ('plugin-container', 'media-plugin-helper', 'gpu-helper'):
        app_bundle(app / f'Contents/MacOS/{helper}.app', helper, 'nl.axiosozo.' + helper)
    write(app / 'Contents/Frameworks/ChannelPrefs.framework/ChannelPrefs', MACHO)
    plist(app / 'Contents/Frameworks/ChannelPrefs.framework/Resources/Info.plist', CFBundleExecutable='ChannelPrefs')
    write(app / 'Contents/Resources/gmp-clearkey/0.1/libclearkey.dylib', MACHO)
    write(app / 'Contents/Resources/firefox.icns', b'axiosozo-icon')
    write(app / 'Contents/Resources/en.lproj/InfoPlist.strings',
          overrides.pop('strings', 'CFBundleName = "AxioSozo";\n').encode('utf-16'))
    write(app / 'Contents/Resources/application.ini',
          overrides.pop('ini', '[App]\nVendor=AxioSozo\nName=AxioSozo\nProfile=AxioSozo\n'))
    gre = {'modules/AppConstants.sys.mjs': overrides.pop('constants', (
               'export var AppConstants = Object.freeze({\n  MOZ_UPDATER: false,\n'
               '  MOZ_UPDATE_CHANNEL: "axiosozo-preview",\n'
               '  MOZ_GOOGLE_SAFEBROWSING_API_KEY: "no-google-safebrowsing-api-key",\n});\n')),
           'chrome/toolkit/content/global/license.html': '<h1>Mozilla Public License 2.0</h1>'}
    browser = {'localization/en-US/branding/brand.ftl': overrides.pop('brand', (
                   '# Firefox and Mozilla must be treated as a brand.\n'
                   '-brand-short-name = AxioSozo\n-brand-full-name = AxioSozo\n')),
               'chrome/en-US/locale/branding/brand.properties': 'brandShortName=AxioSozo\n',
               'localization/en-US/browser/other.ftl': 'import-from = Import from Firefox\n'}
    for folder, files in (('', gre), ('browser', browser)):
        if packaged:
            with zipfile.ZipFile(write(app / 'Contents/Resources' / folder / 'omni.ja', b''), 'w') as jar:
                for inner, text in files.items():
                    jar.writestr(inner, text)
        else:
            for inner, text in files.items():
                write(app / 'Contents/Resources' / folder / inner, text)
    return app


def make_cef(base):
    cef = Path(base) / 'AxioCEFProbe.app'
    app_bundle(cef, 'AxioCEFProbe', 'dev.axiosozo.cef-probe')
    write(cef / 'Contents/Resources/LICENSE.txt', 'CEF BSD license')
    write(cef / 'Contents/Resources/CREDITS.html', 'Chromium credits')
    framework = cef / 'Contents/Frameworks/Chromium Embedded Framework.framework'
    write(framework / 'Versions/A/Chromium Embedded Framework', MACHO)
    plist(framework / 'Versions/A/Resources/Info.plist', CFBundleExecutable='Chromium Embedded Framework')
    write(framework / 'Versions/A/Libraries/libEGL.dylib', MACHO)
    os.symlink('A', framework / 'Versions/Current')
    for entry in ('Chromium Embedded Framework', 'Resources', 'Libraries'):
        os.symlink('Versions/Current/' + entry, framework / entry)
    for role in ('', ' (GPU)', ' (Renderer)', ' (Plugin)', ' (Alerts)'):
        name = 'AxioCEFProbe Helper' + role
        app_bundle(cef / f'Contents/Frameworks/{name}.app', name, 'dev.axiosozo.cef-probe.helper')
    return cef


def git(repo, *args):
    subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Release Test',
                    '-c', 'user.email=release-test@invalid', '-c', 'commit.gpgsign=false', *args],
                   cwd=repo, check=True, capture_output=True, timeout=60)


def make_repo(base):
    repo = Path(base) / 'repo'
    write(repo / 'LICENSE', 'Mozilla Public License Version 2.0\n==================================\n')
    write(repo / 'THIRD_PARTY_NOTICES.md', '# Third-party notices\n')
    write(repo / 'apps/browser/chrome/EngineProbeControls.sys.mjs',
          'if (env.get("AXIOSOZO_ENGINE_SWITCHING") !== "1") return null;\n')
    write(repo / 'apps/browser/branding/firefox.icns', b'axiosozo-icon')
    git(repo, 'init', '-q')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'synthetic release fixture')
    return repo


class Recorder:
    """Replaces preview.RUNNER. Local ditto/xattr run for real; Apple tools are simulated."""

    def __init__(self, team='ABCDE12345', allow_apple=False):
        self.calls, self.team, self.allow_apple = [], team, allow_apple

    def __call__(self, argv, capture=False):
        argv = list(map(str, argv))
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool in ('ditto', 'xattr'):
            return subprocess.run(argv, capture_output=True, timeout=120).returncode, ''
        if not self.allow_apple:
            raise AssertionError('unexpected tool invocation: ' + ' '.join(argv))
        if tool == 'codesign' and '--display' in argv and '--entitlements' in argv:
            return 0, '<key>com.apple.security.cs.allow-jit</key><true/>'
        if tool == 'codesign' and '--display' in argv:
            return 0, f'flags=0x10000(runtime)\nTeamIdentifier={self.team}\n'
        if tool == 'hdiutil' and argv[1] == 'create':
            write(argv[-1], b'synthetic dmg bytes')
        return 0, ''

    def tools(self):
        return [Path(call[0]).name + (' ' + call[1] if Path(call[0]).name == 'xcrun' else '') for call in self.calls]


class ReleaseTestCase(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix='case-', dir=TEST_ROOT))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.stage_root = self.base / 'stage'
        self.stage_root.mkdir()
        environment = {key: value for key, value in os.environ.items() if key not in SIGN_ENV}
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apfs(self):
        """Staging needs APFS (the project exFAT folder creates AppleDouble files that
        codesign rejects), so staged fixtures use a task directory on the project volume."""
        if not storage.mounted():
            self.skipTest(f'{storage.BUILD_ROOT} (project APFS volume) is not mounted')
        APFS_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        base = Path(tempfile.mkdtemp(prefix='case-', dir=APFS_TEST_ROOT))
        self.addCleanup(shutil.rmtree, base, True)
        self.stage_root = base / 'stage'
        self.stage_root.mkdir()
        return base

    def run_main(self, argv, repo, runner=None):
        output = io.StringIO()
        with patch.object(preview, 'RUNNER', runner or Recorder()), contextlib.redirect_stdout(output):
            code = preview.main(argv, repo=repo)
        return code, output.getvalue()

    def stage_dirs(self):
        return [path for path in self.stage_root.iterdir() if not path.name.startswith('._')]


class SourceGate(ReleaseTestCase):
    def test_untracked_file_refuses_before_any_write(self):
        repo, app = make_repo(self.base), make_app(self.base / 'build')
        write(repo / 'stray.txt', 'untracked')
        code, output = self.run_main(['--app', str(app), '--cef-app', 'none', '--stage-root', str(self.stage_root)], repo)
        self.assertEqual(code, preview.EXIT_SOURCE_REFUSED)
        self.assertIn('DIRTY_TREE', output)
        self.assertEqual(self.stage_dirs(), [])

    def test_modified_tracked_file_refuses(self):
        repo, app = make_repo(self.base), make_app(self.base / 'build')
        write(repo / 'THIRD_PARTY_NOTICES.md', 'changed\n')
        code, _ = self.run_main(['--app', str(app), '--cef-app', 'none', '--stage-root', str(self.stage_root)], repo)
        self.assertEqual(code, preview.EXIT_SOURCE_REFUSED)
        self.assertEqual(self.stage_dirs(), [])

    def test_commit_mismatch_refuses(self):
        repo, app = make_repo(self.base), make_app(self.base / 'build')
        code, output = self.run_main(['--app', str(app), '--cef-app', 'none', '--stage-root', str(self.stage_root),
                                      '--expect-commit', '0' * 40], repo)
        self.assertEqual(code, preview.EXIT_SOURCE_REFUSED)
        self.assertIn('COMMIT_MISMATCH', output)


class SigningIdentity(ReleaseTestCase):
    def test_missing_identity_blocks_after_identity_free_steps(self):
        apfs = self.apfs()
        repo, app, cef = make_repo(self.base), make_app(apfs / 'build'), make_cef(apfs / 'build')
        recorder = Recorder()
        code, output = self.run_main(['--app', str(app), '--cef-app', str(cef), '--stage-root', str(self.stage_root)],
                                     repo, recorder)
        self.assertEqual(code, preview.EXIT_BLOCKED_SIGNING_IDENTITY)
        self.assertIn('RESULT: BLOCKED_SIGNING_IDENTITY exit=10', output)
        self.assertEqual(set(recorder.tools()), {'ditto', 'xattr'})
        [stage] = self.stage_dirs()
        staged = stage / 'dmg-root/AxioSozo.app'
        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'PREVIEW-NOTICE.txt'):
            self.assertTrue((staged / bundle_checks.NOTICE_DIR / name).is_file(), name)
        self.assertTrue((staged / bundle_checks.CEF_EMBED / 'Contents/Resources/CREDITS.html').is_file())
        self.assertTrue((stage / 'entitlements/browser.plist').is_file())
        notes = (stage / 'RELEASE_NOTES.md').read_text()
        self.assertIn('Preview', notes)
        self.assertIn('WITHOUT phishing and malware protection', notes)
        self.assertIn('Widevine', notes)
        self.assertNotIn('READY.', notes.replace('Not READY.', ''))
        report = json.loads((stage / 'release-report.json').read_text())
        self.assertEqual(report['status'], 'BLOCKED_SIGNING_IDENTITY')
        self.assertFalse(report['ready'])
        self.assertEqual(list(stage.glob('*.dmg')), [])

    def test_ad_hoc_identity_is_not_accepted(self):
        apfs = self.apfs()
        repo, app = make_repo(self.base), make_app(apfs / 'build')
        code, output = self.run_main(['--app', str(app), '--cef-app', 'none', '--stage-root', str(self.stage_root),
                                      '--identity', '-', '--team-id', 'ABCDE12345'], repo)
        self.assertEqual(code, preview.EXIT_BLOCKED_SIGNING_IDENTITY)
        self.assertIn('SIGNING_IDENTITY_INVALID', output)

    def test_notarize_requires_explicit_authorization(self):
        repo, app = make_repo(self.base), make_app(self.base / 'build')
        code, output = self.run_main(['--dry-run', '--app', str(app), '--cef-app', 'none', '--stage-root',
                                      str(self.stage_root), '--notarize'], repo)
        self.assertIn('NOTARIZATION_NOT_AUTHORIZED', output)
        self.assertNotIn('notarytool', output)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            preview.main(['--authorized'], repo=repo)
        self.assertEqual(raised.exception.code, 2)

    def test_identity_path_signs_inside_out_without_contacting_apple(self):
        apfs = self.apfs()
        repo, app, cef = make_repo(self.base), make_app(apfs / 'build'), make_cef(apfs / 'build')
        recorder = Recorder(allow_apple=True)
        with patch.dict(os.environ, {'TMPDIR': str(apfs) + '/'}):
            code, output = self.run_main(['--app', str(app), '--cef-app', str(cef), '--stage-root',
                                          str(self.stage_root), '--identity', IDENTITY, '--team-id', 'ABCDE12345'],
                                         repo, recorder)
        self.assertEqual(code, preview.EXIT_NOT_NOTARIZED, output)
        tools = recorder.tools()
        self.assertFalse(any(tool in ('xcrun notarytool', 'xcrun stapler', 'spctl') for tool in tools))
        signs = [call for call in recorder.calls if Path(call[0]).name == 'codesign' and '--sign' in call]
        self.assertTrue(all('--timestamp=none' in call for call in signs))
        self.assertTrue(all('--options' in call for call in signs[:-1]))  # the last one is the DMG
        [stage] = self.stage_dirs()
        [dmg] = stage.glob('*.dmg')
        sums = (stage / 'SHA256SUMS.txt').read_text()
        self.assertEqual(sums, f'{hashlib.sha256(dmg.read_bytes()).hexdigest()}  {dmg.name}\n')
        self.assertIn('NOT notarized', (stage / 'RELEASE_NOTES.md').read_text())


class DryRunPlan(ReleaseTestCase):
    def test_plan_is_inside_out_with_helpers_before_main_app(self):
        app, cef = make_app(self.base / 'build'), make_cef(self.base / 'build')
        steps = signing_plan.plan(app, embeds={bundle_checks.CEF_EMBED: cef})
        order = [step['relative'] for step in steps]
        self.assertEqual(order[-1], '.')
        for index, earlier in enumerate(order):
            for later in order[index + 1:]:
                self.assertFalse(earlier == '.' or Path(earlier) in Path(later).parents,
                                 f'container {earlier} would be signed before its content {later}')
        helpers = [i for i, rel in enumerate(order) if ' Helper' in rel and rel.endswith('.app')]
        self.assertEqual(len(helpers), 5)
        self.assertLess(max(helpers), order.index(bundle_checks.CEF_EMBED))
        self.assertLess(order.index(bundle_checks.CEF_EMBED), order.index('.'))
        framework = bundle_checks.CEF_EMBED + '/Contents/Frameworks/Chromium Embedded Framework.framework'
        self.assertLess(order.index(framework + '/Versions/A/Libraries/libEGL.dylib'), order.index(framework))
        self.assertNotIn('Contents/MacOS/axiosozo', order)  # sealed with the root bundle
        self.assertFalse(any(rel.endswith('framework/Chromium Embedded Framework') for rel in order))
        entitlements = {step['relative']: step['entitlements'] for step in steps}
        self.assertEqual(entitlements['.'], 'browser.plist')
        self.assertEqual(entitlements['Contents/MacOS/plugin-container.app'], 'plugin-container.plist')
        self.assertEqual(entitlements['Contents/MacOS/gpu-helper.app'], None)
        self.assertEqual(entitlements[bundle_checks.CEF_EMBED + '/Contents/Frameworks/AxioCEFProbe Helper (Renderer).app'],
                         'cef-helper-jit.plist')
        self.assertTrue(all(step['command'][4:6] == ['--options', 'runtime'] for step in steps))

    def test_dry_run_prints_every_codesign_and_modifies_nothing(self):
        apfs = self.apfs()
        repo, app, cef = make_repo(self.base), make_app(apfs / 'build'), make_cef(apfs / 'build')
        before = sorted(str(path) for path in app.rglob('*'))
        recorder = Recorder()
        code, output = self.run_main(['--dry-run', '--app', str(app), '--cef-app', str(cef),
                                      '--stage-root', str(self.stage_root)], repo, recorder)
        self.assertEqual(code, preview.EXIT_BLOCKED_SIGNING_IDENTITY)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(before, sorted(str(path) for path in app.rglob('*')))
        codesign_lines = [line for line in output.splitlines() if '[codesign;' in line]
        self.assertEqual(len(codesign_lines), len(signing_plan.plan(app, embeds={bundle_checks.CEF_EMBED: cef})))
        self.assertTrue(codesign_lines[-1].rstrip("'").endswith('dmg-root/AxioSozo.app'))
        self.assertIn('entitlements/browser.plist', codesign_lines[-1])
        self.assertIn('${AXIOSOZO_SIGN_IDENTITY}', codesign_lines[0])
        self.assertIn('# AxioSozo 0.2 Preview', output)


class BundleChecks(ReleaseTestCase):
    def test_notice_presence(self):
        app, cef = make_app(self.base / 'build'), make_cef(self.base / 'build')
        shutil.copytree(cef, app / bundle_checks.CEF_EMBED, symlinks=True)
        result = bundle_checks.notices(app, bundle_checks.CEF_EMBED)
        self.assertEqual(sorted(item['detail'] for item in result['findings']),
                         sorted(f'{bundle_checks.NOTICE_DIR}/{name}'
                                for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'PREVIEW-NOTICE.txt')))
        write(app / bundle_checks.NOTICE_DIR / 'LICENSE', 'Mozilla Public License Version 2.0\n')
        write(app / bundle_checks.NOTICE_DIR / 'THIRD_PARTY_NOTICES.md', 'notices')
        write(app / bundle_checks.NOTICE_DIR / 'PREVIEW-NOTICE.txt', 'AxioSozo 0.2 - Preview')
        (app / bundle_checks.CEF_EMBED / 'Contents/Resources/CREDITS.html').unlink()
        result = bundle_checks.notices(app, bundle_checks.CEF_EMBED)
        self.assertEqual([item['detail'] for item in result['findings']],
                         [bundle_checks.CEF_EMBED + '/Contents/Resources/CREDITS.html'])

    def test_branding_scan_clean_bundle(self):
        app = make_app(self.base / 'build')
        result = bundle_checks.branding(app, repo=make_repo(self.base))
        self.assertEqual([item for item in result['findings'] if item['level'] != 'info'], [])
        [ui] = [item for item in result['findings'] if item['code'] == 'UI_STRING_MARKS']
        self.assertEqual(ui['counts'], {'Firefox': 1})

    def test_branding_scan_reports_marks(self):
        app = make_app(self.base / 'build',
                       info={'CFBundleIdentifier': 'org.mozilla.firefox', 'CFBundleName': 'Firefox Nightly',
                             'MozillaDeveloperObjPath': '/obj'},
                       strings='CFBundleName = "Zen Browser";\n',
                       brand='# Firefox comment is ignored\n-brand-short-name = Zen\n-brand-full-name = AxioSozo\n',
                       ini='[App]\nVendor=Mozilla\nName=AxioSozo\nProfile=zen\n')
        result = bundle_checks.branding(app)
        codes = sorted(item['code'] for item in result['findings'] if item['level'] == 'blocker')
        self.assertEqual(codes, ['BRANDING_MARK', 'BRANDING_MARK', 'BRANDING_MARK', 'BUNDLE_ID_MARK',
                                 'BUNDLE_ID_NOT_AXIOSOZO', 'OBJDIR_DEVELOPMENT_BUNDLE', 'PROFILE_ROOT_NOT_ISOLATED'])
        sources = [item.get('source', '') for item in result['findings']]
        self.assertIn('Contents/Resources/browser/localization/en-US/branding/brand.ftl:2', sources)
        self.assertNotIn('Contents/Resources/browser/localization/en-US/branding/brand.ftl:1', sources)
        self.assertIn('APPLICATION_INI_MARK', [item['code'] for item in result['findings']])

    def test_packaged_omni_ja_is_read(self):
        app = make_app(self.base / 'build', packaged=True, constants=(
            'MOZ_UPDATER: false,\nMOZ_GOOGLE_SAFEBROWSING_API_KEY: "SYNTHETIC-KEY-VALUE",\n'))
        self.assertEqual(bundle_checks.updater(app)['state'], 'DISABLED')
        safe = bundle_checks.safe_browsing(app)
        self.assertEqual(safe['state'], 'PRESENT')
        self.assertNotIn('SYNTHETIC-KEY-VALUE', json.dumps(safe))
        self.assertTrue(bundle_checks.notices(app)['present']['about:license (Gecko/Firefox/Zen notices)'])
        self.assertEqual([item for item in bundle_checks.branding(app)['findings'] if item['level'] == 'blocker'], [])

    def test_safe_browsing_absent_is_labelled(self):
        app = make_app(self.base / 'build')
        safe = bundle_checks.safe_browsing(app)
        self.assertEqual(safe['state'], 'ABSENT')
        self.assertIn('WITHOUT phishing and malware protection', artifacts.safe_browsing_sentence(safe['state']))
        self.assertEqual([item['level'] for item in safe['findings']], ['warning'])

    def test_safe_browsing_absent_blocks_distribution(self):
        app = make_app(self.base / 'build')
        safe = bundle_checks.safe_browsing(app, distribution=True)
        self.assertEqual([(item['level'], item['code']) for item in safe['findings']],
                         [('blocker', 'SAFE_BROWSING_KEY_ABSENT')])
        present = make_app(self.base / 'keyed', constants=(
            'MOZ_UPDATER: false,\nMOZ_GOOGLE_SAFEBROWSING_API_KEY: "SYNTHETIC-KEY-VALUE",\n'))
        self.assertEqual(bundle_checks.safe_browsing(present, distribution=True)['findings'], [])
        repo = make_repo(self.base)
        args = ['--dry-run', '--app', str(app), '--cef-app', 'none', '--stage-root', str(self.stage_root),
                '--identity', IDENTITY, '--team-id', 'ABCDE12345']
        _, local = self.run_main(args, repo)
        self.assertIn('WARNING SAFE_BROWSING_KEY_ABSENT', local)
        self.assertNotIn('BLOCKER [preflight] SAFE_BROWSING_KEY_ABSENT', local)
        _, notarized = self.run_main(args + ['--notarize', '--authorized'], repo)
        self.assertIn('BLOCKER [preflight] SAFE_BROWSING_KEY_ABSENT', notarized)

    def test_updater_enabled_blocks(self):
        app = make_app(self.base / 'build', constants='MOZ_UPDATER: true,\n')
        self.assertEqual(bundle_checks.updater(app)['findings'][0]['code'], 'UPDATER_ENABLED')

    def test_chromium_enabled_by_launch_environment_blocks(self):
        app = make_app(self.base / 'build', info={'LSEnvironment': {'AXIOSOZO_ENGINE_SWITCHING': '1'}})
        result = bundle_checks.chromium(app)
        self.assertEqual(result['default'], 'ON')
        self.assertEqual(result['findings'][0]['code'], 'CHROMIUM_ENABLED_BY_DEFAULT')

    def test_symlink_leaving_bundle_blocks(self):
        app = make_app(self.base / 'build')
        os.symlink(str(self.base / 'repo-outside'), app / 'Contents/MacOS/xpcshell')
        codes = [item['code'] for item in bundle_checks.packaging(app)['findings']]
        self.assertEqual(codes, ['SYMLINKS_LEAVE_BUNDLE', 'TEST_ARTIFACTS_PRESENT'])

    def test_exfat_stage_root_is_refused(self):
        repo, app = make_repo(self.base), make_app(self.base / 'build')
        if preview.filesystem_type(self.stage_root) in ('apfs', 'hfs'):
            self.skipTest('project folder is not on exFAT here')
        code, output = self.run_main(['--app', str(app), '--cef-app', 'none', '--stage-root', str(self.stage_root)], repo)
        self.assertEqual(code, preview.EXIT_BLOCKED_ENV)
        self.assertIn('STAGE_FILESYSTEM_UNSUPPORTED', output)
        self.assertEqual(self.stage_dirs(), [])

    def test_stage_root_must_be_external(self):
        self.assertIsNotNone(preview.stage_root_problem('/tmp/release'))
        self.assertIsNotNone(preview.stage_root_problem(Path.home() / 'release'))
        self.assertIsNone(preview.stage_root_problem(self.stage_root))


class Checksums(ReleaseTestCase):
    def test_sha256sums_format(self):
        first, second = write(self.base / 'b.dmg', b'second'), write(self.base / 'a.dmg', b'first')
        sums = artifacts.write_checksums([first, second], self.base / 'SHA256SUMS.txt')
        expected = {'a.dmg': hashlib.sha256(b'first').hexdigest(), 'b.dmg': hashlib.sha256(b'second').hexdigest()}
        self.assertEqual(sums, expected)
        self.assertEqual((self.base / 'SHA256SUMS.txt').read_text(),
                         f'{expected["a.dmg"]}  a.dmg\n{expected["b.dmg"]}  b.dmg\n')
        check = subprocess.run(['/usr/bin/shasum', '-a', '256', '-c', 'SHA256SUMS.txt'], cwd=self.base,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == '__main__':
    unittest.main()
