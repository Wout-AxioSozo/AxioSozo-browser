import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, chmodSync, existsSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createFixtureAdapter, codexRequest, livePreflight } from '../src/adapters.mjs';
import { discover, DRIVERS } from '../src/discovery.mjs';
import { parseGenericCliVersion } from '../vendor/t3/version.ts';
import { appendAcpStderrTail, sanitizeAcpStderrExcerpt } from '../vendor/t3/AcpStderr.ts';

const binding = { instance_id: 'instance-a', account_identity: 'account-a', session_id: 'browser-session-a' };
const input = (turn = 'turn-a') => ({ version: 1, ...binding, request_id: `request-${turn}`, turn_id: turn, text: 'SYNTHETIC TEST: return hello' });
const waitFor = (adapter, type) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => { adapter.off('event', onEvent); reject(new Error(`Timed out waiting for ${type}`)); }, 2500);
  function onEvent(event) { if (event.type === type) { clearTimeout(timer); adapter.off('event', onEvent); resolve(event); } }
  adapter.on('event', onEvent);
});

for (const driver of DRIVERS) {
  test(`${driver}: real owned fixture subprocess, accepted is separate from completion, shutdown twice`, async () => {
    for (let run = 0; run < 2; run++) {
      const adapter = await createFixtureAdapter(driver, binding); const events = [];
      adapter.on('event', event => events.push(event));
      try {
        const completion = waitFor(adapter, 'turn_finished'); const accepted = await adapter.start(input());
        assert.equal(accepted.status, 'accepted'); assert.equal((await completion).status, 'completed');
        assert.equal(events.filter(event => event.type === 'text_delta').map(event => event.text).join(''), 'hello');
        assert(events.every(event => event.label === 'TEST_FIXTURE' && event.session_id === binding.session_id && event.event_id));
        assert.equal(new Set(events.map(event => event.event_id)).size, events.length);
        assert.equal(adapter.capabilities.automatic_browser_control, false);
      } finally { await adapter.close(); }
      assert.throws(() => process.kill(adapter.pid, 0), /ESRCH/);
    }
  });
  test(`${driver}: instance/account isolation rejects before send`, async () => {
    const adapter = await createFixtureAdapter(driver, binding);
    try {
      await assert.rejects(adapter.start({ ...input(), instance_id: 'instance-other' }), { code: 'INSTANCE_MISMATCH' });
      await assert.rejects(adapter.start({ ...input(), account_identity: 'account-other' }), { code: 'INSTANCE_MISMATCH' });
      await assert.rejects(adapter.resume({ ...binding, instance_id: 'instance-other' }), { code: 'INSTANCE_MISMATCH' });
      assert.equal(adapter.active, null);
    } finally { await adapter.close(); }
  });
  test(`${driver}: cancellation waits for native terminal event`, async () => {
    const adapter = await createFixtureAdapter(driver, binding, { behavior: 'hold' });
    try {
      const delta = waitFor(adapter, 'text_delta'); await adapter.start(input()); await delta;
      const completion = waitFor(adapter, 'turn_finished');
      assert.equal((await adapter.interrupt({ session_id: binding.session_id, turn_id: 'turn-a' })).status, 'accepted');
      assert.equal((await completion).status, 'cancelled');
    } finally { await adapter.close(); }
  });
  test(`${driver}: process crash is uncertain and reconnect never replays`, async () => {
    const adapter = await createFixtureAdapter(driver, binding, { behavior: 'crash' });
    try {
      const completion = waitFor(adapter, 'turn_finished'); await adapter.start(input());
      assert.equal((await completion).status, 'uncertain');
      await assert.rejects(adapter.resume(binding), { code: 'UNCERTAIN' });
      await assert.rejects(adapter.start(input('turn-b')), { code: 'SESSION_BUSY' });
    } finally { await adapter.close(); }
  });
  test(`${driver}: no live launch without authentication or proven sandbox`, () => {
    assert.throws(() => livePreflight(driver), { code: 'BLOCKED_AUTH' });
    assert.throws(() => livePreflight(driver, { authorized: true, authenticated: true }), { code: 'BLOCKED_ENV' });
  });
}
test('Codex resume executes generated-schema native thread/resume', async () => {
  const adapter = await createFixtureAdapter('codex', binding);
  try { assert.equal((await adapter.resume(binding)).native_session_id, adapter.native_session_id); }
  finally { await adapter.close(); }
});
test('Claude and Antigravity do not claim unimplemented continuation', async () => {
  for (const driver of ['claude-code', 'antigravity']) {
    const adapter = await createFixtureAdapter(driver, binding);
    try { assert.deepEqual(await adapter.resume(binding), { status: 'unsupported' }); }
    finally { await adapter.close(); }
  }
});
test('Codex validates outgoing methods against installed version, not guessed methods', () => {
  assert.equal(codexRequest('turn/interrupt', { threadId: 't', turnId: 'u' }).method, 'turn/interrupt');
  assert.throws(() => codexRequest('turn/cancel', {}), { code: 'INVALID_PROTOCOL' });
  assert.throws(() => codexRequest('turn/interrupt', { threadId: 1 }), { code: 'INVALID_PROTOCOL' });
});
test('Deduplicates native UUID events but retains legitimate repeated text', async () => {
  for (const driver of ['claude-code', 'codex']) {
    const adapter = await createFixtureAdapter(driver, binding, { behavior: 'duplicate' }); let text = '';
    adapter.on('event', event => { if (event.type === 'text_delta') text += event.text; });
    try { const completion = waitFor(adapter, 'turn_finished'); await adapter.start(input()); await completion; assert.equal(text, driver === 'claude-code' ? 'hello' : 'hellohello'); }
    finally { await adapter.close(); }
  }
});
test('Codex command approvals are explicitly denied', async () => {
  const adapter = await createFixtureAdapter('codex', binding, { behavior: 'approval' });
  try { const approval = waitFor(adapter, 'approval'); const completion = waitFor(adapter, 'turn_finished'); await adapter.start(input()); assert.equal((await approval).status, 'unsupported'); assert.equal((await completion).status, 'completed'); }
  finally { await adapter.close(); }
});
for (const behavior of ['malformed', 'oversized']) test(`Host rejects ${behavior} fixture messages and reaps child`, async () => {
  const adapter = await createFixtureAdapter('antigravity', binding, { behavior });
  const completion = waitFor(adapter, 'turn_finished'); await adapter.start(input());
  assert.equal((await completion).status, 'uncertain'); await adapter.close();
  assert.throws(() => process.kill(adapter.pid, 0), /ESRCH/);
});
test('Discovery never executes malicious launch hooks, MCP, or provider PATH entries', () => {
  const directory = mkdtempSync(fileURLToPath(new URL('../fixtures/discovery-', import.meta.url)));
  try {
    const marker = `${directory}/HOOK_WAS_EXECUTED`;
    for (const command of ['codex', 'claude', 'agy']) { const file = `${directory}/${command}`; writeFileSync(file, `#!/bin/sh\ntouch '${marker}'\n`); chmodSync(file, 0o700); }
    writeFileSync(`${directory}/.mcp.json`, JSON.stringify({ mcpServers: { attack: { command: 'touch', args: [marker] } } }));
    assert(discover({ searchPath: directory }).every(item => item.installed && item.auth_status === 'unknown'));
    assert.equal(existsSync(marker), false);
  } finally { rmSync(directory, { recursive: true }); }
});
test('T3 version extraction and bounded redaction regressions', () => {
  assert.equal(parseGenericCliVersion('codex-cli 0.155.1'), '0.155.1');
  assert.equal(parseGenericCliVersion('v2.1.278'), '2.1.278');
  assert.equal(parseGenericCliVersion('unknown'), null);
  assert.equal(appendAcpStderrTail('x'.repeat(4096), 'abc').length, 4096);
  const result = sanitizeAcpStderrExcerpt('Authorization: Bearer secret-value\nx-api-key: secret-api\n/Users/synthetic/file\nhttp://localhost/pair#abc', { HOME: '/Users/synthetic' });
  assert(!result.includes('secret-value') && !result.includes('secret-api') && !result.includes('/Users/synthetic') && !result.includes('#abc'));
  const signIn = sanitizeAcpStderrExcerpt('Sign in: https://accounts.google.com/o/oauth2/v2/auth?state=oauth-state-value', { HOME: '/Users/synthetic' });
  assert(signIn.includes('[sign-in-url]') && !signIn.includes('oauth-state-value'));
});
