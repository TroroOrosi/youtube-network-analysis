/* Browser driver behavior without a network, browser, or third-party JS library. */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const source = execFileSync('python', ['-c',
  "from fastapi.testclient import TestClient; from web_ui.app import create_app; " +
  "print(TestClient(create_app()).get('/assets/collecting.js').text)"
], {cwd: path.resolve(__dirname, '../..'), encoding: 'utf8'});

function harness(replies) {
  const state = {calls: [], timers: [], navigations: [], nativeSubmits: 0};
  const button = {disabled: false};
  const status = {textContent: ''};
  const form = {
    action: 'https://app.example/collecting/step',
    querySelector: () => button,
    addEventListener: (name, callback) => { state[name] = callback; },
    requestSubmit: () => state.nativeSubmits++,
  };
  vm.runInNewContext(source, {
    document: {getElementById: id => id === 'collection-step' ? form : status},
    window: {location: {href: 'https://app.example/collecting',
      origin: 'https://app.example', assign: url => state.navigations.push(url)}},
    URL,
    FormData: class {constructor(form) {this.form = form;}},
    setTimeout: (callback, delay) => {state.timers.push({callback, delay}); return state.timers.length;},
    clearTimeout: () => {},
    fetch: async (url, options) => {
      state.calls.push({url, options});
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      assert.ok(reply, 'driver made an unexpected extra request');
      return {redirected: false, ok: reply.status < 400,
        headers: {get: () => reply.retryAfter || null}, ...reply};
    },
  });
  return {...state, state, button, status};
}
const settled = () => new Promise(resolve => setImmediate(resolve));

test('a 429 waits for Retry-After and then continues the same CSRF form', async () => {
  const {state, button} = harness([
    {status: 429, retryAfter: '60'},
    {status: 200, redirected: true, url: 'https://app.example/?msg=collected'},
  ]);
  await settled();
  assert.equal(state.calls.length, 1);
  assert.equal(state.nativeSubmits, 0);
  assert.equal(state.timers[0].delay, 60000);
  assert.equal(button.disabled, true);
  state.timers[0].callback();
  await settled();
  assert.equal(state.calls.length, 2);
  assert.equal(state.calls[0].options.method, 'POST');
  assert.equal(state.calls[0].options.credentials, 'same-origin');
  assert.deepEqual(state.navigations, ['https://app.example/?msg=collected']);
});

test('a storage 503 backs off rather than submitting in a tight loop', async () => {
  const {state} = harness([{status: 503, retryAfter: '60'}]);
  await settled();
  assert.equal(state.calls.length, 1);
  assert.equal(state.timers.length, 1);
  assert.ok(state.timers[0].delay >= 60000);
  assert.deepEqual(state.navigations, []);
});

test('a CSRF/permission refusal stops automatic retries', async () => {
  const {state, button, status} = harness([{status: 403}]);
  await settled();
  assert.equal(state.calls.length, 1);
  assert.equal(state.timers.length, 0);
  assert.equal(button.disabled, false);
  assert.match(status.textContent, /開き直/);
});

test('an ambiguous network failure does not blindly repeat the POST', async () => {
  const {state, button, status} = harness([new Error('network lost')]);
  await settled();
  assert.equal(state.calls.length, 1);
  assert.equal(state.timers.length, 0);
  assert.equal(button.disabled, false);
  assert.match(status.textContent, /開き直/);
});

test('the driver never navigates to an external redirect', async () => {
  const {state, status} = harness([{status: 200, redirected: true, url: 'https://outside.example/'}]);
  await settled();
  assert.equal(state.calls.length, 1);
  assert.deepEqual(state.navigations, []);
  assert.match(status.textContent, /開き直/);
});

test('repeated throttling eventually leaves an actionable manual control', async () => {
  const {state, button, status} = harness(Array.from({length: 6}, () => ({status: 429})));
  await settled();
  for (let index = 0; index < 5; index++) {
    assert.ok(state.timers[index]);
    state.timers[index].callback();
    await settled();
  }
  assert.equal(state.calls.length, 6);
  assert.equal(state.timers.length, 5);
  assert.equal(button.disabled, false);
  assert.match(status.textContent, /開き直/);
});
