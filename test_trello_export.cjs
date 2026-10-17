const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function browser(results, {total = 3, completed = 0} = {}) {
  const button = {disabled: false, textContent: ''};
  const progress = {value: completed};
  const status = {textContent: ''};
  const box = {hidden: true, querySelector: s => s === 'progress' ? progress : status};
  let submit;
  const form = {dataset: {total: String(total), completed: String(completed)},
    querySelector: () => button, addEventListener: (_, cb) => { submit = cb; }};
  const location = {href: 'https://bos.test/siparisler/192/trello', reloads: 0,
    reload() { this.reloads++; }, assign(url) { this.target = url; }};
  const listeners = new Set();
  const calls = [];
  const context = {
    document: {getElementById: id => id === 'trello-export-all' ? form : box},
    window: {addEventListener: name => listeners.add(name), removeEventListener: name => listeners.delete(name)},
    FormData: class {}, AbortController, setTimeout, clearTimeout, location,
    fetch: async (url, options) => {
      calls.push({url, options});
      const result = results.shift();
      assert.ok(result, 'unexpected repeated request');
      if (result instanceof Error) throw result;
      return {ok: !result.error, headers: {get: () => 'application/json'}, json: async () => result};
    }
  };
  vm.runInNewContext(fs.readFileSync('static/trello-export.js', 'utf8'), context);
  return {submit: () => submit({preventDefault() {}}), form, button, progress, status, location, listeners, calls};
}

test('one click completes three short card requests followed by Chat', async () => {
  const b = browser([
    {total: 3, completed: 1, chat_done: false},
    {total: 3, completed: 2, chat_done: false},
    {total: 3, completed: 3, chat_done: false},
    {total: 3, completed: 3, chat_done: true}
  ]);
  await b.submit();
  assert.equal(b.calls.length, 4);
  assert.equal(b.progress.value, 3);
  assert.equal(b.location.reloads, 1);
  assert.equal(b.listeners.size, 0);
});

test('PDF failure stops and another click continues from the saved progress', async () => {
  const results = [
    {total: 3, completed: 1, chat_done: false},
    {total: 3, completed: 1, error: 'PDF yüklenemedi', chat_done: false}
  ];
  const b = browser(results);
  await b.submit();
  assert.equal(b.calls.length, 2);
  assert.equal(b.button.disabled, false);
  assert.equal(b.location.reloads, 0);
  assert.equal(b.status.textContent, 'PDF yüklenemedi');
  results.push({total: 3, completed: 2, chat_done: false},
    {total: 3, completed: 3, chat_done: false}, {total: 3, completed: 3, chat_done: true});
  await b.submit();
  assert.equal(b.calls.length, 5);
  assert.equal(b.location.reloads, 1);
});

test('Chat failure permits retry without waiting for a new card', async () => {
  const results = [{total: 3, completed: 3, error: 'Chat gönderilemedi', chat_done: false}];
  const b = browser(results, {completed: 3});
  await b.submit();
  assert.equal(b.button.disabled, false);
  results.push({total: 3, completed: 3, chat_done: true});
  await b.submit();
  assert.equal(b.calls.length, 2);
  assert.equal(b.location.reloads, 1);
});

test('stale sales form navigates to procurement using GET without resubmission', async () => {
  const b = browser([{error: 'Tedarik siparişinden aktarın', redirect_url: '/siparisler/192/trello'}]);
  await b.submit();
  assert.equal(b.location.target, '/siparisler/192/trello');
  assert.equal(b.calls.length, 1);
  assert.equal(b.location.reloads, 0);
  assert.equal(b.listeners.size, 0);
});
