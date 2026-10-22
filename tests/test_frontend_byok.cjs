// Node-only unit test: no real browser, model credentials or network requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const stored = {};
const requests = [];
const location = { search: '' };
const context = vm.createContext({
  window: { WQ_CONFIG: { apiBase: 'http://test.invalid' }, location }, location,
  document: { getElementById: () => ({ dataset: {} }) },
  localStorage: { getItem: () => null, setItem: (key, value) => { stored[key] = value; } },
  URL, console,
  fetch: async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, text: async () => '{"success":true}' };
  }
});
for (const name of ['core.js', 'llm-settings.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/assets/js', name), 'utf8'), context);
}
const WQ = context.window.WQ;
let dialog;
WQ.dialog = options => { dialog = options; };
WQ.toast = () => {};
const fields = {
  apiMode: { value: 'custom' }, apiBaseUrl: { value: 'https://mock.invalid/v1/' },
  apiModel: { value: 'mock-model' }, apiKey: { value: 'fake-test-key' },
  apiConfigError: {}
};
const wrap = { querySelector: selector => fields[selector.slice(1)] };

(async () => {
  assert.equal(JSON.parse(decodeURIComponent(WQ.llm.requestHeader())).enabled, false);
  WQ.llm.open();
  dialog.actions.find(action => action.value === 'save').onClick(wrap);
  assert.equal(JSON.parse(stored['wq.llm.profile']).base_url, 'https://mock.invalid/v1');
  assert.ok(!JSON.stringify(stored).includes('fake-test-key'));
  assert.ok(!JSON.stringify(stored).includes('api_key'));
  await WQ.api('/api/llm_status');
  await WQ.stream('/api/ai_report/generate', { station: 'st10' });
  for (const call of requests) {
    const settings = JSON.parse(decodeURIComponent(call.options.headers['X-WQ-LLM-Config']));
    assert.equal(settings.api_key, 'fake-test-key');
    assert.equal(settings.model, 'mock-model');
  }
  fields.apiBaseUrl.value = 'https://user:password@mock.invalid/v1';
  assert.equal(dialog.actions.find(action => action.value === 'save').onClick(wrap), false);
  assert.equal(fields.apiConfigError.hidden, false);
  dialog.actions.find(action => action.value === 'clear').onClick(wrap);
  assert.equal(JSON.parse(decodeURIComponent(WQ.llm.requestHeader())).api_key, '');
  assert.equal(JSON.parse(decodeURIComponent(WQ.llm.requestHeader())).enabled, false);
  console.log('PASS: disabled default, API and SSE headers, no key storage, validation, key removal');
})().catch(error => { console.error(error); process.exitCode = 1; });
