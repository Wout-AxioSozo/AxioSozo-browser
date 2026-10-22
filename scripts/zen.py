#!/usr/bin/env python3
"""Audited Zen source adapter. Never substitutes a stock app for a missing build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import plistlib
import shutil
import stat
import subprocess
import sys
import urllib.request
import storage
import zen_toolchain
import zen_import

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'upstream/zen'
# The volume root or one named sub-root on it (storage.build_root validates).
BUILD_ROOT = storage.build_root()
BUILD = BUILD_ROOT / 'zen'
STAGE = BUILD / 'source'
OBJECT = BUILD / 'obj'
EXTERNAL = '/Users/wout/.local/bin/dev-external'
MOUNT = '/Users/wout/.local/bin/mount-dev-storage'
ZEN_SHA = '541ee55196395ee8082862b83c84c90ab41d9f6d'
FIREFOX_REVISION = '48a735cc74a7e6d730f9bf59c3de58f750287a23'
FIREFOX_SHA512 = 'a5cc16a63f4b43f01bc26e2feb12e54e5281aebdc81775cbc0e8d77dd5ceefa5055e78d2a6ad54895c397d414d1fac13d49ddf28099f534269a6a166dedafa5e'
FIREFOX_VERSION = '158.0.1'
FIREFOX_URL = f'https://archive.mozilla.org/pub/firefox/candidates/{FIREFOX_VERSION}-candidates/build1/source/firefox-{FIREFOX_VERSION}.source.tar.xz'
BUNDLE_ID = 'nl.axiosozo.browser.dev'


def capture(args, cwd=None, env=None):
    try:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=30, env=env)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def file_hash(path, algorithm='sha512'):
    digest = hashlib.new(algorithm)
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


CHROME_SOURCE = ROOT / 'apps/browser/chrome'
CONTEXTS_SOURCE = ROOT / 'packages/contexts/src'
MIRROR = 'src/zen/common/axiosozo'
GENERATED_JAR = MIRROR + '/jar.inc.mn'
PREFS_MIRROR = 'prefs/axiosozo.yaml'
PACKAGED_SUFFIXES = {'.mjs', '.js', '.xhtml', '.html', '.css', '.svg'}
JAR_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*')
# Native Gecko components (XPIDL, C++, Objective-C++) compiled into libxul.
NATIVE_SOURCE = ROOT / 'apps/browser/native'
NATIVE_MIRROR = 'src/zen/axiosozo-native'
NATIVE_ENGINE = 'zen/axiosozo-native'  # where refresh_links exposes NATIVE_MIRROR inside engine/
NATIVE_SUFFIXES = {'.idl', '.h', '.hpp', '.cpp', '.mm', '.m', '.conf', '.build'}  # '.build' covers moz.build


def _git_files(folder):
    """Relative paths under folder that git tracks or would track: tracked plus
    untracked-but-not-ignored (`ls-files --cached --others --exclude-standard`).
    Ignored files (local builds, secrets, sidecars) are never packaged."""
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                 'ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', '.'],
                                cwd=folder, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'PACKAGING_GIT_UNAVAILABLE: {folder}') from error
    if result.returncode != 0:
        raise RuntimeError(f'PACKAGING_GIT_UNAVAILABLE: {folder}')
    names = []
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8', 'surrogateescape')
        if name.startswith('/') or '\\' in name or any(part in ('', '.', '..') for part in name.rstrip('/').split('/')):
            raise RuntimeError('UNPACKAGEABLE_CHROME_FILE: ' + name)
        names.append(name)
    return sorted(set(names))


def _tree(folder, suffixes, recursive=True):
    """Packaged sources under folder: git-known, regular, never a symlink.

    Every path component below folder is lstat-checked; a symlink anywhere
    (file or directory) fails closed instead of being followed or skipped."""
    if folder.is_symlink():
        raise RuntimeError(f'SYMLINKED_CHROME_FILE: {folder}')
    if not folder.is_dir():
        return []
    paths = []
    for name in _git_files(folder):
        parts = name.rstrip('/').split('/')
        if any(part.startswith('.') for part in parts):
            continue
        if not recursive and len(parts) != 1:
            continue
        current = folder
        kind = None
        for part in parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                kind = 'missing'  # tracked but deleted in the worktree
                break
            if stat.S_ISLNK(mode):
                raise RuntimeError(f'SYMLINKED_CHROME_FILE: {folder.name}/{name}')
            kind = 'file' if stat.S_ISREG(mode) else 'directory' if stat.S_ISDIR(mode) else 'other'
        if kind == 'other':
            raise RuntimeError(f'UNPACKAGEABLE_CHROME_FILE: {name}')
        if kind == 'file' and current.suffix in suffixes:
            paths.append(current)
    return sorted(paths)


def packaged_files():
    """[(relative name under content/browser/axiosozo/, authoritative source)].

    Every chrome file (any subdirectory, e.g. overview/) plus the DOM-free
    contexts core as contexts/*.mjs. New files ship without an overlay edit:
    the hash-guarded jar.inc.mn change only includes the generated manifest.
    """
    files = [(path.relative_to(CHROME_SOURCE).as_posix(), path)
             for path in _tree(CHROME_SOURCE, PACKAGED_SUFFIXES)]
    if any(name.split('/')[0] == 'contexts' for name, _ in files):
        raise RuntimeError('RESERVED_CHROME_PATH: apps/browser/chrome/contexts is generated from packages/contexts/src')
    files += [('contexts/' + path.name, path) for path in _tree(CONTEXTS_SOURCE, {'.mjs'}, recursive=False)]
    for name, _ in files:
        if not JAR_NAME.fullmatch(name) or name == 'jar.inc.mn':
            raise RuntimeError('UNPACKAGEABLE_CHROME_FILE: ' + name)
    return sorted(files)


def native_files():
    """[(relative name under apps/browser/native, authoritative source)]: git-known,
    regular files only. The directory's own moz.build is generated, never authored."""
    files = [(path.relative_to(NATIVE_SOURCE).as_posix(), path) for path in _tree(NATIVE_SOURCE, NATIVE_SUFFIXES)]
    for name, _ in files:
        if not JAR_NAME.fullmatch(name):
            raise RuntimeError('UNPACKAGEABLE_NATIVE_FILE: ' + name)
        if name == 'moz.build':
            raise RuntimeError('RESERVED_NATIVE_PATH: apps/browser/native/moz.build is generated from its subdirectories')
    return sorted(files)


def generated_native_moz_build(files):
    subdirs = sorted({name.split('/')[0] for name, _ in files if name.count('/') == 1 and name.endswith('/moz.build')})
    lines = ['# This Source Code Form is subject to the terms of the Mozilla Public',
             '# License, v. 2.0. If a copy of the MPL was not distributed with this',
             '# file, You can obtain one at http://mozilla.org/MPL/2.0/.',
             '# Generated by AxioSozo scripts/zen.py from apps/browser/native. Do not edit.']
    if subdirs:
        lines += ['', 'DIRS += ['] + [f'    "{name}",' for name in subdirs] + [']']
    return ('\n'.join(lines) + '\n').encode()


def native_outputs(files=None):
    """Owned files of the native mirror → exact bytes; empty when there is no native source."""
    files = native_files() if files is None else files
    if not files:
        return {}
    outputs = {NATIVE_MIRROR + '/moz.build': generated_native_moz_build(files)}
    for name, source in files:
        outputs[NATIVE_MIRROR + '/' + name] = source.read_bytes()
    return outputs


def native_digests(outputs=None):
    """{top-level native directory ('.' for the generated root): sha256 of its mirrored files}."""
    outputs = native_outputs() if outputs is None else outputs
    groups = {}
    for relative, data in sorted(outputs.items()):
        if relative.startswith(NATIVE_MIRROR + '/'):
            parts = relative[len(NATIVE_MIRROR) + 1:].split('/')
            groups.setdefault(parts[0] if len(parts) > 1 else '.', hashlib.sha256()).update(
                relative.encode() + b'\0' + hashlib.sha256(data).digest())
    return {name: digest.hexdigest() for name, digest in sorted(groups.items())}


def generated_jar_manifest(files=None):
    lines = ['# This Source Code Form is subject to the terms of the Mozilla Public',
             '# License, v. 2.0. If a copy of the MPL was not distributed with this',
             '# file, You can obtain one at http://mozilla.org/MPL/2.0/.',
             '# Generated by AxioSozo scripts/zen.py from apps/browser/chrome and',
             '# packages/contexts/src. Do not edit; run `scripts/zen.py prepare`.']
    for name, _ in (packaged_files() if files is None else files):
        lines.append(f'        content/browser/axiosozo/{name} (../../zen/common/axiosozo/{name})')
    return ('\n'.join(lines) + '\n').encode()


def generated_outputs():
    """Every file the overlay owns in the Zen tree → exact bytes."""
    files = packaged_files()
    if (CHROME_SOURCE / 'defaults.yaml').is_symlink():
        raise RuntimeError('SYMLINKED_CHROME_FILE: defaults.yaml')
    outputs = {PREFS_MIRROR: (CHROME_SOURCE / 'defaults.yaml').read_bytes(),
               GENERATED_JAR: generated_jar_manifest(files)}
    for name, source in files:
        outputs[MIRROR + '/' + name] = source.read_bytes()
    outputs.update(native_outputs())
    return outputs


def owned_output(relative):
    return relative == PREFS_MIRROR or relative.startswith((MIRROR + '/', NATIVE_MIRROR + '/'))


def unexpected_source_changes(destination=UPSTREAM):
    """Reject changes that the pinned build clone would silently omit or overwrite."""
    tracked = capture(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                       'diff', '--name-only', '-z', 'HEAD', '--'], destination)
    untracked = capture(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                         'ls-files', '--others', '--exclude-standard', '-z'], destination)
    if tracked is None or untracked is None:
        return ['SOURCE_STATUS_UNAVAILABLE']
    records = {}
    for item in overlay_records():
        records.setdefault(item['path'], set()).update((item['before_sha256'], item['after_sha256']))
    # Exact results of withdrawn patches are known; the next overlay restores them.
    for item in retired_records():
        records.setdefault(item['path'], set()).update(item['patched_sha256'])
    try:
        generated = generated_outputs()
    except RuntimeError as error:
        return [str(error)]
    previous = overlay_state(destination)
    if previous is None:
        return ['INVALID_OVERLAY_STATE']
    unknown = []
    for relative in sorted(set((tracked + '\0' + untracked).split('\0'))):
        if not relative or any(part.startswith('._') for part in Path(relative).parts):
            continue
        if relative == '.axiosozo-overlay-state.json':
            continue
        path = destination / relative
        if relative in records and path.is_file():
            if file_hash(path, 'sha256') in records[relative]:
                continue
        # Owned mirrors: current generated bytes, or the unmodified previous
        # generation (a source refresh, or a removed file the next overlay deletes).
        if (relative in generated or relative in previous) and path.is_file() and not path.is_symlink():
            if path.read_bytes() == generated.get(relative) or file_hash(path, 'sha256') == previous.get(relative):
                continue
        unknown.append(relative)
    # This ignored input is particularly easy to lose when cloning a generated build stage.
    if (destination / 'mozconfig').exists():
        unknown.append('mozconfig (ignored upstream input; export to maintained build configuration)')
    return unknown


def doctor():
    issues = []
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        issues.append('macOS Apple Silicon required for this build')
    if not Path('/Volumes/DevStorage').is_mount() or not storage.mounted():
        issues.append('Project APFS build storage is not mounted')
        free = 0
    else:
        free = shutil.disk_usage(BUILD_ROOT).free // (1024**3)
        if free < 30:
            issues.append(f'Project storage has {free} GiB free; Zen requires at least 30 GB')
    actual = capture(['git', 'rev-parse', 'HEAD'], UPSTREAM)
    if actual != ZEN_SHA:
        issues.append('Zen checkout missing or does not match pin')
    else:
        unexpected = unexpected_source_changes()
        if unexpected:
            issues.append('UNEXPORTED_SOURCE_CHANGES: ' + ', '.join(unexpected[:10]))
    tool_environment = zen_toolchain.environment()
    versions = {name: capture([name, '--version'], ROOT, tool_environment) for name in ['node', 'rustc', 'python3', 'gtar', 'cbindgen']}
    versions['npm_path'] = shutil.which('npm', path=tool_environment['PATH'])  # Do not invoke npm merely for discovery.
    if not versions['node'] or not versions['node'].startswith('v22.'):
        issues.append('Select Node 22 from upstream .nvmrc before setup')
    if not versions['rustc'] or not versions['rustc'].startswith('rustc 1.95.0 '):
        issues.append('Select Rust 1.95.0 from upstream .rust-toolchain before setup')
    if not versions['gtar']:
        issues.append('GNU tar (gtar) is required by pinned Surfer')
    if versions['cbindgen'] != 'cbindgen 0.29.4':
        issues.append('Project-local cbindgen0.29.4 required by pinned Firefox')
    if not versions['python3'] or not versions['python3'].startswith('Python 3.11.'):
        issues.append('Project-local Python3.11 required by pinned upstream')
    sdk_roots = [Path('/Library/Developer/CommandLineTools/SDKs'), Path('/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs')]
    if not any(list(path.glob('MacOSX*.sdk')) for path in sdk_roots):
        issues.append('macOS SDK unavailable')
    native_build_verified = False
    stamp = BUILD / 'build-stamp.json'
    if stamp.is_file():
        try:
            saved = json.loads(stamp.read_text())
            native_build_verified = (saved.get('fingerprint') == fingerprint()
                                     and saved.get('executable') == str(validate_application()))
        except (OSError, ValueError, KeyError, RuntimeError):
            pass
    return {'status': 'BLOCKED_ENV' if issues else 'PASS', 'issues': issues,
            'free_gib': free, 'versions': versions, 'upstream_sha': actual,
            'upstream_python': '3.11', 'build_jobs': build_jobs(), 'build_root': str(BUILD),
            'application': str(OBJECT / 'dist/AxioSozo Dev.app'),
            'bundle_id': BUNDLE_ID, 'native_build_verified': native_build_verified}


def overlay(destination):
    """Strict idempotent patch application; unknown source changes are rejected.
    Returns the owned output paths written or removed by this run."""
    outputs = generated_outputs()
    # Records marked "when": "native" (DIRS += axiosozo-native) apply only while native source exists.
    native = any(path.startswith(NATIVE_MIRROR + '/') for path in outputs)
    manifest = [item for item in overlay_records() if native or item.get('when') != 'native']
    previous = overlay_state(destination)
    if previous is None:
        raise RuntimeError('INVALID_OVERLAY_STATE')
    restore_retired(destination)
    revert_records(destination, [item for item in overlay_records() if item not in manifest])
    apply_records(destination, manifest)
    changed = []
    for relative, data in outputs.items():
        target = destination / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError('UNEXPECTED_MIRROR_ENTRY: ' + relative)
        if not target.resolve().is_relative_to(destination.resolve()):
            raise RuntimeError('MIRROR_ESCAPE: ' + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
            changed.append(relative)
    # Remove mirrors of deleted sources, only when still exactly as generated.
    for relative, digest in previous.items():
        target = destination / relative
        if relative not in outputs and target.is_file() and not target.is_symlink() \
                and file_hash(target, 'sha256') == digest:
            target.unlink()
            changed.append(relative)
    for mirror in [destination / MIRROR, destination / NATIVE_MIRROR]:
        for folder in sorted((path for path in mirror.rglob('*') if path.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    if not native and (destination / NATIVE_MIRROR).is_dir() and not any((destination / NATIVE_MIRROR).iterdir()):
        (destination / NATIVE_MIRROR).rmdir()
    state = {relative: hashlib.sha256(data).hexdigest() for relative, data in sorted(outputs.items())}
    (destination / '.axiosozo-overlay-state.json').write_text(json.dumps({'version': 1, 'files': state}, indent=2) + '\n')
    return sorted(changed)


def overlay_state(destination):
    """Previous generation {relative: sha256}; {} when absent, None when invalid."""
    state_path = destination / '.axiosozo-overlay-state.json'
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text())
        previous = state['files']
        if state['version'] != 1 or not isinstance(previous, dict) or not all(
                isinstance(path, str) and owned_output(path) and '..' not in Path(path).parts
                and isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                for path, value in previous.items()):
            return None
        return previous
    except (ValueError, KeyError, TypeError):
        return None


def materialize_axiosozo_resources(stage, app):
    """Replace AxioSozo JAR symlinks in the dev bundle with copies.

    about:axiosozo and its actor child load in the sandboxed privilegedabout
    process, which cannot follow the second symlink hop into stage/src (same
    reason as zen_import.materialize_zen_content_resources). Every packaged
    file must be present; unknown files or links are never replaced.
    """
    source_root = stage / MIRROR
    bundle_root = app / 'Contents/Resources/browser/chrome/browser/content/browser/axiosozo'
    if not source_root.is_dir() or not bundle_root.is_dir():
        raise RuntimeError('AXIOSOZO_BUNDLE_MISSING')
    replacements = []
    for name, _ in packaged_files():
        source = source_root / name
        target = bundle_root / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError('AXIOSOZO_MIRROR_MISSING: ' + name)
        if not target.parent.resolve().is_relative_to(bundle_root.resolve()):
            raise RuntimeError('AXIOSOZO_DESTINATION_ESCAPE: ' + name)
        if target.is_symlink():
            if target.resolve(strict=True) != source.resolve(strict=True):
                raise RuntimeError('AXIOSOZO_UNEXPECTED_LINK: ' + name)
            replacements.append((source, target))
        elif not target.is_file():
            raise RuntimeError('AXIOSOZO_RESOURCE_NOT_PACKAGED: ' + name)
        elif target.read_bytes() != source.read_bytes():
            raise RuntimeError('AXIOSOZO_RESOURCE_MODIFIED: ' + name)
    for source, target in replacements:
        temporary = target.with_name('.axiosozo-materialize-' + target.name)
        temporary.write_bytes(source.read_bytes())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    if any(target.is_symlink() or target.read_bytes() != source.read_bytes() for source, target in replacements):
        raise RuntimeError('AXIOSOZO_MATERIALIZATION_FAILED')
    return len(replacements)


def overlay_records():
    return json.loads((ROOT / 'patches/zen/overlay.json').read_text())


def retired_records():
    return json.loads((ROOT / 'patches/zen/retired.json').read_text())


def restore_retired(destination):
    """Return withdrawn overlay results to pinned upstream; any other content is left alone."""
    for item in retired_records():
        target = destination / item['path']
        if file_hash(target, 'sha256') not in item['patched_sha256']:
            continue
        stock = subprocess.run(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                                'show', 'HEAD:' + item['path']], cwd=destination,
                               capture_output=True, timeout=30, check=True).stdout
        if hashlib.sha256(stock).hexdigest() != item['stock_sha256']:
            raise RuntimeError(f'RETIRED_PATCH_BASE_MISMATCH: {item["path"]}')
        target.write_bytes(stock)


def revert_records(destination, items):
    """Undo records whose exact result is present, so a withdrawn condition leaves stock files."""
    for item in items:
        target = destination / item['path']
        text = target.read_text()
        if hashlib.sha256(text.encode()).hexdigest() != item['after_sha256']:
            continue
        for old, new in reversed(item['replacements']):
            if text.count(new) != 1:
                raise RuntimeError(f'PATCH_AMBIGUOUS: {item["path"]}')
            text = text.replace(new, old, 1)
        if hashlib.sha256(text.encode()).hexdigest() != item['before_sha256']:
            raise RuntimeError(f'PATCH_CONFLICT: {item["path"]}')
        target.write_text(text)


def apply_records(destination, manifest):
    for index, item in enumerate(manifest):
        target = destination / item['path']
        text = target.read_text()
        current_hash = hashlib.sha256(text.encode()).hexdigest()
        later_results = {later['after_sha256'] for later in manifest[index + 1:]
                         if later['path'] == item['path']}
        if current_hash == item['after_sha256'] or current_hash in later_results:
            continue
        if current_hash != item['before_sha256']:
            raise RuntimeError(f'PATCH_CONFLICT: {item["path"]}')
        for old, new in item['replacements']:
            if text.count(old) != 1:
                raise RuntimeError(f'PATCH_AMBIGUOUS: {item["path"]}')
            text = text.replace(old, new, 1)
        target.write_text(text)


def fingerprint(native=True):
    """Build input digest. native=False leaves out apps/browser/native, so
    native-build can tell native-only edits from changes needing a full setup."""
    digest = hashlib.sha256()
    for folder in [ROOT / 'apps/browser/chrome', ROOT / 'apps/browser/branding', ROOT / 'patches/zen', CONTEXTS_SOURCE]:
        for path in sorted(folder.rglob('*')) if folder.is_dir() else []:
            if path.is_file() and not path.name.startswith('._'):
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
    digest.update(Path(__file__).read_bytes())
    digest.update((ROOT / 'scripts/zen_toolchain.py').read_bytes())
    digest.update((ROOT / 'scripts/zen_import.py').read_bytes())
    if native:
        for name, path in native_files():
            digest.update(b'native/' + name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def build_jobs():
    """Parallel build jobs: AXIOSOZO_BUILD_JOBS, else the CPU count bounded by
    memory (about 2.5 GiB per Gecko/Rust compile job, so 16 GiB gives 6)."""
    value = os.environ.get('AXIOSOZO_BUILD_JOBS')
    if value is None:
        try:
            memory = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        except (ValueError, OSError):
            return 2
        return max(1, min(os.cpu_count() or 2, int(memory / (2.5 * 2**30))))
    if not re.fullmatch(r'[1-9][0-9]{0,2}', value):
        raise RuntimeError('INVALID_AXIOSOZO_BUILD_JOBS: ' + value)
    return int(value)


def build_environment():
    env = zen_toolchain.environment()
    jobs = str(build_jobs())
    env.update({'MOZBUILD_STATE_PATH': str(BUILD / 'mozbuild'), 'npm_config_cache': str(BUILD / 'npm-cache'),
                'PIP_CACHE_DIR': str(BUILD / 'pip-cache'), 'PYTHONDONTWRITEBYTECODE': '1',
                'MACHRC': '/dev/null', 'DISABLE_TELEMETRY': '1',
                'MACH_BUILD_PYTHON_NATIVE_PACKAGE_SOURCE': 'none',
                'WASM_CC': str(zen_toolchain.TOOLS / 'wasi/bin/clang'),
                'WASM_CXX': str(zen_toolchain.TOOLS / 'wasi/bin/clang++'),
                'WASI_SYSROOT': str(zen_toolchain.TOOLS / 'wasi/share/wasi-sysroot'),
                'SURFER_NO_BRANDING_PATCH': 'true', 'ZEN_DISABLE_BOOTSTRAP': '1', 'MOZ_AUTOMATION': '1',
                'CARGO_BUILD_JOBS': jobs, 'CMAKE_BUILD_PARALLEL_LEVEL': jobs})
    return env


def run_command(args, cwd=STAGE, env=None):
    print('+ ' + ' '.join(str(arg) for arg in args), flush=True)
    subprocess.run([EXTERNAL, sys.executable, str(ROOT / 'scripts/storage.py'), 'exec', '--', *map(str, args)],
                   cwd=cwd, env=env or zen_toolchain.environment(), check=True)


def fetch_firefox(env=None):
    BUILD.mkdir(parents=True, exist_ok=True)
    archive = BUILD / f'firefox-{FIREFOX_VERSION}.source.tar.xz'
    if not archive.exists():
        partial = archive.with_suffix('.partial')
        run_command(['/usr/bin/curl', '--fail', '--location', '--retry', '3', '--continue-at', '-', '--output', partial, FIREFOX_URL], ROOT, env)
        if file_hash(partial) != FIREFOX_SHA512:
            raise RuntimeError('FIREFOX_CHECKSUM_MISMATCH')
        partial.rename(archive)
    if file_hash(archive) != FIREFOX_SHA512:
        raise RuntimeError('FIREFOX_CHECKSUM_MISMATCH')
    return archive


def stage_mozconfig(jobs):
    """The exact build-stage mozconfig written by setup (an ignored upstream input)."""
    return f'''# AxioSozo development configuration, MPL-2.0.
mk_add_options MOZ_OBJDIR={OBJECT}
mk_add_options MOZ_MAKE_FLAGS="-j{jobs}"
ac_add_options --disable-gtest-in-build
ac_add_options --with-app-basename=AxioSozo
ac_add_options --with-distribution-id=nl.axiosozo
ac_add_options --disable-updater
ac_add_options --disable-debug-symbols
export MOZ_MACBUNDLE_ID=browser.dev
export MOZ_APP_REMOTINGNAME=AxioSozoDev
'''


def setup(build_native=True):
    subprocess.run([MOUNT], check=True)
    if not storage.mounted():
        raise RuntimeError('PROJECT_STORAGE_NOT_MOUNTED')
    unexpected = unexpected_source_changes()
    if unexpected:
        raise RuntimeError('UNEXPORTED_SOURCE_CHANGES: ' + ', '.join(unexpected[:10]))
    zen_toolchain.setup()
    result = doctor()
    print(json.dumps(result, indent=2), flush=True)
    if result['issues']:
        return 20
    BUILD.mkdir(parents=True, exist_ok=True)
    if not STAGE.exists():
        # Independent generated build checkout on APFS; never relocate authoritative source.
        # Use Git transport even for this local clone, avoiding exFAT AppleDouble
        # files in .git/objects and sharing no mutable object storage.
        run_command(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-local', UPSTREAM, STAGE], ROOT)
    if capture(['git', 'rev-parse', 'HEAD'], STAGE) != ZEN_SHA:
        raise RuntimeError('BUILD_STAGE_PIN_MISMATCH')
    build_fingerprint = fingerprint()
    base_fingerprint = fingerprint(native=False)
    native_dirs = native_digests()
    overlay(STAGE)
    env = build_environment()
    jobs = str(build_jobs())
    # No upstream lifecycle hooks, global package installation, or implicit toolchain bootstrap.
    dependencies_hash = file_hash(STAGE / 'package-lock.json', 'sha256')
    dependencies_stamp = STAGE / 'node_modules/.axiosozo-lock'
    if not dependencies_stamp.exists() or dependencies_stamp.read_text() != dependencies_hash:
        run_command(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], env=env)
        dependencies_stamp.write_text(dependencies_hash)
    # Pinned Surfer eagerly loads sharp even when branding is disabled; this reviewed
    # one-line lazy import avoids running native dependency install scripts.
    apply_records(STAGE, [json.loads((ROOT / 'patches/zen/surfer-no-branding.json').read_text())])
    apply_records(STAGE, [json.loads((ROOT / 'patches/zen/surfer-forward-import.json').read_text())])
    archive = fetch_firefox(env)
    engine = STAGE / 'engine'
    extraction = BUILD / 'firefox-extraction.json'
    if not engine.exists():
        extraction.write_text(json.dumps({'sha512': FIREFOX_SHA512, 'destination': str(engine)}))
        engine.mkdir()
    if not (engine / '.axiosozo-source-pin').exists():
        if not extraction.exists() or json.loads(extraction.read_text()) != {'sha512': FIREFOX_SHA512, 'destination': str(engine)} or (engine / '.git').exists():
            raise RuntimeError('UNOWNED_OR_MODIFIED_PARTIAL_FIREFOX_SOURCE')
        # Resume only our recorded incomplete extraction; never reset an active source checkout.
        # macOS libarchive has built-in xz support; GNU tar delegates to an
        # external xz executable absent from a clean system toolchain PATH.
        run_command(['/usr/bin/tar', '--strip-components=1', '-xf', archive, '-C', engine], env=env)
        (engine / '.axiosozo-source-pin').write_text(FIREFOX_REVISION)
    if not (engine / '.axiosozo-source-pin').exists() or (engine / '.axiosozo-source-pin').read_text() != FIREFOX_REVISION:
        raise RuntimeError('FIREFOX_STAGE_NOT_VERIFIED')
    if not (engine / '.git').exists():
        run_command(['git', '-c', 'core.hooksPath=/dev/null', 'init'], engine, env)
    # A large pristine import must not spawn an unowned background pack process
    # that overlaps the subsequent native compiler on this 16 GiB machine.
    run_command(['git', 'config', '--local', 'gc.auto', '0'], engine, env)
    if not capture(['git', 'rev-parse', 'HEAD'], engine):
        # Surfer's patch engine expects a pristine source commit. Local fixed identity only.
        run_command(['git', '-c', 'core.hooksPath=/dev/null', 'add', '.'], engine, env)
        run_command(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=AxioSozo Local Build',
                     '-c', 'user.email=build@invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm',
                     f'Pinned Firefox {FIREFOX_VERSION} source'], engine, env)
    brand = engine / 'browser/branding/axiosozo-dev'
    if not brand.exists():
        shutil.copytree(engine / 'browser/branding/unofficial', brand)
    (brand / 'configure.sh').write_text('# MPL-2.0: development branding.\nMOZ_APP_DISPLAYNAME="AxioSozo Dev"\nMOZ_MACBUNDLE_ID=browser.dev\n')
    for name in ['brand.ftl', 'brand.properties']:
        relative = Path('locales/en-US') / name
        text = (engine / 'browser/branding/unofficial' / relative).read_text()
        text = text.replace('= Nightly', '= AxioSozo Dev').replace('=Nightly', '=AxioSozo Dev')
        text = text.replace('-brand-product-name = Firefox', '-brand-product-name = AxioSozo')
        text = text.replace('-vendor-short-name = Mozilla', '-vendor-short-name = AxioSozo')
        (brand / relative).write_text(text)
    # AxioSozo browser icon set, generated by assets/brand/browser-logo-v2/build_icons.py.
    # The About dialog wordmark is "AXIOSOZO", so replace Nightly's purple background with brand ink.
    for source in sorted((ROOT / 'apps/browser/branding').rglob('*')):
        if source.is_file() and not source.name.startswith('._'):
            shutil.copyfile(source, brand / source.relative_to(ROOT / 'apps/browser/branding'))
    dialog = (engine / 'browser/branding/unofficial/content/aboutDialog.css').read_text()
    for before, after in [('#130829', '#1c1a16'), ('hsla(235, 43%, 10%, 0.5)', 'rgb(15 13 10 / 0.5)')]:
        if before not in dialog:
            raise RuntimeError('BRANDING_DIALOG_CSS_CHANGED')
        dialog = dialog.replace(before, after)
    (brand / 'content/aboutDialog.css').write_text(dialog)
    # Rust ffprefs generates the exact preference patches used by this checkout.
    run_command(['npm', 'run', 'ffprefs'], env=env)
    # Zen's import:dumps filters the signed Mozilla search-config-v2 fallback
    # without re-signing it. Preserve exactly the pinned Mozilla data instead.
    signed_dump = json.loads((ROOT / 'patches/zen/signed-search-dump.json').read_text())
    if zen_import.restore_signed_search_dump(engine, signed_dump, env):
        print('PASS: restored pristine signed Mozilla search-config-v2 fallback', flush=True)
    native_patches = json.loads((ROOT / 'patches/zen/firefox-native.json').read_text())
    import_key, imported_files, needs_import = zen_import.prepare(STAGE, BUILD, env, native_patches)
    if needs_import:
        run_command(['npm', 'run', 'surfer', '--', 'import'], env=env)
        zen_import.finish(STAGE, BUILD, import_key, imported_files)
    zen_import.refresh_links(STAGE)
    installed_locales = zen_import.install_zen_locales(STAGE)
    print(f'PASS: verified 14 Zen en-US Fluent files ({installed_locales} installed)', flush=True)
    apply_records(engine, native_patches)
    run_command(['npm', 'run', 'surfer', '--', 'config', 'brand', 'axiosozo-dev'], env=env)
    run_command(['npm', 'run', 'surfer', '--', 'config', 'buildMode', 'dev'], env=env)
    (STAGE / 'mozconfig').write_text(stage_mozconfig(jobs))
    if not build_native:
        run_command(['npm', 'run', 'build', '--', '--jobs', jobs], env={**env, 'SURFER_MOZCONFIG_ONLY': '1'})
        print('PASS: pinned Firefox/Zen source imported and mozconfig generated; native compilation not started', flush=True)
        return 0
    run_command(['npm', 'run', 'build', '--', '--jobs', jobs], env=env)
    materialized = zen_import.materialize_zen_content_resources(
        STAGE, OBJECT / 'dist/AxioSozo Dev.app')
    print(f'PASS: verified 9 sandbox-readable Zen content resources ({materialized} materialized)', flush=True)
    axiosozo_materialized = materialize_axiosozo_resources(STAGE, OBJECT / 'dist/AxioSozo Dev.app')
    print(f'PASS: verified {len(packaged_files())} packaged AxioSozo chrome files ({axiosozo_materialized} materialized)', flush=True)
    executable = validate_application()
    (BUILD / 'build-stamp.json').write_text(json.dumps({'zen_sha': ZEN_SHA, 'firefox_revision': FIREFOX_REVISION,
                                                     'fingerprint': build_fingerprint, 'base_fingerprint': base_fingerprint,
                                                     'native_dirs': native_dirs, 'executable': str(executable)}))
    if fingerprint() != build_fingerprint:
        raise RuntimeError('SOURCE_CHANGED_DURING_BUILD: repeat setup to package the current chrome')
    return 0


def native_build():
    """Fast path for edits under apps/browser/native only: mirror, then rebuild just the
    changed native directories and relink libxul. Anything else needs a full setup."""
    subprocess.run([MOUNT], check=True)
    if not storage.mounted():
        raise RuntimeError('PROJECT_STORAGE_NOT_MOUNTED')
    stamp_path = BUILD / 'build-stamp.json'
    engine = STAGE / 'engine'
    if not stamp_path.is_file() or not (engine / 'mozconfig').is_file() or capture(['git', 'rev-parse', 'HEAD'], STAGE) != ZEN_SHA:
        raise RuntimeError('FULL_SETUP_REQUIRED: no completed build to update incrementally; run ./dev setup')
    saved = json.loads(stamp_path.read_text())
    if saved.get('base_fingerprint') != fingerprint(native=False):
        raise RuntimeError('FULL_SETUP_REQUIRED: inputs other than apps/browser/native changed; run ./dev setup')
    unexpected = unexpected_source_changes(STAGE)
    # The stage always carries the mozconfig setup wrote; only that exact file is expected.
    mozconfig = STAGE / 'mozconfig'
    if not mozconfig.is_symlink() and mozconfig.is_file() and mozconfig.read_text() == stage_mozconfig(str(build_jobs())):
        unexpected = [item for item in unexpected if not item.startswith('mozconfig ')]
    if unexpected:
        raise RuntimeError('UNEXPORTED_SOURCE_CHANGES: ' + ', '.join(unexpected[:10]))
    build_fingerprint = fingerprint()
    current = native_digests()
    previous = saved.get('native_dirs') or {}
    overlay(STAGE)
    zen_import.refresh_links(STAGE)
    changed = sorted(name for name in current if current[name] != previous.get(name))
    if changed or set(previous) - set(current):
        # Same environment `surfer build` gives mach (brand from `surfer config brand`).
        # mach snapshots the environment in .mozconfig.json; any difference forces a
        # reconfigure and a wide recompile, both here and in the next setup.
        env = {**build_environment(), 'ACCEPTED_MAR_CHANNEL_IDS': 'axiosozo-dev', 'MAR_CHANNEL_ID': 'axiosozo-dev'}
        for name in changed:
            # Firefox 156 mach ignores directory targets without this flag
            # ("Build argument ... is a subdirectory and was ignored").
            run_command(['./mach', 'build', '--allow-subdirectory-build',
                         NATIVE_ENGINE + ('' if name == '.' else '/' + name)], engine, env)
        run_command(['./mach', 'build', 'binaries'], engine, env)
    else:
        print('PASS: no native source changes since the last build', flush=True)
    executable = validate_application()
    if fingerprint() != build_fingerprint:
        raise RuntimeError('SOURCE_CHANGED_DURING_BUILD: repeat native-build')
    stamp_path.write_text(json.dumps({**saved, 'fingerprint': build_fingerprint, 'native_dirs': current,
                                      'executable': str(executable)}))
    print(f'PASS: native rebuild complete ({len(changed)} changed directories)', flush=True)
    return 0


def validate_application():
    app = OBJECT / 'dist/AxioSozo Dev.app'
    with (app / 'Contents/Info.plist').open('rb') as source:
        info = plistlib.load(source)
    if info.get('CFBundleIdentifier') != BUNDLE_ID:
        raise RuntimeError('BUNDLE_ID_NOT_ISOLATED')
    executable = app / 'Contents/MacOS' / info['CFBundleExecutable']
    if not executable.is_file():
        raise RuntimeError('APP_EXECUTABLE_MISSING')
    return executable


def describe():
    stamp = BUILD / 'build-stamp.json'
    if not stamp.exists():
        return {'status': 'BLOCKED_ENV', 'reason': 'CUSTOM_ZEN_BUILD_MISSING', 'setup': './dev setup'}
    unexpected = unexpected_source_changes()
    if unexpected:
        return {'status': 'BLOCKED_ENV', 'reason': 'UNEXPORTED_SOURCE_CHANGES', 'paths': unexpected[:10]}
    saved = json.loads(stamp.read_text())
    if saved.get('fingerprint') != fingerprint():
        return {'status': 'BLOCKED_ENV', 'reason': 'NATIVE_REBUILD_REQUIRED', 'setup': './dev setup'}
    return {'status': 'PASS', 'executable': str(validate_application()), 'bundle_id': BUNDLE_ID,
            'engine': 'gecko', 'cef_embedding': 'experimental_web_mode_unverified',
            'fingerprint': saved['fingerprint']}


def _legacy_run_profile(profile):
    # Preserve the existing runtime namespace and admission checks.
    if not profile or not profile.is_absolute() or not profile.is_dir():
        raise RuntimeError('EXPLICIT_EXISTING_DEVELOPMENT_PROFILE_REQUIRED')
    profile = profile.resolve()
    namespace = BUILD_ROOT / 'runtime' / hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    if profile.parent.parent != namespace or profile.name != 'gecko' or not re.fullmatch(r'[A-Za-z0-9-]+', profile.parent.name):
        raise RuntimeError('DEVELOPMENT_PROFILE_NAMESPACE_MISMATCH')
    if not (profile / '.axiosozo-dev-profile').is_file():
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
    return profile


def _synthetic_directory_identity(path, uid):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
    return (info.st_dev, info.st_ino)


def _read_synthetic_profile_marker(marker, run_id, uid):
    def admitted(info):
        return (stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and 0 <= info.st_size <= 512)
    before = marker.lstat()
    if not admitted(before):
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not admitted(opened) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
        payload = os.read(fd, 512)
        after = os.fstat(fd)
        if (not admitted(after) or len(payload) != opened.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)):
            raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
    finally:
        os.close(fd)
    final = marker.lstat()
    if (not admitted(final) or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate marker key')
            result[key] = value
        return result
    try:
        record = json.loads(payload.decode('utf-8'), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError):
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO') from None
    expected = {'version': 1, 'kind': 'plan4-agent-gui', 'run_id': run_id,
                'worktree_sha256': hashlib.sha256(str(ROOT).encode()).hexdigest()}
    if not isinstance(record, dict) or type(record.get('version')) is not int or record != expected:
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')


def validate_run_profile(profile):
    raw = os.fspath(profile) if profile is not None else None
    path = Path(raw) if raw else None
    workstation = Path('/Volumes/AxioSozoBuild/workstation')
    # This sole short namespace is for an explicitly isolated Step 4 GUI run.
    # Retain the raw argparse spelling so dots, aliases and extra separators fail.
    if path is None or path.parent.parent != workstation:
        return _legacy_run_profile(path)
    run_id = os.environ.get('AXIOSOZO_AGENT_GUI_RUN', '')
    if (BUILD_ROOT != workstation or os.environ.get('AXIOSOZO_SYNTHETIC_TEST') != '1'
            or os.environ.get('AXIOSOZO_AGENT_GUI') != '1'
            or not re.fullmatch(r'[0-9a-f]{16}', run_id)):
        raise RuntimeError('DEVELOPMENT_PROFILE_NAMESPACE_MISMATCH')
    expected = workstation / ('p4c-' + run_id) / 'gecko'
    if raw != str(expected) or path != expected or path.resolve(strict=True) != expected:
        raise RuntimeError('DEVELOPMENT_PROFILE_NAMESPACE_MISMATCH')
    uid = os.getuid()
    before = [_synthetic_directory_identity(directory, uid) for directory in (path.parent, path)]
    _read_synthetic_profile_marker(path / '.axiosozo-dev-profile', run_id, uid)
    if (path.resolve(strict=True) != expected
            or [_synthetic_directory_identity(directory, uid) for directory in (path.parent, path)] != before):
        raise RuntimeError('PROFILE_NOT_OWNED_BY_AXIOSOZO')
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['doctor', 'setup', 'setup-source', 'native-build', 'prepare', 'check', 'describe', 'ready', 'run', 'smoke'])
    parser.add_argument('--profile')
    parser.add_argument('--url', default='about:blank')
    args = parser.parse_args()
    if args.command == 'setup':
        return setup()
    if args.command == 'setup-source':
        return setup(build_native=False)
    if args.command == 'native-build':
        return native_build()
    if args.command == 'prepare':
        unexpected = unexpected_source_changes()
        if unexpected:
            raise RuntimeError('UNEXPORTED_SOURCE_CHANGES: ' + ', '.join(unexpected[:10]))
        overlay(UPSTREAM)
        print('PASS: privileged chrome overlay applied to pinned Zen source; native build not performed')
        return 0
    if args.command == 'check':
        # Every packaged module, including overview/ and the contexts core, must parse.
        for _, path in packaged_files():
            if path.suffix == '.mjs':
                subprocess.run(['node', '--check', path], check=True)
        # Keep new browser surfaces in the root check automatically. The actual
        # Rust process tests run separately only after its build is verified.
        tests = sorted(path for path in (ROOT / 'apps/browser/tests').glob('*.test.mjs')
                       if not path.name.startswith('._') and path.name != 'coordinator.test.mjs')
        subprocess.run(['node', '--test', *tests], check=True)
        subprocess.run([sys.executable, '-B', ROOT / 'apps/browser/tests/test_bootstrap.py'], check=True)
        return 0
    if args.command == 'smoke':
        result = describe()
        if result['status'] == 'PASS':
            result = {'status': 'BLOCKED_ENV', 'reason': 'NATIVE_GUI_SMOKE_HARNESS_NOT_YET_VERIFIED'}
        print(json.dumps(result, indent=2))
        return 20
    if args.command == 'run':
        result = describe()
        if result['status'] != 'PASS':
            print(json.dumps(result, indent=2))
            return 20
        # Root dev owns profile locking and child process groups. No fallback to personal profiles.
        profile = validate_run_profile(args.profile)
        os.execv(result['executable'], [result['executable'], '-no-remote', '-profile', str(profile), args.url])
    result = doctor() if args.command == 'doctor' else describe()
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'PASS' else 20


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(json.dumps({'status': 'BLOCKED_ENV', 'error': str(error)}), file=sys.stderr)
        sys.exit(20)
