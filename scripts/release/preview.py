#!/usr/bin/env python3
"""AxioSozo Preview DMG pipeline (handoff 3, F7): prepared locally, never published.

Steps: source gate (clean tree, exact commit) -> locate app -> preflight (branding,
bundle identity, packaging, notices, updater, Safe Browsing, Chromium default) ->
stage on the external build volume -> ship notices -> inside-out hardened-runtime
codesign -> verify -> DMG -> [notarize + staple, only with --notarize --authorized]
-> checksums -> Preview release notes.

Nothing is uploaded, pushed, tagged or published. Without --notarize and
--authorized this script never contacts Apple: signatures use --timestamp=none
and the result is a local verification artifact only. It never lists or reads
keychain identities; the identity string is an input.

Exit codes: 0 notarized Preview prepared (still a Preview, never READY);
1 unexpected failure; 2 usage; 3 source refused (dirty tree / commit mismatch);
4 preflight blocked; 5 BLOCKED_ENV; 10 BLOCKED_SIGNING_IDENTITY;
11 signed but not notarized (notarization not requested/authorized);
12 signature, notarization or verification failed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'scripts'))
import artifacts  # noqa: E402
import bundle_checks  # noqa: E402
import signing_plan  # noqa: E402
import storage  # noqa: E402  (read-only use: BUILD_ROOT and mounted())

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2
EXIT_SOURCE_REFUSED, EXIT_PREFLIGHT_BLOCKED, EXIT_BLOCKED_ENV = 3, 4, 5
EXIT_BLOCKED_SIGNING_IDENTITY, EXIT_NOT_NOTARIZED, EXIT_VERIFY_FAILED = 10, 11, 12
CATEGORY_EXIT = {'source': EXIT_SOURCE_REFUSED, 'env': EXIT_BLOCKED_ENV, 'preflight': EXIT_PREFLIGHT_BLOCKED,
                 'signing': EXIT_BLOCKED_SIGNING_IDENTITY, 'notarization': EXIT_NOT_NOTARIZED,
                 'verify': EXIT_VERIFY_FAILED}
PRIORITY = ('source', 'env', 'preflight', 'verify', 'signing', 'notarization')
STATUS = {EXIT_OK: 'PREVIEW_PREPARED_NOTARIZED', EXIT_SOURCE_REFUSED: 'SOURCE_REFUSED',
          EXIT_PREFLIGHT_BLOCKED: 'PREFLIGHT_BLOCKED', EXIT_BLOCKED_ENV: 'BLOCKED_ENV',
          EXIT_BLOCKED_SIGNING_IDENTITY: 'BLOCKED_SIGNING_IDENTITY',
          EXIT_NOT_NOTARIZED: 'PREPARED_NOT_NOTARIZED', EXIT_VERIFY_FAILED: 'VERIFY_FAILED'}
IDENTITY_ENV, TEAM_ENV, PROFILE_ENV = 'AXIOSOZO_SIGN_IDENTITY', 'AXIOSOZO_TEAM_ID', 'AXIOSOZO_NOTARY_PROFILE'
IDENTITY_FORMAT = re.compile(r'^Developer ID Application: .+ \(([A-Z0-9]{10})\)$')
TEAM_FORMAT = re.compile(r'^[A-Z0-9]{10}$')
PROFILE_FORMAT = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
CEF_DEFAULT = storage.BUILD_ROOT / 'cef/AxioCEFProbe.app'
SLOW = {'notarytool': 3600, 'hdiutil': 1800, 'codesign': 1800, 'ditto': 1800}


def execute(argv, capture=False):
    """The only place external tools run. Tests replace RUNNER."""
    print('+ ' + shlex.join(map(str, argv)), flush=True)
    timeout = next((value for key, value in SLOW.items() if any(key in str(arg) for arg in argv[:3])), 600)
    result = subprocess.run(list(map(str, argv)), capture_output=capture, text=True, timeout=timeout)
    return result.returncode, (result.stdout or '') + (result.stderr or '') if capture else ''


RUNNER = execute


class Report:
    def __init__(self, dry_run):
        self.data = {'label': 'Preview', 'ready': False, 'dry_run': dry_run, 'network': 'none',
                     'steps': [], 'blockers': [], 'warnings': [], 'info': []}

    def step(self, name, status, **fields):
        self.data['steps'].append({'step': name, 'status': status, **fields})

    def block(self, category, code, detail, **extra):
        self.data['blockers'].append({'category': category, 'code': code, 'detail': detail, **extra})

    def absorb(self, findings):
        for item in findings:
            if item['level'] == 'blocker':
                self.block('preflight', item['code'], item['detail'],
                           **{k: v for k, v in item.items() if k not in ('level', 'code', 'detail')})
            else:
                self.data['warnings' if item['level'] == 'warning' else 'info'].append(item)

    def exit_code(self):
        categories = {item['category'] for item in self.data['blockers']}
        for category in PRIORITY:
            if category in categories:
                return CATEGORY_EXIT[category]
        return EXIT_OK

    def blocked(self, *categories):
        return any(item['category'] in categories for item in self.data['blockers'])


def git(repo, *args):
    return subprocess.run(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null', *args],
                          cwd=repo, capture_output=True, text=True, timeout=60)


def source_state(repo, expect_commit=None):
    head = git(repo, 'rev-parse', '--verify', 'HEAD^{commit}')
    status = git(repo, 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
    branch = git(repo, 'rev-parse', '--abbrev-ref', 'HEAD')
    state = {'commit': head.stdout.strip() if head.returncode == 0 else None,
             'branch': branch.stdout.strip() if branch.returncode == 0 else None,
             'clean': head.returncode == 0 and status.returncode == 0 and not status.stdout.strip(),
             'dirty': [line for line in status.stdout.splitlines() if line.strip()][:25]}
    state['dirty_count'] = len([line for line in status.stdout.splitlines() if line.strip()])
    state['commit_matches'] = expect_commit is None or state['commit'] == expect_commit
    return state


def signing_inputs(args, report):
    identity = args.identity or os.environ.get(IDENTITY_ENV) or None
    team = args.team_id or os.environ.get(TEAM_ENV) or None
    profile = args.notary_profile or os.environ.get(PROFILE_ENV) or None
    if not identity:
        report.block('signing', 'BLOCKED_SIGNING_IDENTITY',
                     f'no Developer ID Application identity (--identity or {IDENTITY_ENV}); '
                     'open decision #3 (Apple Developer account) is unresolved')
    else:
        match = IDENTITY_FORMAT.match(identity)
        if not match:
            report.block('signing', 'SIGNING_IDENTITY_INVALID',
                         'identity must be "Developer ID Application: NAME (TEAMID)"; ad hoc and other '
                         'certificate types cannot be notarized')
        elif team and match.group(1) != team:
            report.block('signing', 'TEAM_ID_MISMATCH', 'team ID does not match the identity suffix')
    if identity and not team:
        report.block('signing', 'TEAM_ID_MISSING', f'--team-id or {TEAM_ENV} required')
    elif team and not TEAM_FORMAT.match(team):
        report.block('signing', 'TEAM_ID_INVALID', 'team ID must be 10 uppercase letters/digits')
    if args.notarize and not args.authorized:
        report.block('notarization', 'NOTARIZATION_NOT_AUTHORIZED',
                     '--notarize also needs --authorized (Wout\'s explicit per-release authorization)')
    elif args.notarize and not (profile and PROFILE_FORMAT.match(profile)):
        report.block('notarization', 'NOTARY_PROFILE_MISSING',
                     f'--notary-profile or {PROFILE_ENV}: a profile created by Wout with '
                     '`xcrun notarytool store-credentials`')
    elif not args.notarize:
        report.block('notarization', 'NOTARIZATION_NOT_REQUESTED',
                     'without --notarize --authorized the DMG is a local verification artifact only')
    return {'identity': identity, 'team': team, 'profile': profile,
            'network': bool(args.notarize and args.authorized)}


def locate_app(repo, explicit, report):
    if explicit:
        app = Path(explicit).absolute()
        if app.suffix != '.app' or not (app / 'Contents/Info.plist').is_file():
            report.block('env', 'APP_NOT_FOUND', f'{app} is not an .app bundle')
            return None
        report.step('locate_app', 'PASS', app=str(app), provenance='EXPLICIT_PATH_UNVERIFIED')
        return app
    result = subprocess.run([sys.executable, str(Path(repo) / 'scripts/zen.py'), 'describe'],
                            cwd=repo, capture_output=True, text=True, timeout=120,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        described = json.loads(result.stdout)
    except ValueError:
        described = {'status': 'BLOCKED_ENV', 'error': (result.stderr or result.stdout)[-400:]}
    if described.get('status') != 'PASS':
        report.block('env', 'APP_NOT_FOUND', 'scripts/zen.py describe: ' + json.dumps(described))
        report.step('locate_app', 'BLOCKED', describe=described)
        return None
    app = Path(described['executable']).parents[2]
    report.step('locate_app', 'PASS', app=str(app), provenance='scripts/zen.py describe',
                fingerprint=described.get('fingerprint'), bundle_id=described.get('bundle_id'))
    return app


def stage_root_problem(path):
    resolved = Path(path).resolve()
    if not str(resolved).startswith('/Volumes/'):
        return f'{resolved} is not on external storage under /Volumes'
    for forbidden in {Path('/tmp').resolve(), Path('/private/var/folders'), Path.home().resolve()}:
        if resolved == forbidden or forbidden in resolved.parents:
            return f'{resolved} is inside {forbidden}'
    return None


def filesystem_type(path):
    """Filesystem of the mount containing `path` (from /sbin/mount; read-only)."""
    resolved = str(Path(path).resolve())
    output = subprocess.run(['/sbin/mount'], capture_output=True, text=True, timeout=30).stdout
    best, kind = '', None
    for line in output.splitlines():
        match = re.match(r'^.+ on (.+) \(([A-Za-z0-9_-]+)', line)
        if match:
            mountpoint = match.group(1)
            if (resolved == mountpoint or resolved.startswith(mountpoint.rstrip('/') + '/')) and len(mountpoint) > len(best):
                best, kind = mountpoint, match.group(2)
    return kind


def cef_source(args):
    if args.cef_app == 'none':
        return None
    if args.cef_app:
        return Path(args.cef_app).absolute()
    return CEF_DEFAULT if CEF_DEFAULT.is_dir() else None


def planned_commands(ctx):
    """Every command of a real run, in order, for printing (dry-run) and the report."""
    stage, app, dmg_root, dmg = ctx['stage'], ctx['stage_app'], ctx['dmg_root'], ctx['dmg']
    sign, profile = ctx['identity'] or signing_plan.IDENTITY_PLACEHOLDER, ctx['profile'] or '${AXIOSOZO_NOTARY_PROFILE}'
    timestamp = '--timestamp' if ctx['network'] else '--timestamp=none'
    commands = [('stage', 'none', ['/usr/bin/ditto', str(ctx['source_app']), str(app)])]
    if ctx['cef']:
        commands.append(('stage', 'none', ['/usr/bin/ditto', str(ctx['cef']), str(app / bundle_checks.CEF_EMBED)]))
    commands.append(('stage', 'none', ['/usr/bin/xattr', '-cr', str(app)]))
    commands.append(('notices', 'none', ['(write)', str(app / bundle_checks.NOTICE_DIR), 'LICENSE',
                                         'THIRD_PARTY_NOTICES.md', 'PREVIEW-NOTICE.txt']))
    commands += [('codesign', 'identity', step['command']) for step in ctx['codesign']]
    commands += [('verify', 'identity', ['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)]),
                 ('verify', 'identity', ['/usr/bin/codesign', '--display', '--verbose=2', '(each signed item)']),
                 ('verify', 'identity', ['/usr/bin/codesign', '--display', '--entitlements', '-', '--xml', str(app)])]
    if ctx['network']:
        zip_path = stage / 'notarize-app.zip'
        commands += [('notarize', 'identity+authorized', ['/usr/bin/ditto', '-c', '-k', '--keepParent', str(app), str(zip_path)]),
                     ('notarize', 'identity+authorized', ['/usr/bin/xcrun', 'notarytool', 'submit', str(zip_path),
                                                          '--keychain-profile', profile, '--wait', '--output-format', 'json']),
                     ('notarize', 'identity+authorized', ['/usr/bin/xcrun', 'stapler', 'staple', str(app)]),
                     ('notarize', 'identity+authorized', ['/usr/bin/xcrun', 'stapler', 'validate', str(app)])]
    commands += [('dmg', 'identity', ['/bin/ln', '-s', '/Applications', str(dmg_root / 'Applications')]),
                 ('dmg', 'identity', ['/usr/bin/hdiutil', 'create', '-volname', ctx['volume_name'], '-srcfolder',
                                      str(dmg_root), '-fs', 'HFS+', '-format', 'UDZO', str(dmg)]),
                 ('dmg', 'identity', ['/usr/bin/codesign', '--force', '--sign', sign, timestamp, str(dmg)]),
                 ('verify', 'identity', ['/usr/bin/hdiutil', 'verify', str(dmg)]),
                 ('verify', 'identity', ['/usr/bin/codesign', '--verify', '--strict', '--verbose=2', str(dmg)])]
    if ctx['network']:
        commands += [('notarize', 'identity+authorized', ['/usr/bin/xcrun', 'notarytool', 'submit', str(dmg),
                                                          '--keychain-profile', profile, '--wait', '--output-format', 'json']),
                     ('notarize', 'identity+authorized', ['/usr/bin/xcrun', 'stapler', 'staple', str(dmg)]),
                     ('notarize', 'identity+authorized', ['/usr/bin/xcrun', 'stapler', 'validate', str(dmg)]),
                     ('verify', 'identity+authorized', ['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', str(app)]),
                     ('verify', 'identity+authorized', ['/usr/sbin/spctl', '--assess', '--type', 'open', '--context',
                                                        'context:primary-signature', '--verbose=4', str(dmg)])]
    commands += [('checksums', 'identity', ['(write)', str(stage / 'SHA256SUMS.txt')]),
                 ('notes', 'none', ['(write)', str(stage / 'RELEASE_NOTES.md')])]
    return commands


def preflight(report, app, cef, repo, distribution=False):
    brand = bundle_checks.branding(app, repo=repo)
    report.absorb(brand['findings'])
    report.step('branding', 'BLOCKED' if any(f['level'] == 'blocker' for f in brand['findings']) else 'PASS',
                bundle_id=brand['bundle_id'], version=brand['version'], profile_root=brand.get('profile_root'))
    packaged = bundle_checks.packaging(app)
    report.absorb(packaged['findings'])
    report.step('packaging', 'BLOCKED' if packaged['findings'] else 'PASS',
                external_symlinks=packaged['external_symlinks'])
    if cef:
        if not (cef / 'Contents/Info.plist').is_file():
            report.block('env', 'CEF_APP_NOT_FOUND', str(cef))
        else:
            cef_packaging = bundle_checks.packaging(cef)
            report.absorb(cef_packaging['findings'])
    updater = bundle_checks.updater(app)
    report.absorb(updater['findings'])
    report.step('updater', updater['state'], channel=updater['channel'], source=updater['source'],
                flags=updater['flags'], notice='manual updates; own MAR keys and update host not built')
    # Only a notarized build is distributable; it must ship Safe Browsing.
    safe = bundle_checks.safe_browsing(app, distribution=distribution)
    report.absorb(safe['findings'])
    report.step('safe_browsing', safe['state'], source=safe['source'],
                label=artifacts.safe_browsing_sentence(safe['state']))
    chromium = bundle_checks.chromium(app, repo=repo)
    chromium['cef_embedded'] = bool(cef)
    chromium['label'] = ('EXPERIMENTAL: embedded, but the current CEF adapter only launches the host from a '
                         './dev development session, so Chromium mode is unavailable in this build' if cef
                         else 'NOT_INCLUDED: Chromium mode is unavailable in this build')
    report.absorb(chromium['findings'])
    report.step('chromium', chromium['default'], cef_embedded=chromium['cef_embedded'],
                source_gate=chromium['source_gate'], label=chromium['label'])
    present = bundle_checks.notices(app)['present']
    upstream = {name: ok for name, ok in present.items() if not name.startswith(bundle_checks.NOTICE_DIR)}
    for name, ok in upstream.items():
        if not ok:
            report.block('preflight', 'NOTICE_MISSING', name)
    if cef:
        for name in ('LICENSE.txt', 'CREDITS.html'):
            if not (cef / 'Contents/Resources' / name).is_file():
                report.block('preflight', 'NOTICE_MISSING', f'CEF Contents/Resources/{name}')
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        if not (Path(repo) / name).is_file():
            report.block('preflight', 'NOTICE_MISSING', f'repository {name}')
    report.step('notices_source', 'PASS' if all(upstream.values()) else 'BLOCKED', present=upstream,
                shipped_at_staging=[f'{bundle_checks.NOTICE_DIR}/{name}'
                                    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'PREVIEW-NOTICE.txt')])
    return brand, safe, chromium


def stage_bundle(ctx, repo, report, context):
    app = ctx['stage_app']
    ctx['dmg_root'].mkdir(parents=True)
    for label, needs, command in ctx['commands']:
        if label != 'stage':
            continue
        code, _ = RUNNER(command)
        if code:
            report.block('env', 'STAGE_FAILED', shlex.join(command))
            return False
    notices_dir = app / bundle_checks.NOTICE_DIR
    notices_dir.mkdir(parents=True, exist_ok=False)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(Path(repo) / name, notices_dir / name)
    notice = artifacts.preview_notice(context)
    (notices_dir / 'PREVIEW-NOTICE.txt').write_text(notice)
    (ctx['dmg_root'] / 'READ ME - AxioSozo Preview.txt').write_text(notice)
    shutil.copytree(signing_plan.ENTITLEMENTS_DIR, ctx['stage'] / 'entitlements',
                    ignore=shutil.ignore_patterns('._*'))
    # Re-inspect exactly what will be signed.
    cef_rel = bundle_checks.CEF_EMBED if ctx['cef'] else None
    staged_notices = bundle_checks.notices(app, cef_rel)
    report.absorb(staged_notices['findings'])
    report.step('notices_staged', 'BLOCKED' if staged_notices['findings'] else 'PASS', present=staged_notices['present'])
    for check in (bundle_checks.packaging(app), bundle_checks.branding(app, cef_rel=cef_rel, repo=repo)):
        report.absorb([item for item in check['findings'] if item['level'] == 'blocker'])
    report.step('stage', 'BLOCKED' if report.blocked('preflight') else 'PASS', path=str(app))
    return not report.blocked('preflight', 'env')


def sign_and_verify(ctx, report):
    for step in signing_plan.plan(ctx['stage_app'], ctx['identity'], ctx['network'],
                                  entitlements_dir=ctx['stage'] / 'entitlements'):
        code, output = RUNNER(step['command'], capture=True)
        if code:
            report.block('verify', 'CODESIGN_FAILED', step['relative'], output=output[-800:])
            return False
        ctx['signed'].append(step['relative'])
    report.step('codesign', 'PASS', items=len(ctx['signed']))
    app = ctx['stage_app']
    code, output = RUNNER(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)], capture=True)
    if code:
        report.block('verify', 'CODESIGN_VERIFY_FAILED', output[-800:])
        return False
    for relative in ctx['signed']:
        target = app if relative == '.' else app / relative
        code, output = RUNNER(['/usr/bin/codesign', '--display', '--verbose=2', str(target)], capture=True)
        if code or 'runtime' not in output or f'TeamIdentifier={ctx["team"]}' not in output:
            report.block('verify', 'HARDENED_RUNTIME_OR_TEAM_MISSING', relative, output=output[-400:])
            return False
    code, output = RUNNER(['/usr/bin/codesign', '--display', '--entitlements', '-', '--xml', str(app)], capture=True)
    if code or 'com.apple.security.cs.allow-jit' not in output:
        report.block('verify', 'MAIN_APP_JIT_ENTITLEMENT_MISSING', output[-400:])
        return False
    report.step('verify_signatures', 'PASS', items=len(ctx['signed']))
    return True


def notarize(path, ctx, report, name):
    code, output = RUNNER(['/usr/bin/xcrun', 'notarytool', 'submit', str(path), '--keychain-profile',
                           ctx['profile'], '--wait', '--output-format', 'json'], capture=True)
    (ctx['stage'] / f'notary-{name}.json').write_text(output)
    try:
        status = json.loads(output[output.index('{'):]).get('status')
    except ValueError:
        status = None
    if code or status != 'Accepted':
        report.block('verify', 'NOTARIZATION_FAILED', f'{name}: status={status!r}', output=output[-800:])
        return False
    for action in ('staple', 'validate'):
        code, output = RUNNER(['/usr/bin/xcrun', 'stapler', action, str(ctx['stage_app'] if name == 'app' else path)],
                              capture=True)
        if code:
            report.block('verify', 'STAPLER_FAILED', f'{name} {action}', output=output[-400:])
            return False
    report.step(f'notarize_{name}', 'PASS')
    return True


def build_dmg(ctx, report):
    dmg = ctx['dmg']
    os.symlink('/Applications', ctx['dmg_root'] / 'Applications')
    for command in (['/usr/bin/hdiutil', 'create', '-volname', ctx['volume_name'], '-srcfolder', str(ctx['dmg_root']),
                     '-fs', 'HFS+', '-format', 'UDZO', str(dmg)],
                    ['/usr/bin/codesign', '--force', '--sign', ctx['identity'],
                     '--timestamp' if ctx['network'] else '--timestamp=none', str(dmg)],
                    ['/usr/bin/hdiutil', 'verify', str(dmg)],
                    ['/usr/bin/codesign', '--verify', '--strict', '--verbose=2', str(dmg)]):
        code, output = RUNNER(command, capture=True)
        if code:
            report.block('verify', 'DMG_STEP_FAILED', shlex.join(command), output=output[-800:])
            return False
    report.step('dmg', 'PASS', path=str(dmg))
    return True


def gatekeeper(ctx, report):
    for command in (['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', str(ctx['stage_app'])],
                    ['/usr/sbin/spctl', '--assess', '--type', 'open', '--context', 'context:primary-signature',
                     '--verbose=4', str(ctx['dmg'])]):
        code, output = RUNNER(command, capture=True)
        if code:
            report.block('verify', 'GATEKEEPER_REJECTED', shlex.join(command), output=output[-400:])
            return False
    report.step('gatekeeper', 'PASS')
    return True


def print_plan(ctx, report):
    print('AxioSozo Preview release plan (label: Preview; never READY)')
    print(f'  source commit: {ctx["commit"]}  app: {ctx["source_app"]}')
    print(f'  stage: {ctx["stage"]}')
    print(f'  network: {"Apple timestamp + notarytool (authorized)" if ctx["network"] else "none"}')
    for number, (label, needs, command) in enumerate(ctx['commands'], 1):
        print(f'{number:4d}. [{label}; needs {needs}] {shlex.join(map(str, command))}')
    print('  codesign entitlement provenance:')
    for step in ctx['codesign']:
        print(f'       {step["relative"]}: {step["entitlements"] or "(none)"} <- {step["provenance"]}')


def finish(report, args, stage=None):
    code = report.exit_code()
    report.data['exit'] = code
    report.data['status'] = STATUS.get(code, 'FAILED')
    if stage is not None and stage.is_dir():
        (stage / 'release-report.json').write_text(json.dumps(report.data, indent=2) + '\n')
    for item in report.data['blockers']:
        print(f'BLOCKER [{item["category"]}] {item["code"]}: {item["detail"]}', flush=True)
    for item in report.data['warnings']:
        print(f'WARNING {item["code"]}: {item["detail"]}', flush=True)
    if args.json:
        print(json.dumps(report.data, indent=2))
    print(f'RESULT: {report.data["status"]} exit={code} (Preview; not READY)', flush=True)
    return code


def run(args, repo=ROOT):
    report = Report(args.dry_run)
    source = source_state(repo, args.expect_commit)
    report.step('source', 'PASS' if source['clean'] and source['commit_matches'] else 'REFUSED', **source)
    if not source['commit']:
        report.block('source', 'NO_COMMIT', 'repository has no HEAD commit')
    if not source['clean']:
        report.block('source', 'DIRTY_TREE', f'{source["dirty_count"]} modified/untracked paths; '
                     'release only from a clean tree', paths=source['dirty'])
    if not source['commit_matches']:
        report.block('source', 'COMMIT_MISMATCH', f'HEAD {source["commit"]} != --expect-commit {args.expect_commit}')
    if report.blocked('source') and not args.dry_run:
        return finish(report, args)  # refuse before any file is written
    signing = signing_inputs(args, report)
    report.data['network'] = 'apple timestamp + notarytool (authorized)' if signing['network'] else 'none'
    app = locate_app(repo, args.app, report)
    if app is None:
        return finish(report, args)
    cef = cef_source(args)
    brand, safe, chromium = preflight(report, app, cef, repo, distribution=args.notarize)
    version = re.sub(r'[^A-Za-z0-9._-]', '_', brand['version'] or 'unknown')
    commit = source['commit'] or 'unknown'
    stage_root = Path(args.stage_root) if args.stage_root else storage.BUILD_ROOT / 'release'
    problem = stage_root_problem(stage_root)
    if problem:
        report.block('env', 'STAGE_ROOT_REFUSED', problem)
    elif not args.stage_root and not storage.mounted():
        report.block('env', 'BUILD_VOLUME_NOT_MOUNTED', f'{storage.BUILD_ROOT} is not the mounted project volume')
    elif filesystem_type(stage_root) not in ('apfs', 'hfs'):
        # exFAT and similar store xattrs as AppleDouble ._ files; codesign rejects that detritus.
        report.block('env', 'STAGE_FILESYSTEM_UNSUPPORTED', f'{stage_root} is on {filesystem_type(stage_root)}; '
                     f'stage on the APFS project volume {storage.BUILD_ROOT}')
    stage = stage_root / f'preview-{version}-{commit[:12]}'
    if stage.exists() and not args.dry_run:
        report.block('env', 'STAGE_EXISTS', f'{stage} exists; it is never deleted automatically')
    dmg_root = stage / 'dmg-root'
    ctx = {'stage': stage, 'dmg_root': dmg_root, 'stage_app': dmg_root / app.name, 'source_app': app, 'cef': cef,
           'dmg': stage / f'AxioSozo-{version}-preview-{commit[:12]}-macos-arm64.dmg',
           'volume_name': f'AxioSozo {version} Preview', 'commit': commit, 'signed': [], **signing}
    ctx['codesign'] = signing_plan.plan(app, signing['identity'], signing['network'], stage_app=ctx['stage_app'],
                                        embeds={bundle_checks.CEF_EMBED: cef} if cef else None,
                                        entitlements_dir=stage / 'entitlements')
    ctx['commands'] = planned_commands(ctx)
    report.data['plan'] = [{'step': label, 'needs': needs, 'command': list(map(str, command))}
                           for label, needs, command in ctx['commands']]
    report.data['codesign_order'] = [{k: step[k] for k in ('relative', 'kind', 'entitlements', 'provenance')}
                                     for step in ctx['codesign']]
    context = {'version': version, 'commit': commit, 'safe_browsing': safe['state'], 'chromium': chromium,
               'dmg_name': ctx['dmg'].name}
    if args.dry_run:
        print_plan(ctx, report)
        print('--- release notes template (draft, Preview) ---')
        print(artifacts.release_notes(context))
        return finish(report, args)
    if report.blocked('env', 'preflight'):
        return finish(report, args)
    stage.mkdir(parents=True)
    if not stage_bundle(ctx, repo, report, context):
        return finish(report, args, stage)
    (stage / 'RELEASE_NOTES.md').write_text(artifacts.release_notes(context))
    if report.blocked('signing'):
        # Every step that needs no identity has run; stop here (decision #3).
        print_plan(ctx, report)
        return finish(report, args, stage)
    temporary = os.environ.get('TMPDIR', '')
    if stage_root_problem(temporary or '/tmp'):
        report.block('env', 'TMPDIR_NOT_EXTERNAL', 'run via dev-external + scripts/storage.py exec so Apple '
                     'tools use the build volume')
        return finish(report, args, stage)
    if not sign_and_verify(ctx, report):
        return finish(report, args, stage)
    if signing['network']:
        zip_path = stage / 'notarize-app.zip'
        code, _ = RUNNER(['/usr/bin/ditto', '-c', '-k', '--keepParent', str(ctx['stage_app']), str(zip_path)])
        if code:
            report.block('verify', 'NOTARIZATION_ZIP_FAILED', str(zip_path))
            return finish(report, args, stage)
        if not notarize(zip_path, ctx, report, 'app'):
            return finish(report, args, stage)
    if not build_dmg(ctx, report):
        return finish(report, args, stage)
    if signing['network'] and not (notarize(ctx['dmg'], ctx, report, 'dmg') and gatekeeper(ctx, report)):
        return finish(report, args, stage)
    context['checksums'] = artifacts.write_checksums([ctx['dmg']], stage / 'SHA256SUMS.txt')
    context['signing'] = ('Developer ID signed, notarized and stapled' if signing['network'] else
                          'Developer ID signed WITHOUT secure timestamp; NOT notarized; local verification only, '
                          'not for distribution')
    (stage / 'RELEASE_NOTES.md').write_text(artifacts.release_notes(context))
    report.step('checksums', 'PASS', sha256=context['checksums'])
    return finish(report, args, stage)


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument('--dry-run', action='store_true', help='print the full plan; modify nothing, run no Apple tool')
    result.add_argument('--app', help='explicit .app (default: located via scripts/zen.py describe)')
    result.add_argument('--cef-app', help='CEF host .app to embed at Contents/Helpers, or "none" '
                        f'(default: {CEF_DEFAULT} when present)')
    result.add_argument('--stage-root', help=f'external staging root (default: {storage.BUILD_ROOT}/release)')
    result.add_argument('--expect-commit', help='refuse unless HEAD is exactly this commit')
    result.add_argument('--identity', help=f'Developer ID Application identity (or {IDENTITY_ENV})')
    result.add_argument('--team-id', help=f'Apple team ID (or {TEAM_ENV})')
    result.add_argument('--notary-profile', help=f'notarytool keychain profile name (or {PROFILE_ENV})')
    result.add_argument('--notarize', action='store_true', help='submit to Apple notary service (needs --authorized)')
    result.add_argument('--authorized', action='store_true',
                        help='Wout explicitly authorized contacting Apple for this specific release')
    result.add_argument('--json', action='store_true', help='also print the full JSON report')
    return result


def main(argv=None, repo=ROOT):
    args = parser().parse_args(argv)
    if args.authorized and not args.notarize:
        parser().error('--authorized only applies together with --notarize')
    return run(args, repo=repo)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, subprocess.SubprocessError) as error:
        print('BLOCKED_ENV: ' + str(error), file=sys.stderr)
        raise SystemExit(EXIT_BLOCKED_ENV)
