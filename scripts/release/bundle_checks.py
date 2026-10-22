"""Read-only inspection of a macOS .app for the AxioSozo Preview release gate.

Nothing in this module writes, signs, launches or contacts the network. Every
check returns data; the pipeline decides what blocks. A finding is either a
`blocker` (the Preview must not be produced), a `warning` (ship only with an
explicit decision) or `info`.
"""
import configparser
import hashlib
import os
from pathlib import Path
import plistlib
import re
import zipfile

# User-visible marks that must not appear in AxioSozo branding strings.
MARKS = re.compile(r'\b(Firefox|Mozilla|Zen|Chrome|Chromium|Google|Nightly|Twilight)\b')
ID_MARKS = {'mozilla', 'firefox', 'zen', 'chrome', 'chromium', 'google', 'nightly', 'twilight'}
VISIBLE_PLIST_KEYS = ('CFBundleName', 'CFBundleDisplayName', 'CFBundleGetInfoString',
                      'NSHumanReadableCopyright', 'CFBundleSpokenName')
BUNDLE_ID = re.compile(r'^nl\.axiosozo\.[a-z0-9][a-z0-9.-]*$')
DEVELOPER_KEYS = ('MozillaDeveloperObjPath', 'MozillaDeveloperRepoPath')
# Present only in objdir development bundles or test builds, never in `mach package` output.
TEST_ARTIFACTS = ('Contents/MacOS/gtest', 'Contents/MacOS/xpcshell', 'Contents/MacOS/ssltunnel',
                  'Contents/MacOS/http3server', 'Contents/Resources/fix_stacks.py',
                  'Contents/Resources/nsinstall', 'Contents/Resources/BadCertAndPinningServer')
UPDATER_PATHS = ('Contents/MacOS/updater.app',
                 'Contents/MacOS/updater.app/Contents/Frameworks/UpdateSettings.framework',
                 'Contents/Library/LaunchServices/org.mozilla.updater')
SAFE_BROWSING_ABSENT = 'no-google-safebrowsing-api-key'
NOTICE_DIR = 'Contents/Resources/axiosozo-notices'
CEF_EMBED = 'Contents/Helpers/AxioCEFProbe.app'


def finding(level, code, detail, **extra):
    return {'level': level, 'code': code, 'detail': detail, **extra}


def load_plist(path):
    try:
        with Path(path).open('rb') as source:
            return plistlib.load(source)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


class Resources:
    """Reads Gecko resources from an unpacked tree or from omni.ja (packaged builds)."""

    def __init__(self, app):
        self.app = Path(app)
        self.root = self.app / 'Contents/Resources'
        self._jars = {}
        self.unreadable = []

    def _jar(self, base):
        if base not in self._jars:
            path = self.root / base / 'omni.ja'
            jar = None
            if path.is_file():
                try:
                    jar = zipfile.ZipFile(path)
                except (OSError, zipfile.BadZipFile):
                    self.unreadable.append(str(path.relative_to(self.app)))
            self._jars[base] = jar
        return self._jars[base]

    def read(self, base, inner):
        """Return (text, origin) for a GRE ('') or app ('browser') resource."""
        path = self.root / base / inner
        if path.is_file():
            return path.read_text(errors='replace'), str(path.relative_to(self.app))
        jar = self._jar(base)
        if jar is not None:
            try:
                return (jar.read(inner).decode('utf-8', 'replace'),
                        str((self.root / base / 'omni.ja').relative_to(self.app)) + '!' + inner)
            except KeyError:
                pass
        return None, None

    def iter_localization(self, base, locale='en-US'):
        folder = self.root / base / 'localization' / locale
        if folder.is_dir():
            for directory, _dirs, files in os.walk(folder):
                for name in sorted(files):
                    if name.endswith('.ftl') and not name.startswith('._'):
                        path = Path(directory) / name
                        yield path.read_text(errors='replace'), str(path.relative_to(self.app))
        jar = self._jar(base)
        if jar is not None:
            prefix = f'localization/{locale}/'
            for name in sorted(jar.namelist()):
                if name.startswith(prefix) and name.endswith('.ftl'):
                    yield (jar.read(name).decode('utf-8', 'replace'),
                           str((self.root / base / 'omni.ja').relative_to(self.app)) + '!' + name)


def fluent_values(text):
    """Values of Fluent/properties lines; comments and message identifiers are ignored."""
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if '=' in stripped and not line[:1].isspace():
            yield number, stripped.split('=', 1)[1]
        elif line[:1].isspace():
            # Fluent attribute (`.label = x`) or multiline continuation.
            yield number, stripped.split('=', 1)[1] if stripped.startswith('.') and '=' in stripped else stripped


def strings_values(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-16') if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode('utf-8', 'replace')
    for match in re.finditer(r'^\s*"?([A-Za-z0-9_]+)"?\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', text, re.M):
        yield match.group(1), match.group(2)


def scan_marks(value):
    return sorted(set(MARKS.findall(value or '')))


def branding(app, cef_rel=None, repo=None):
    """Branding and bundle identity; blocking only for user-visible branding strings."""
    app = Path(app)
    findings = []
    info = load_plist(app / 'Contents/Info.plist')
    if info is None:
        return {'findings': [finding('blocker', 'INFO_PLIST_UNREADABLE', 'Contents/Info.plist missing or invalid')],
                'bundle_id': None, 'version': None}
    bundle_id = info.get('CFBundleIdentifier', '')
    if not BUNDLE_ID.match(bundle_id):
        findings.append(finding('blocker', 'BUNDLE_ID_NOT_AXIOSOZO', f'CFBundleIdentifier={bundle_id!r}'))
    tokens = set(re.split(r'[.\-_]', bundle_id.lower()))
    if tokens & ID_MARKS:
        findings.append(finding('blocker', 'BUNDLE_ID_MARK', f'CFBundleIdentifier={bundle_id!r} contains {sorted(tokens & ID_MARKS)}'))
    if bundle_id.endswith('.dev'):
        findings.append(finding('warning', 'DEV_BUNDLE_ID',
                                f'{bundle_id} is the development identity; a Preview normally uses its own non-.dev identity'))
    for key, value in sorted(info.items()):
        if key in VISIBLE_PLIST_KEYS or key.endswith('UsageDescription'):
            marks = scan_marks(value if isinstance(value, str) else '')
            if marks:
                findings.append(finding('blocker', 'BRANDING_MARK', f'Info.plist {key}={value!r}',
                                        source='Contents/Info.plist', marks=marks))
            if key in ('CFBundleName', 'CFBundleDisplayName') and isinstance(value, str) and value.endswith(' Dev'):
                findings.append(finding('warning', 'DEV_DISPLAY_NAME', f'Info.plist {key}={value!r}'))
    for key in DEVELOPER_KEYS:
        if key in info:
            findings.append(finding('blocker', 'OBJDIR_DEVELOPMENT_BUNDLE',
                                    f'Info.plist has {key}; distribute `mach package` output, not the objdir .app'))
    privileged = info.get('SMPrivilegedExecutables') or {}
    if privileged:
        findings.append(finding('warning', 'FOREIGN_PRIVILEGED_EXECUTABLES',
                                'Info.plist SMPrivilegedExecutables references ' + ', '.join(sorted(privileged))
                                + ' with a foreign signing requirement; unused while the updater is disabled'))
    for strings in sorted((app / 'Contents/Resources').glob('*.lproj/InfoPlist.strings')):
        for key, value in strings_values(strings):
            marks = scan_marks(value)
            if marks:
                findings.append(finding('blocker', 'BRANDING_MARK', f'{key}={value!r}',
                                        source=str(strings.relative_to(app)), marks=marks))
    resources = Resources(app)
    brand_files = [('browser', 'localization/en-US/branding/brand.ftl'),
                   ('browser', 'chrome/en-US/locale/branding/brand.properties')]
    found_brand = 0
    for base, inner in brand_files:
        text, origin = resources.read(base, inner)
        if text is None:
            continue
        found_brand += 1
        for number, value in fluent_values(text):
            marks = scan_marks(value)
            if marks:
                findings.append(finding('blocker', 'BRANDING_MARK', value.strip(),
                                        source=f'{origin}:{number}', marks=marks))
    if not found_brand:
        findings.append(finding('blocker', 'BRAND_FILES_NOT_FOUND', 'brand.ftl / brand.properties not found'))
    ini_text, ini_origin = resources.read('', 'application.ini')
    profile_root = None
    if ini_text is not None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read_string(ini_text)
            section = parser['App'] if parser.has_section('App') else {}
            profile_root = section.get('Profile') or section.get('Name')
            if not profile_root or 'axiosozo' not in profile_root.lower():
                findings.append(finding('blocker', 'PROFILE_ROOT_NOT_ISOLATED',
                                        f'{ini_origin} Profile={section.get("Profile")!r}: the app would use '
                                        f'~/Library/Application Support/{profile_root} (stock Zen uses "zen"); '
                                        'set MOZ_APP_PROFILE to an AxioSozo-owned name'))
            for key in ('Vendor', 'Name', 'CodeName'):
                marks = scan_marks(section.get(key, ''))
                if marks:
                    findings.append(finding('warning', 'APPLICATION_INI_MARK', f'{key}={section.get(key)!r}',
                                            source=ini_origin, marks=marks))
            if 'zen-browser' in section.get('SourceRepository', ''):
                findings.append(finding('warning', 'SOURCE_REPOSITORY_UPSTREAM',
                                        f'SourceRepository={section.get("SourceRepository")!r}'))
            if parser.has_section('Crash Reporter'):
                crash = parser['Crash Reporter']
                if crash.get('Enabled') == '1' and 'mozilla' in crash.get('ServerURL', ''):
                    findings.append(finding('warning', 'CRASH_REPORTS_TO_MOZILLA',
                                            'crash reporter enabled with a mozilla.com submission URL'))
        except configparser.Error as error:
            findings.append(finding('blocker', 'APPLICATION_INI_INVALID', str(error)))
    else:
        findings.append(finding('blocker', 'APPLICATION_INI_NOT_FOUND', 'Resources/application.ini not found'))
    icon_name = info.get('CFBundleIconFile')
    if icon_name and repo is not None:
        icon = app / 'Contents/Resources' / (icon_name if icon_name.endswith('.icns') else icon_name + '.icns')
        expected = Path(repo) / 'apps/browser/branding/firefox.icns'
        if icon.is_file() and expected.is_file():
            if sha256_path(icon) != sha256_path(expected):
                findings.append(finding('warning', 'APP_ICON_NOT_AXIOSOZO',
                                        f'{icon.name} differs from apps/browser/branding/firefox.icns'))
        else:
            findings.append(finding('warning', 'APP_ICON_UNVERIFIED', f'could not compare {icon_name}'))
    if cef_rel:
        for plist in sorted((app / cef_rel).rglob('Info.plist')):
            data = load_plist(plist) or {}
            for key in VISIBLE_PLIST_KEYS:
                marks = scan_marks(data.get(key, '') if isinstance(data.get(key), str) else '')
                if marks:
                    findings.append(finding('blocker', 'BRANDING_MARK', f'{key}={data.get(key)!r}',
                                            source=str(plist.relative_to(app)), marks=marks))
    # Zen's own UI strings are not branding files; report them for review only.
    hits = []
    for base in ('', 'browser'):
        for text, origin in resources.iter_localization(base):
            for number, value in fluent_values(text):
                marks = scan_marks(value)
                if marks:
                    hits.append({'source': f'{origin}:{number}', 'marks': marks, 'text': value.strip()[:120]})
    if hits:
        counts = {}
        for hit in hits:
            for mark in hit['marks']:
                counts[mark] = counts.get(mark, 0) + 1
        findings.append(finding('info', 'UI_STRING_MARKS',
                                f'{len(hits)} en-US UI strings outside the brand files mention a mark; review',
                                counts=counts, examples=hits[:8]))
    for path in resources.unreadable:
        findings.append(finding('blocker', 'OMNIJA_UNREADABLE', path))
    return {'findings': findings, 'bundle_id': bundle_id,
            'version': info.get('CFBundleShortVersionString'), 'profile_root': profile_root,
            'executable': info.get('CFBundleExecutable')}


def packaging(app):
    """Distributable layout: no symlink may leave the bundle; no test harness binaries."""
    app = Path(app)
    root = app.resolve()
    external = []
    for directory, dirs, files in os.walk(app):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                target = os.readlink(path)
                resolved = (path.parent / target).resolve() if not os.path.isabs(target) else Path(target).resolve()
                if resolved != root and root not in resolved.parents:
                    external.append(str(path.relative_to(app)))
    findings = []
    if external:
        findings.append(finding('blocker', 'SYMLINKS_LEAVE_BUNDLE',
                                f'{len(external)} symlinks point outside the bundle (objdir build?)',
                                examples=external[:10], count=len(external)))
    present = [relative for relative in TEST_ARTIFACTS if (app / relative).exists() or (app / relative).is_symlink()]
    if present:
        findings.append(finding('blocker', 'TEST_ARTIFACTS_PRESENT',
                                'test-harness binaries present; use `mach package` output', paths=present))
    return {'findings': findings, 'external_symlinks': len(external)}


def app_constants(app):
    text, origin = Resources(app).read('', 'modules/AppConstants.sys.mjs')
    if text is None:
        return None, None

    def value(name):
        match = re.search(rf'\b{name}:\s*(?://[^\n]*\n\s*)*("([^"]*)"|true|false)', text)
        if not match:
            return None
        return match.group(2) if match.group(2) is not None else match.group(1) == 'true'
    return {name: value(name) for name in ('MOZ_UPDATER', 'MOZ_UPDATE_CHANNEL', 'MOZ_GOOGLE_SAFEBROWSING_API_KEY',
                                          'MOZ_CRASHREPORTER', 'MOZ_TELEMETRY_REPORTING', 'MOZ_DATA_REPORTING',
                                          'MOZ_NORMANDY')}, origin


def updater(app):
    """Updater must be absent: our own MAR keys and update host do not exist yet."""
    app = Path(app)
    constants, origin = app_constants(app)
    findings = []
    present = [relative for relative in UPDATER_PATHS if (app / relative).exists()]
    if constants is None or constants['MOZ_UPDATER'] is None:
        findings.append(finding('blocker', 'UPDATER_STATE_UNKNOWN', 'AppConstants MOZ_UPDATER could not be read'))
        state = 'UNKNOWN'
    elif constants['MOZ_UPDATER'] or present:
        findings.append(finding('blocker', 'UPDATER_ENABLED',
                                f'MOZ_UPDATER={constants["MOZ_UPDATER"]}; updater paths: {present}'))
        state = 'ENABLED'
    else:
        state = 'DISABLED'
    return {'findings': findings, 'state': state, 'source': origin,
            'channel': (constants or {}).get('MOZ_UPDATE_CHANNEL'),
            'flags': {key: value for key, value in (constants or {}).items() if key != 'MOZ_GOOGLE_SAFEBROWSING_API_KEY'}}


def safe_browsing(app, distribution=False):
    """Presence only; the key value itself is never printed or stored. A build
    without a key may be a local artifact, never a distributed (notarized) one."""
    constants, origin = app_constants(app)
    key = (constants or {}).get('MOZ_GOOGLE_SAFEBROWSING_API_KEY')
    if key is None:
        return {'findings': [finding('blocker', 'SAFE_BROWSING_STATE_UNKNOWN',
                                     'MOZ_GOOGLE_SAFEBROWSING_API_KEY could not be read')],
                'state': 'UNKNOWN', 'source': origin}
    state = 'ABSENT' if key in ('', SAFE_BROWSING_ABSENT) else 'PRESENT'
    findings = []
    if state == 'ABSENT' and distribution:
        findings.append(finding('blocker', 'SAFE_BROWSING_KEY_ABSENT',
                                'distribution build has no Safe Browsing key: it would ship WITHOUT '
                                'phishing/malware protection; build with AxioSozo\'s own key'))
    elif state == 'ABSENT':
        findings.append(finding('warning', 'SAFE_BROWSING_KEY_ABSENT',
                                'build has no Safe Browsing key: ships WITHOUT phishing/malware protection '
                                'and every artifact must say so; a notarized build is refused'))
    return {'findings': findings, 'state': state, 'source': origin}


def chromium(app, repo=None, cef_rel=None):
    """Chromium mode must be off by default; if CEF is embedded it is labelled experimental."""
    app = Path(app)
    info = load_plist(app / 'Contents/Info.plist') or {}
    environment = info.get('LSEnvironment') or {}
    findings = []
    axiosozo = {key: value for key, value in environment.items() if key.startswith('AXIOSOZO_')}
    if environment.get('AXIOSOZO_ENGINE_SWITCHING') == '1':
        findings.append(finding('blocker', 'CHROMIUM_ENABLED_BY_DEFAULT', 'LSEnvironment sets AXIOSOZO_ENGINE_SWITCHING=1'))
    elif axiosozo:
        findings.append(finding('warning', 'AXIOSOZO_LAUNCH_ENVIRONMENT', f'LSEnvironment sets {sorted(axiosozo)}'))
    gate = None
    if repo is not None:
        source = Path(repo) / 'apps/browser/chrome/EngineProbeControls.sys.mjs'
        gate = source.is_file() and 'AXIOSOZO_ENGINE_SWITCHING' in source.read_text(errors='replace')
        if not gate:
            findings.append(finding('warning', 'CHROMIUM_GATE_NOT_FOUND',
                                    'EngineProbeControls env gate not found in source; verify Chromium stays off'))
    embedded = bool(cef_rel) and (app / cef_rel).is_dir()
    enabled = any(item['code'] == 'CHROMIUM_ENABLED_BY_DEFAULT' for item in findings)
    return {'findings': findings, 'default': 'ON' if enabled else 'OFF',
            'source_gate': gate, 'cef_embedded': embedded,
            'label': ('EXPERIMENTAL: embedded, but the current CEF adapter only launches the host from a ./dev '
                      'development session, so Chromium mode is unavailable in this build' if embedded
                      else 'NOT_INCLUDED: Chromium mode is unavailable in this build')}


def notices(app, cef_rel=None):
    """Notices that must ship inside the app (checked on the staged copy)."""
    app = Path(app)
    resources = Resources(app)
    required = {
        f'{NOTICE_DIR}/LICENSE': lambda p: p.read_text(errors='replace').startswith('Mozilla Public License Version 2.0'),
        f'{NOTICE_DIR}/THIRD_PARTY_NOTICES.md': lambda p: p.stat().st_size > 0,
        f'{NOTICE_DIR}/PREVIEW-NOTICE.txt': lambda p: 'Preview' in p.read_text(errors='replace'),
    }
    results = {}
    for relative, valid in required.items():
        path = app / relative
        results[relative] = bool(path.is_file() and not path.is_symlink() and valid(path))
    text, origin = resources.read('', 'chrome/toolkit/content/global/license.html')
    results['about:license (Gecko/Firefox/Zen notices)'] = text is not None and 'Mozilla Public License' in text
    if cef_rel and (app / cef_rel).is_dir():
        for name in ('LICENSE.txt', 'CREDITS.html'):
            path = app / cef_rel / 'Contents/Resources' / name
            results[f'{cef_rel}/Contents/Resources/{name}'] = path.is_file() and path.stat().st_size > 0
    missing = [name for name, ok in results.items() if not ok]
    findings = [finding('blocker', 'NOTICE_MISSING', name) for name in missing]
    return {'findings': findings, 'present': results}


def sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
