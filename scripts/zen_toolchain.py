#!/usr/bin/env python3
"""Project-local, checksum-pinned build tools. Never installs into HOME or system paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import storage

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = storage.build_root()
# Pinned, checksum-verified tools are shared by every build root on the volume.
TOOLS = storage.VOLUME / 'toolchains/zen'
DOWNLOADS = TOOLS / 'downloads'
PIN_FILE = ROOT / 'patches/zen/toolchains.json'
STORAGE = ROOT / 'scripts/storage.py'
EXTERNAL = '/Users/wout/.local/bin/dev-external'


def hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def pins():
    result = json.loads(PIN_FILE.read_text())
    for name, pin in result.items():
        if not pin.get('sha256') or len(pin['sha256']) != 64 or not pin['url'].startswith('https://'):
            raise RuntimeError('UNVERIFIED_TOOLCHAIN_PIN: ' + name)
    return result


def environment():
    env = storage.safe_environment(os.environ)
    # A desktop terminal may carry another project's release/cross-compile flags.
    # Only this repository's maintained configuration selects native build mode.
    for key in ['ZEN_RELEASE', 'ZEN_CROSS_COMPILING', 'SCCACHE_GHA_ENABLED',
                'SURFER_MOZCONFIG_ONLY', 'MOZCONFIG', 'MOZ_ARTIFACT_BUILDS',
                'CC', 'CXX', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'BASH_ENV', 'ENV',
                'CDPATH', 'MAKEFLAGS', 'MFLAGS', 'DESTDIR', 'MOZ_FETCHES_DIR',
                'MOZBUILD_STATE_PATH', 'MACHRC', 'TASK_ID', 'MOZ_SCM_LEVEL',
                'MACH_USE_SYSTEM_PYTHON', 'MACH_BUILD_PYTHON_NATIVE_PACKAGE_SOURCE']:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(('TASKCLUSTER_', 'TOOLTOOL_', 'SURFER_', 'WASM_', 'WASI_')):
            env.pop(key)
    env.update({
        'PATH': ':'.join(str(TOOLS / name / 'bin') for name in ['node', 'rust', 'python', 'tar', 'cbindgen']) + ':/usr/bin:/bin:/usr/sbin:/sbin',
        'CARGO_HOME': str(TOOLS / 'cargo-home'), 'RUSTUP_HOME': str(TOOLS / 'rustup-home'),
        'CARGO_BUILD_JOBS': '2', 'CMAKE_BUILD_PARALLEL_LEVEL': '2',
        'PIP_CACHE_DIR': str(TOOLS / 'pip-cache'), 'PYTHONDONTWRITEBYTECODE': '1',
        'npm_config_cache': str(TOOLS / 'npm-cache'), 'npm_config_userconfig': '/dev/null',
        'npm_config_globalconfig': str(TOOLS / 'npm-global-emptyrc'), 'npm_config_audit': 'false', 'npm_config_fund': 'false',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
        'CONFIG_SITE': '/dev/null',
        'npm_config_update_notifier': 'false',
    })
    return env


def run(args, cwd=None):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    subprocess.run([EXTERNAL, sys.executable, str(STORAGE), 'exec', '--', *map(str, args)],
                   cwd=cwd or ROOT, env=environment(), check=True)


def download(pin):
    path = DOWNLOADS / pin['filename']
    if path.is_file() and hash_file(path) == pin['sha256']:
        return path
    if path.exists():
        raise RuntimeError('ARCHIVE_CHECKSUM_MISMATCH: ' + str(path))
    partial = path.with_name(path.name + '.partial')
    run(['/usr/bin/curl', '--fail', '--location', '--retry', '3', '--continue-at', '-', '--output', partial, pin['url']])
    if hash_file(partial) != pin['sha256']:
        raise RuntimeError('DOWNLOAD_CHECKSUM_MISMATCH: ' + str(partial))
    partial.rename(path)
    return path


def extract(archive, destination):
    archive_hash = hash_file(archive)
    if (destination / '.extracted').is_file():
        if (destination / '.extracted').read_text().strip() != archive_hash:
            raise RuntimeError('EXTRACTED_TOOLCHAIN_PIN_MISMATCH: ' + str(destination))
        return
    receipt = destination.parent / (destination.name + '.extraction.json')
    expected = {'sha256': archive_hash, 'destination': str(destination)}
    if destination.exists():
        if not receipt.is_file() or json.loads(receipt.read_text()) != expected:
            raise RuntimeError('UNOWNED_TOOLCHAIN_DESTINATION: ' + str(destination))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(expected) + '\n')
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as package:
        for member in package:
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise RuntimeError('UNSAFE_ARCHIVE_PATH')
    # BSD tar is sufficient for tool extraction. The pinned GNU tar is used by Surfer.
    run(['/usr/bin/tar', '-xf', archive, '--strip-components=1', '-C', destination])
    (destination / '.extracted').write_text(archive_hash + '\n')


def installed(name, pin):
    marker = TOOLS / name / '.installed'
    if not marker.is_file():
        return False
    if marker.read_text().strip() != pin['sha256']:
        raise RuntimeError('INSTALLED_TOOLCHAIN_PIN_MISMATCH: ' + name)
    return True


def fetch():
    if not storage.mounted():
        raise RuntimeError('PROJECT_STORAGE_NOT_MOUNTED')
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    empty_npm_config = TOOLS / 'npm-global-emptyrc'
    if empty_npm_config.exists() and empty_npm_config.read_bytes():
        raise RuntimeError('UNEXPECTED_NPM_BUILD_CONFIG')
    if not empty_npm_config.exists():
        empty_npm_config.write_bytes(b'')
    for name, pin in pins().items():
        archive = download(pin)
        destination = TOOLS / name if name in ['node', 'python', 'wasi'] else TOOLS / 'sources' / name
        extract(archive, destination)
    print('Tool archives verified and extracted; source installer execution requires recorded audit hashes.')


def setup():
    fetch()
    locked = pins()
    for name, relative in [('rust', 'install.sh'), ('tar', 'configure'), ('cbindgen', 'build.rs')]:
        source = TOOLS / 'sources' / name
        expected = locked[name].get('audited_installer_sha256')
        if not expected or hash_file(source / relative) != expected:
            raise RuntimeError('INSTALLER_AUDIT_REQUIRED: ' + str(source / relative))
    if not installed('rust', locked['rust']):
        run(['/bin/bash', 'install.sh', '--prefix=' + str(TOOLS / 'rust'), '--disable-ldconfig',
             '--components=rustc,rust-std-aarch64-apple-darwin,cargo'], TOOLS / 'sources/rust')
        (TOOLS / 'rust/.installed').write_text(locked['rust']['sha256'] + '\n')
    if not installed('tar', locked['tar']):
        source = TOOLS / 'sources/tar'
        run(['/bin/sh', 'configure', '--prefix=' + str(TOOLS / 'tar'), '--program-prefix=g', '--disable-nls'], source)
        # GNU tar 1.35 detects Darwin libiconv but omits it from tar_LDADD
        # when NLS is disabled. The configure link probe verified -liconv.
        run(['/usr/bin/make', '-j2', 'LIBS=-liconv'], source)
        run(['/usr/bin/make', 'install', 'LIBS=-liconv'], source)
        (TOOLS / 'tar/.installed').write_text(locked['tar']['sha256'] + '\n')
    source = TOOLS / 'sources/cbindgen'
    if hash_file(source / 'Cargo.lock') != locked['cbindgen']['cargo_lock_sha256']:
        raise RuntimeError('CBINDGEN_LOCK_MISMATCH')
    if not installed('cbindgen', locked['cbindgen']):
        target = TOOLS / 'build/cbindgen'
        run(['cargo', 'build', '--locked', '--release', '--bin', 'cbindgen',
             '--target-dir', target, '--jobs', '2'], source)
        destination = TOOLS / 'cbindgen/bin'
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target / 'release/cbindgen', destination / 'cbindgen')
        (TOOLS / 'cbindgen/.installed').write_text(locked['cbindgen']['sha256'] + '\n')
    for binary in ['node/bin/node', 'rust/bin/rustc', 'rust/bin/cargo', 'python/bin/python3', 'tar/bin/gtar', 'cbindgen/bin/cbindgen', 'wasi/bin/clang']:
        run([TOOLS / binary, '--version'])
    (TOOLS / 'installed.json').write_text(json.dumps({'pins': locked, 'scope': str(TOOLS)}, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['fetch', 'setup'])
    args = parser.parse_args()
    try:
        globals()[args.command]()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print('BLOCKED_ENV:', error, file=sys.stderr)
        sys.exit(20)
