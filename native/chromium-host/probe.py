#!/usr/bin/env python3
"""Reproducible CEF E0 component build/probe. E1/E2 are never inferred from E0."""
import argparse
import contextlib
import hashlib
import http.server
import inspect
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = Path(os.environ.get('AXIOSOZO_BUILD_ROOT', '/Volumes/AxioSozoBuild'))
BASE = BUILD_ROOT / 'cef'
VERSION = '156.0.7+g47dbc1d+chromium-156.0.8078.17'
ARCHIVE_NAME = f'cef_binary_{VERSION}_macosarm64_minimal.tar.bz2'
CEF = BASE / ARCHIVE_NAME.removesuffix('.tar.bz2')
# Versioned, so a previous pin's archive is never resumed into or reused.
ARCHIVE = BASE / ARCHIVE_NAME
URL = 'https://cef-builds.spotifycdn.com/' + ARCHIVE_NAME.replace('+', '%2B')
SHA256 = 'd9a4e98dfde499bfecc49ec2290395fbfffc3923fed5f967bb95c6e05e03e903'
ARCHIVE_BYTES = 134827440
APP = BASE / 'AxioCEFProbe.app'
BINARY = APP / 'Contents/MacOS/AxioCEFProbe'
EVIDENCE = ROOT / 'docs/evidence'
EXTERNAL = '/Users/wout/.local/bin/dev-external'
MOUNT = '/Users/wout/.local/bin/mount-dev-storage'
SECURITY = '/usr/bin/security'
IDENTITY_ENV = 'AXIOSOZO_CODESIGN_IDENTITY'
ADHOC_HINT = ('signing: ad hoc (no ' + IDENTITY_ENV + '); macOS Keychain will ask about '
              '"Chromium Safe Storage" again after every rebuild. See README, '
              '"Stable local signing identity".')


def emit(status, **fields):
    print(json.dumps(dict(component='cef', status=status, **fields)), flush=True)


def run_command(args, **kwargs):
    command = list(map(str, args))
    print('+ ' + ' '.join(command), flush=True)
    # CEF is a small prebuilt component: a compiler/signer stalled for ten
    # minutes is an environment blocker, not permission to weaken protections.
    bounded = any(Path(arg).name in {'clang++', 'codesign'} for arg in command)
    limit = kwargs.pop('timeout_seconds', 600 if bounded else None)
    process = None
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        code = process.wait(timeout=limit)
        if code:
            raise subprocess.CalledProcessError(code, command)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('BLOCKED_ENV: native compiler/signature command exceeded '
                           + str(limit) + ' seconds; owned process group stopped') from exc
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        cleanup_group(process)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def host_check():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('BLOCKED_ENV: the pinned binary needs macOS arm64')
    if not Path('/Volumes/DevStorage').is_mount():
        raise RuntimeError('BLOCKED_ENV: DevStorage is not mounted')
    if not BUILD_ROOT.is_mount():
        raise RuntimeError('BLOCKED_ENV: AXIOSOZO_BUILD_ROOT must be the mounted project APFS volume')
    info = plistlib.loads(subprocess.check_output(['/usr/bin/hdiutil', 'info', '-plist']))
    verified = any(image.get('image-path') == '/Volumes/T9/Dev.sparsebundle'
                   and any(entity.get('mount-point') == str(BUILD_ROOT)
                           for entity in image.get('system-entities', []))
                   for image in info.get('images', []))
    if verified:
        # Other volumes of the same image (source, DevStorage) must not pass.
        disk = plistlib.loads(subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', str(BUILD_ROOT)]))
        verified = (disk.get('VolumeName') == 'AxioSozoBuild' and disk.get('MountPoint') == str(BUILD_ROOT)
                    and str(disk.get('FilesystemType', '')).lower() == 'apfs')
    if not verified:
        raise RuntimeError('BLOCKED_ENV: project build root is not the AxioSozoBuild APFS volume of the verified T9 image')
    if not shutil.which('xcrun'):
        raise RuntimeError('BLOCKED_ENV: Xcode command line tools missing')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def plist(bundle, executable, helper=False, suffix=''):
    path = bundle / 'Contents'
    (path / 'MacOS').mkdir(parents=True, exist_ok=True)
    info = dict(CFBundleIdentifier='dev.axiosozo.cef-probe' + suffix,
                CFBundleExecutable=executable, CFBundleName=executable,
                CFBundlePackageType='APPL', CFBundleVersion='1',
                CFBundleShortVersionString='0.1', NSHighResolutionCapable=True,
                NSPrincipalClass='NSApplication', LSMinimumSystemVersion='14.5')
    if helper:
        info['LSUIElement'] = True
    (path / 'Info.plist').write_bytes(plistlib.dumps(info))
    (path / 'PkgInfo').write_bytes(b'APPL????')


def parse_identities(listing):
    """(SHA-1, common name) pairs from `security find-identity -p codesigning` text."""
    found = {}
    for line in listing.splitlines():
        match = re.match(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]*)"', line)
        if match:
            found.setdefault(match.group(1).upper(), match.group(2))
    return list(found.items())


def resolve_identity(spec, valid, every=None):
    """Map a certificate common name or SHA-1 to one (SHA-1, name) that can sign.

    `valid` and `every` are `find-identity` outputs with and without `-v`; the second
    only sharpens the error when the certificate exists but is not trusted for code
    signing. The hash, never the name, is what codesign receives, so two certificates
    with the same name cannot be confused.
    """
    spec = (spec or '').strip()
    if not spec:
        raise RuntimeError('FAIL: ' + IDENTITY_ENV + ' is empty; unset it for ad hoc signing')
    pool = parse_identities(valid)
    if re.fullmatch(r'[0-9A-Fa-f]{40}', spec):
        matches = [item for item in pool if item[0] == spec.upper()]
    else:
        matches = [item for item in pool if item[1] == spec]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError('FAIL: ' + IDENTITY_ENV + ' matches several identities; use one SHA-1: '
                           + ', '.join(sorted(h for h, _ in matches)))
    listed = parse_identities(every or '')
    if any(h == spec.upper() or n == spec for h, n in listed):
        raise RuntimeError('FAIL: identity "' + spec + '" exists but is not valid for code signing '
                           '(untrusted, expired or missing its private key); in Keychain Access set '
                           'the certificate trust for Code Signing to Always Trust')
    raise RuntimeError('FAIL: no valid code-signing identity "' + spec + '" in `security '
                       'find-identity -v -p codesigning`')


def current_signing(env=None, runner=subprocess.run):
    """Signing mode for this build. Read-only: it never creates or changes a certificate."""
    spec = (os.environ if env is None else env).get(IDENTITY_ENV)
    if spec is None:
        return dict(mode='adhoc', identity=None, name=None)
    def listing(*flags):
        done = runner([SECURITY, 'find-identity', *flags, '-p', 'codesigning'],
                      capture_output=True, text=True, timeout=30)
        return done.stdout
    valid = listing('-v')
    try:
        digest_, name = resolve_identity(spec, valid)
    except RuntimeError:
        # Distinguish "untrusted" from "absent" for the error message only.
        resolve_identity(spec, valid, listing())
        raise
    return dict(mode='identity', identity=digest_, name=name)


def signing_label(signing):
    if signing['mode'] == 'adhoc':
        return 'adhoc'
    return 'identity:' + signing['identity']


def designated_requirement(identifier, identity_hash):
    """Stable across rebuilds: bundle identifier plus this exact certificate leaf."""
    return f'designated => identifier "{identifier}" and certificate leaf = H"{identity_hash.lower()}"'


def sign_command(target, signing, identifier=None):
    """One codesign invocation; ad hoc keeps the historical bare form."""
    if signing['mode'] == 'adhoc':
        return ['codesign', '--force', '--sign', '-', target]
    command = ['codesign', '--force', '--sign', signing['identity'], '--timestamp=none']
    if identifier:
        command.append('-r=' + designated_requirement(identifier, signing['identity']))
    return command + [target]


def bundle_identifier(bundle):
    return plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())['CFBundleIdentifier']


def fingerprint(signing=None):
    h = hashlib.sha256()
    h.update(SHA256.encode())
    for name in ['host.mm', 'stream.inc', 'input.inc', 'transport.hpp', 'engine_surface_v1.h',
                 'surface_transport.hpp', 'surface_transport.mm', 'crash_guard.hpp',
                 'a11y.inc', 'a11y_forward.inc', 'a11y_core.hpp']:
        h.update((ROOT / 'native/chromium-host' / name).read_bytes())
    # Runner-only lifecycle changes do not change the native build recipe.
    h.update(inspect.getsource(setup).encode())
    h.update(inspect.getsource(plist).encode())
    h.update(inspect.getsource(sign_command).encode())
    # A different identity (or ad hoc) must re-sign; the SHA-1 is a public cert digest.
    h.update(signing_label(signing or current_signing()).encode())
    h.update(subprocess.check_output(['xcrun', 'clang++', '--version']))
    return h.hexdigest()


def setup():
    run_command([MOUNT])
    host_check()
    signing = current_signing()
    if signing['mode'] == 'adhoc':
        print(ADHOC_HINT, file=sys.stderr, flush=True)
    if not BUILD_ROOT.exists():
        raise RuntimeError('BLOCKED_ENV: AXIOSOZO_BUILD_ROOT is not ready')
    BASE.mkdir(parents=True, exist_ok=True)
    if not ARCHIVE.exists() or ARCHIVE.stat().st_size != ARCHIVE_BYTES:
        run_command(['curl', '--fail', '--location', '--max-time', '180',
                     '--speed-time', '20', '--speed-limit', '1024', '--continue-at', '-',
                     URL, '--output', ARCHIVE])
    if digest(ARCHIVE) != SHA256:
        raise RuntimeError('FAIL: CEF archive SHA256 differs; never extracting it')
    # The verified upstream archive is immutable. Extraction has no lifecycle hooks.
    if not (CEF / 'libcef_dll/wrapper/cef_scoped_sandbox_context_mac.mm').exists():
        run_command([EXTERNAL, 'tar', '-xjf', ARCHIVE, '-C', BASE])
    stamp = BASE / 'build.json'
    identity = fingerprint(signing)
    if BINARY.exists() and stamp.exists() and json.loads(stamp.read_text()).get('fingerprint') == identity:
        emit('PASS', action='reused_build', binary=str(BINARY), signing=signing_label(signing))
        return
    plist(APP, 'AxioCEFProbe')
    resources = APP / 'Contents/Resources'
    resources.mkdir(exist_ok=True)
    for notice in ['LICENSE.txt', 'CREDITS.html']:
        shutil.copy2(CEF / notice, resources / notice)
    frameworks = APP / 'Contents/Frameworks'
    frameworks.mkdir(exist_ok=True)
    framework = frameworks / 'Chromium Embedded Framework.framework'
    # Clone on the external APFS volume where possible; avoid a second large copy.
    # Never keep a framework copied from a previous pin: the host's API hash and
    # version check would reject it at launch.
    if framework.exists():
        shutil.rmtree(framework)
    run_command(['cp', '-cR', CEF / 'Release/Chromium Embedded Framework.framework', framework])
    # Xcode 26 requires a versioned framework. Preserve official contents and notices.
    if not (framework / 'Versions/A').exists():
        entries = list(framework.iterdir())
        (framework / 'Versions/A').mkdir(parents=True)
        for entry in entries:
            entry.rename(framework / 'Versions/A' / entry.name)
            (framework / entry.name).symlink_to('Versions/A/' + entry.name)
        (framework / 'Versions/Current').symlink_to('A')
    # Upstream README explicitly permits shipping only the configured locale.
    # E0 uses CEF's default English locale; retain ICU and every non-locale asset.
    for locale in (framework / 'Versions/A/Resources').glob('*.lproj'):
        if locale.name != 'en.lproj':
            shutil.rmtree(locale)
    source = ROOT / 'native/chromium-host/host.mm'
    sandbox = CEF / 'libcef_dll/wrapper/cef_scoped_sandbox_context_mac.mm'
    for directory in [BASE / 'tmp', BASE / 'clang-cache']:
        directory.mkdir(exist_ok=True)
    common = [EXTERNAL, 'env', 'TMPDIR=' + str(BASE / 'tmp'),
              'CLANG_MODULE_CACHE_PATH=' + str(BASE / 'clang-cache'), 'xcrun', 'clang++', '-std=c++20', '-fobjc-arc',
              '-arch', 'arm64', '-mmacosx-version-min=14.5', '-DCEF_USE_SANDBOX=1', '-DCEF_API_VERSION=15600',
              '-I', CEF, '-framework', 'Cocoa', source]
    # Only the browser process links the Mach/IOSurface transport. Helpers compile
    # inert stubs (AXIO_CEF_HELPER) and never link Metal or IOSurface.
    run_command(common + [ROOT / 'native/chromium-host/surface_transport.mm', '-framework', 'Metal',
                          '-framework', 'IOSurface', '-lbsm', '-o', BINARY])
    helper_name = 'AxioCEFProbe Helper'
    helper = frameworks / (helper_name + '.app')
    plist(helper, helper_name, True, '.helper')
    run_command(common + ['-DAXIO_CEF_HELPER=1', sandbox, '-o', helper / 'Contents/MacOS' / helper_name])
    # Chromium requires these role-specific helper bundle names even with one codebase.
    for role in ['GPU', 'Renderer', 'Plugin', 'Alerts']:
        name = f'{helper_name} ({role})'
        bundle = frameworks / (name + '.app')
        plist(bundle, name, True, '.helper.' + role.lower())
        shutil.copy2(helper / 'Contents/MacOS' / helper_name, bundle / 'Contents/MacOS' / name)
    # Local development only: ad hoc, or a user-created self-signed identity that keeps
    # the Keychain ACL stable across rebuilds. No account, notarization or distribution.
    for lib in (framework / 'Versions/A/Libraries').glob('*.dylib'):
        run_command(sign_command(lib, signing))
    run_command(sign_command(framework, signing))
    for helper_bundle in sorted(frameworks.glob('*Helper*.app')):
        run_command(sign_command(helper_bundle, signing, bundle_identifier(helper_bundle)))
    run_command(sign_command(APP, signing, bundle_identifier(APP)))
    stamp.write_text(json.dumps(dict(fingerprint=identity, cef=VERSION, archive_sha256=SHA256,
                                     built_at=time.time(),
                                     signing=signing_label(signing),
                                     signing_name=signing['name']), indent=2) + '\n')
    emit('PASS', action='native_build', binary=str(BINARY), signing=signing_label(signing))


def check():
    host_check()
    if not BINARY.exists():
        raise RuntimeError('BLOCKED_ENV: CEF native component is not built; run ./dev setup')
    signing = current_signing()
    stamp = BASE / 'build.json'
    if not stamp.exists() or json.loads(stamp.read_text()).get('fingerprint') != fingerprint(signing):
        raise RuntimeError('BLOCKED_ENV: native CEF rebuild required (signing mode now '
                           + signing_label(signing) + ')')
    run_command(['codesign', '--verify', '--deep', '--strict', APP])
    # codesign prints the requirement on stdout; its other notes go to stderr.
    requirement = subprocess.run(['codesign', '--display', '--requirements', '-', APP],
                                 capture_output=True, text=True, timeout=60).stdout.strip()
    print(requirement, flush=True)
    if signing['mode'] == 'adhoc':
        print(ADHOC_HINT, file=sys.stderr, flush=True)
    emit('PASS', action='native_build_check', version=VERSION, signing=signing_label(signing),
         signing_name=signing['name'], designated_requirement=requirement)


def native_test():
    """Exercise actual native framing/credit threads, without pretending to run CEF."""
    host_check()
    BASE.mkdir(parents=True, exist_ok=True)
    for directory in [BASE / 'tmp', BASE / 'clang-cache']:
        directory.mkdir(exist_ok=True)
    binary = BASE / 'transport-test'
    run_command([EXTERNAL, 'env', 'TMPDIR=' + str(BASE / 'tmp'),
                 'CLANG_MODULE_CACHE_PATH=' + str(BASE / 'clang-cache'),
                 'xcrun', 'clang++', '-std=c++20', '-Wall', '-Wextra',
                 ROOT / 'native/chromium-host/test_transport.cc', '-o', binary])
    run_command([binary], timeout_seconds=15)
    # Crash/hang robustness (crash_guard.hpp) against a Breakpad-like inherited
    # exception port, in real child processes. No CEF, no Keychain.
    guard = BASE / 'crash-guard-test'
    run_command([EXTERNAL, 'env', 'TMPDIR=' + str(BASE / 'tmp'),
                 'CLANG_MODULE_CACHE_PATH=' + str(BASE / 'clang-cache'),
                 'xcrun', 'clang++', '-std=c++20', '-Wall', '-Wextra',
                 ROOT / 'native/chromium-host/tests/crash_guard_test.cc', '-o', guard])
    run_command([guard], timeout_seconds=30)
    # Accessibility mirror (a11y_core.hpp): tree, redaction, chunking, credits. No CEF.
    a11y = BASE / 'a11y-core-test'
    run_command([EXTERNAL, 'env', 'TMPDIR=' + str(BASE / 'tmp'),
                 'CLANG_MODULE_CACHE_PATH=' + str(BASE / 'clang-cache'),
                 'xcrun', 'clang++', '-std=c++20', '-Wall', '-Wextra',
                 '-I', ROOT / 'native/chromium-host',
                 ROOT / 'native/chromium-host/tests/a11y_core_test.cc', '-o', a11y])
    run_command([a11y], timeout_seconds=30)


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Only one synthetic file; never directory serving, HEAD, or proxy requests."""
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def record_request(self, status):
        # Record only known synthetic URL/Host values; rejected input is not logged.
        hosts = self.headers.get_all('Host', [])
        item = dict(timestamp=time.time(), method=self.command, status=status,
                    path=self.path if self.path in ['/engine.html', '/engine.html?page=2'] else 'REJECTED_PATH',
                    host=hosts[0] if hosts == [f'127.0.0.1:{self.server.server_port}'] else 'REJECTED_HOST')
        log = getattr(self.server, 'request_log', None)
        if log:
            with log.open('a') as out:
                out.write(json.dumps(item) + '\n')

    def do_GET(self):
        hosts = self.headers.get_all('Host', [])
        if hosts != [f'127.0.0.1:{self.server.server_port}']:
            self.record_request(403)
            self.send_error(403)
            return
        if self.path not in ['/engine.html', '/engine.html?page=2']:
            self.record_request(404)
            self.send_error(404)
            return
        data = (ROOT / 'tests/fixtures/engine.html').read_bytes()
        self.record_request(200)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def denied_method(self):
        self.record_request(405)
        self.send_error(405)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_TRACE = denied_method


def archive_previous_run():
    """Keep actual prior logs/frames immutable when the root re-runs the probe."""
    if not (EVIDENCE / 'cef-probe-result.json').exists():
        return
    archive = Path(tempfile.mkdtemp(prefix='cef-run-', dir=EVIDENCE))
    names = ['cef-e0.jsonl', 'cef-stderr.log', 'cef-runtime.log',
             'cef-probe-result.json', 'cef-launch-sample.txt',
             'cef-fixture-requests.jsonl', 'cef-owned-helpers.txt']
    paths = [EVIDENCE / name for name in names]
    paths += list(EVIDENCE.glob('cef-e0-*.png'))
    for path in paths:
        if path.is_file():
            shutil.copy2(path, archive / path.name)
    # A failed next run must not present a previous run's PNG as fresh evidence.
    for path in paths:
        if path.is_file():
            path.unlink()
    emit('ARCHIVED_EVIDENCE', path=str(archive))


def cleanup_group(process):
    """The group is session-owned even when the original parent already exited."""
    if process is None:
        return False
    def exists():
        process.poll()  # Reap the owned parent if it has exited.
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
    remained = exists()
    if remained:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.wait(timeout=4)
    return remained


def remove_created_profile(profile, process, prefix='cef-profile-'):
    """Remove only this invocation's mkdtemp directory after its owned group exits."""
    if profile.parent != BASE or not profile.name.startswith(prefix) or profile.is_symlink():
        return False
    if process is not None:
        if process.poll() is None:
            return False
        deadline = time.monotonic() + 2
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(.1)
    try:
        shutil.rmtree(profile)
    except OSError:
        return False
    return not profile.exists()


def diagnose_unpainted(process, stop, output):
    """One bounded sample of this native session, only when no paint arrived."""
    if stop.wait(5) or process.poll() is not None:
        return
    output.flush()
    if 'native_frame' in (EVIDENCE / 'cef-e0.jsonl').read_text():
        return
    try:
        group = subprocess.run(['pgrep', '-g', str(process.pid)], capture_output=True,
                               text=True, timeout=3)
        pids = [int(value) for value in group.stdout.split()]
        if not pids:
            return
        rows = subprocess.run(['ps', '-p', ','.join(map(str, pids)), '-o',
                               'pid=,ppid=,pgid=,stat=,etime=,command='],
                              capture_output=True, text=True, timeout=3)
        (EVIDENCE / 'cef-owned-helpers.txt').write_text(rows.stdout)
        for pid in pids[:5]:
            if stop.is_set():
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run(['/usr/bin/sample', str(pid), '1', '1', '-file',
                                str(EVIDENCE / f'cef-helper-{pid}.sample.txt')],
                               capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        (EVIDENCE / 'cef-helper-diagnostic-error.txt').write_text(type(error).__name__ + '\n')


def run():
    check()
    EVIDENCE.mkdir(exist_ok=True)
    archive_previous_run()
    # A unique, non-personal profile on every component run. No global browser process lookup.
    profile = Path(tempfile.mkdtemp(prefix='cef-profile-', dir=BASE))
    os.chmod(profile, 0o700)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    server.request_log = EVIDENCE / 'cef-fixture-requests.jsonl'
    server.request_log.write_text('')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.time()
    code = 75
    process = None
    group_cleanup_required = False
    timed_out = False
    diagnostic_stop = threading.Event()
    diagnostic = None
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        url = f'http://127.0.0.1:{server.server_port}/engine.html'
        with (EVIDENCE / 'cef-e0.jsonl').open('w') as out, (EVIDENCE / 'cef-stderr.log').open('w') as err:
            process = subprocess.Popen([str(BINARY), url, str(profile), str(EVIDENCE)],
                                       stdout=out, stderr=err, start_new_session=True,
                                       env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                                            'TMPDIR':str(profile)})
            emit('RUNNING', native_pid=process.pid, profile=str(profile))
            diagnostic = threading.Thread(target=diagnose_unpainted,
                                          args=(process, diagnostic_stop, out), daemon=True)
            diagnostic.start()
            try:
                code = process.wait(timeout=95)
            except subprocess.TimeoutExpired:
                timed_out = True
                code = 75
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    subprocess.run(['/usr/bin/sample', str(process.pid), '1', '1',
                                    '-file', str(EVIDENCE / 'cef-launch-sample.txt')],
                                   timeout=5, capture_output=True)
    except KeyboardInterrupt:
        code = 130
    finally:
        # Ignore another termination signal only during bounded owned-process cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        diagnostic_stop.set()
        if diagnostic:
            diagnostic.join(timeout=8)
        group_cleanup_required = cleanup_group(process)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    profile_removed = remove_created_profile(profile, process)
    if not profile_removed and code == 0:
        code = 76
    records = []
    log = EVIDENCE / 'cef-e0.jsonl'
    for line in (log.read_text() if log.exists() else '').splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            records.append(json.loads(line))
    status = 'PASS' if code == 0 and any(x.get('event') == 'result' and x.get('E0') == 'PASS' for x in records) else 'FAIL'
    if timed_out and not records:
        status = 'BLOCKED_ENV'
    result = dict(E0=status, E1='BLOCKED_ENV', E2='BLOCKED_ENV', exit_code=code,
                  elapsed_seconds=round(time.time()-started,3), platform=platform.platform(),
                  cef=VERSION, archive_sha256=SHA256, profile=str(profile),
                  native_pid=process.pid if process else None,
                  profile_removed=profile_removed,
                  fixture_requests=[json.loads(line) for line in server.request_log.read_text().splitlines()],
                  timed_out=timed_out, group_cleanup_required=group_cleanup_required,
                  reason='Native launch timed out before any host event' if timed_out and not records else
                         'Native fixture rendering/input failed; inspect load events and fixture requests' if status != 'PASS' else
                         'CEF component only; Zen embedding and user switching are separate unverified gates')
    (EVIDENCE / 'cef-probe-result.json').write_text(json.dumps(result, indent=2)+'\n')
    emit(status, **result)
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'check', 'run', 'test-native'])
    args = parser.parse_args()
    try:
        sys.exit({'setup':setup, 'check':check, 'run':run, 'test-native':native_test}[args.command]() or 0)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        emit('BLOCKED_ENV' if 'BLOCKED_ENV' in str(exc) else 'FAIL', message=str(exc))
        sys.exit(2)
