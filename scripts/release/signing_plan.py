"""Inside-out Developer ID signing plan for every nested Mach-O in an .app.

Entitlement mapping mirrors the pinned Firefox 158.0.1 source (unchanged since 156.0):
`taskcluster/config.yml` mac-signing > hardened-sign-config > production, with the
entitlement files from `security/mac/hardenedruntime/production/`. Every item is
signed with the hardened runtime (`--options runtime`) as Firefox does.
"""
from fnmatch import fnmatch
import os
from pathlib import Path
import plistlib

ENTITLEMENTS_DIR = Path(__file__).resolve().parent / 'entitlements'
BUNDLE_SUFFIXES = ('.app', '.framework', '.appex', '.xpc', '.bundle', '.plugin')
THIN = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe'}
FAT = {b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'}
IDENTITY_PLACEHOLDER = '${AXIOSOZO_SIGN_IDENTITY}'

# (relative glob, entitlements file, provenance). First match wins. '' is the .app root.
ENTITLEMENT_RULES = (
    ('Contents/MacOS/plugin-container.app', 'plugin-container.plist',
     'firefox: security/mac/hardenedruntime/production/plugin-container.xml'),
    ('Contents/MacOS/media-plugin-helper.app', 'media-plugin-helper.plist',
     'firefox: security/mac/hardenedruntime/production/media-plugin-helper.xml'),
    ('Contents/MacOS/security-module-helper.app', 'security-module-helper.plist',
     'firefox: security/mac/hardenedruntime/production/security-module-helper.xml'),
    ('Contents/Helpers/*.app/Contents/Frameworks/* Helper (Renderer).app', 'cef-helper-jit.plist',
     'UNVERIFIED_ASSUMPTION: no entitlements file in pinned CEF minimal archive'),
    ('Contents/Helpers/*.app/Contents/Frameworks/* Helper (GPU).app', 'cef-helper-jit.plist',
     'UNVERIFIED_ASSUMPTION: no entitlements file in pinned CEF minimal archive'),
    ('', 'browser.plist',
     'firefox: security/mac/hardenedruntime/production/firefox.browser.xml minus restricted keys'),
)


def is_macho(path):
    try:
        with Path(path).open('rb') as source:
            head = source.read(8)
    except OSError:
        return False
    if head[:4] in THIN:
        return True
    # 0xCAFEBABE is also a Java class file; a fat header has a small arch count.
    return head[:4] in FAT and len(head) == 8 and 0 < int.from_bytes(head[4:8], 'big') < 32


def bundle_executable(bundle):
    """Main executable of a bundle, or None for a resource-only bundle."""
    bundle = Path(bundle)
    if bundle.suffix == '.framework':
        current = bundle / 'Versions/Current'
        base = current.resolve() if current.is_symlink() or current.is_dir() else bundle
        for plist in (base / 'Resources/Info.plist', bundle / 'Resources/Info.plist'):
            if plist.is_file():
                with plist.open('rb') as source:
                    name = plistlib.load(source).get('CFBundleExecutable') or bundle.stem
                break
        else:
            name = bundle.stem
        executable = base / name
        return executable if executable.is_file() else None
    plist = bundle / 'Contents/Info.plist'
    if not plist.is_file():
        return None
    with plist.open('rb') as source:
        name = plistlib.load(source).get('CFBundleExecutable')
    executable = bundle / 'Contents/MacOS' / name if name else None
    return executable if executable and executable.is_file() else None


def entitlements_for(relative):
    for pattern, name, provenance in ENTITLEMENT_RULES:
        if (pattern == '' and relative == '') or (pattern and fnmatch(relative, pattern)):
            return name, provenance
    if relative.startswith('Contents/Helpers/'):
        return None, 'UNVERIFIED_ASSUMPTION: CEF item, hardened runtime without entitlements'
    return None, 'firefox: hardened runtime without entitlements (taskcluster/config.yml production)'


def discover(app):
    """Code items to sign as (kind, relative path): nested bundles and standalone Mach-O
    files. Symlinks are never followed; a bundle's main executable is sealed with it."""
    app = Path(app)
    bundles, machos = [], []
    for directory, dirs, files in os.walk(app):
        base = Path(directory)
        for name in dirs:
            path = base / name
            if name.endswith(BUNDLE_SUFFIXES) and not path.is_symlink():
                bundles.append(path)
        for name in files:
            path = base / name
            if not path.is_symlink() and not name.startswith('._') and is_macho(path):
                machos.append(path)
    main_executables = set()
    for bundle in [app, *bundles]:
        executable = bundle_executable(bundle)
        if executable is not None:
            main_executables.add(executable.resolve())
    items = [('bundle', bundle) for bundle in bundles if bundle_executable(bundle) is not None]
    items += [('macho', path) for path in machos if path.resolve() not in main_executables]
    items.append(('bundle', app))
    return [(kind, '' if path == app else str(path.relative_to(app))) for kind, path in items]


def plan(app, identity=None, timestamp=False, stage_app=None, embeds=None, entitlements_dir=None):
    """Ordered codesign invocations, deepest first: every nested helper, library and
    framework is signed before the bundle that contains it; the .app root is last.

    `app` is inspected; `embeds` maps a relative destination (for example
    Contents/Helpers/AxioCEFProbe.app) to a source bundle that staging will copy
    there. Commands are expressed for `stage_app` when given.
    """
    target_root = Path(stage_app) if stage_app else Path(app)
    entitlements_dir = Path(entitlements_dir) if entitlements_dir else ENTITLEMENTS_DIR
    items = discover(app)
    for prefix, source in sorted((embeds or {}).items()):
        items += [(kind, str(Path(prefix) / relative) if relative else prefix)
                  for kind, relative in discover(source)]
    steps = []
    for kind, relative in items:
        name, provenance = entitlements_for(relative)
        command = ['/usr/bin/codesign', '--force', '--sign', identity or IDENTITY_PLACEHOLDER,
                   '--options', 'runtime', '--timestamp' if timestamp else '--timestamp=none']
        if name:
            command += ['--entitlements', str(entitlements_dir / name)]
        command.append(str(target_root / relative) if relative else str(target_root))
        steps.append({'kind': kind, 'relative': relative or '.', 'entitlements': name,
                      'provenance': provenance, 'depth': len(Path(relative).parts) if relative else 0,
                      'command': command})
    steps.sort(key=lambda step: (-step['depth'], step['relative']))
    return steps
