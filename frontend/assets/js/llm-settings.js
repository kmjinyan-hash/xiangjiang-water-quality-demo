/* 用户自带 API：密钥只留在当前页面内存，不写浏览器存储或后端配置。 */
(function (WQ) {
  'use strict';
  var profile = { enabled: false, base_url: '', model: '', api_key: '' };
  try {
    var saved = JSON.parse(localStorage.getItem('wq.llm.profile') || '{}');
    profile.base_url = typeof saved.base_url === 'string' ? saved.base_url : '';
    profile.model = typeof saved.model === 'string' ? saved.model : '';
  } catch (e) { /* 浏览器禁用存储时仍可使用内存配置 */ }

  function encode(value) { return encodeURIComponent(JSON.stringify(value)); }
  function refresh() {
    var badge = document.getElementById('llmConnectionState');
    if (badge) badge.textContent = profile.enabled ? '已启用' : '未配置';
    var button = document.getElementById('btnApiSettings');
    if (button) button.dataset.enabled = profile.enabled ? 'true' : 'false';
  }
  function remember() {
    try {
      localStorage.setItem('wq.llm.profile', JSON.stringify({
        base_url: profile.base_url, model: profile.model
      }));
    } catch (e) { /* 密钥不会写入存储 */ }
    refresh();
    WQ.bus.emit('llm-config-changed', { enabled: profile.enabled });
  }
  function open() {
    WQ.dialog({
      title: '接入自己的大模型 API', icon: 'i-model',
      html: '<div class="wq-api-settings">' +
        '<p class="wq-api-lead">选择自己的服务，为水质报告和断面解读提供 AI 能力。</p>' +
        '<label class="wq-field"><span class="wq-field__label">使用方式</span>' +
          '<select class="wq-select" id="apiMode"><option value="off">不启用大模型（规则分析）</option>' +
          '<option value="custom">接入自己的 API（OpenAI 兼容）</option></select></label>' +
        '<div id="apiFields" class="wq-api-fields">' +
          '<label class="wq-field"><span class="wq-field__label">服务地址 · Base URL</span>' +
          '<input class="wq-input" id="apiBaseUrl" type="url" placeholder="https://api.example.com/v1" autocomplete="off" spellcheck="false"></label>' +
          '<label class="wq-field"><span class="wq-field__label">模型名称 · Model ID</span>' +
          '<input class="wq-input" id="apiModel" placeholder="填写服务商提供的模型名称" autocomplete="off" spellcheck="false"></label>' +
          '<label class="wq-field"><span class="wq-field__label">API Key</span>' +
          '<span class="wq-api-key"><input class="wq-input" id="apiKey" type="password" placeholder="填写你的 API Key" autocomplete="off" spellcheck="false">' +
          '<button class="wq-btn" id="apiShowKey" type="button" aria-label="显示 API Key">显示</button></span></label>' +
          '<div class="wq-api-test"><button class="wq-btn" id="apiTest" type="button">测试连接</button>' +
          '<span id="apiTestResult" role="status" aria-live="polite"></span></div>' +
        '</div>' +
        '<p class="wq-api-note">支持 OpenAI 兼容的对话接口。测试连接会发送一条简短请求，可能产生少量费用。</p>' +
        '<p class="wq-api-note">密钥仅在当前页面有效，刷新后需重新填写；经本机服务转发，不写入服务器配置。只记住服务地址和模型名称。</p>' +
        '<p class="wq-api-error" id="apiConfigError" role="alert" hidden></p></div>',
      actions: [
        { label: '停用并清除密钥', value: 'clear', onClick: function () {
          profile.enabled = false; profile.api_key = ''; remember();
          WQ.toast('已停用大模型并清除密钥');
        } },
        { label: '取消', value: null },
        { label: '保存设置', primary: true, value: 'save', onClick: function (wrap) {
          var candidate = read(wrap);
          if (!candidate) return false;
          profile = candidate; remember();
          WQ.toast(profile.enabled ? '已启用你的 API' : '已切换为规则分析', 'ok');
        } }
      ],
      onOpen: function (wrap) {
        var find = function (id) { return wrap.querySelector('#' + id); };
        find('apiMode').value = profile.enabled ? 'custom' : 'off';
        find('apiBaseUrl').value = profile.base_url;
        find('apiModel').value = profile.model;
        find('apiKey').value = profile.api_key;
        function updateMode() {
          var off = find('apiMode').value === 'off';
          find('apiFields').hidden = off;
          find('apiConfigError').hidden = true;
        }
        find('apiMode').addEventListener('change', updateMode);
        updateMode();
        find('apiShowKey').addEventListener('click', function () {
          var visible = find('apiKey').type === 'password';
          find('apiKey').type = visible ? 'text' : 'password';
          this.textContent = visible ? '隐藏' : '显示';
          this.setAttribute('aria-label', visible ? '隐藏 API Key' : '显示 API Key');
        });
        var revision = 0;
        ['apiBaseUrl', 'apiModel', 'apiKey', 'apiMode'].forEach(function (id) {
          find(id).addEventListener('input', function () {
            revision += 1;
            find('apiTestResult').textContent = '';
          });
        });
        find('apiTest').addEventListener('click', function () {
          var candidate = read(wrap);
          if (!candidate || !candidate.enabled) return;
          var version = revision;
          var button = this;
          button.disabled = true;
          find('apiTestResult').textContent = '正在连接…';
          WQ.api('/api/llm_status?ping=1', {
            headers: { 'X-WQ-LLM-Config': encode(candidate) }
          }).then(function (result) {
            if (version !== revision) return;
            var ping = (result.data || {}).ping || {};
            if (!ping.ok) throw new Error(ping.error || '没有收到模型回复');
            find('apiTestResult').textContent = '连接成功 · ' + ping.ms + ' ms';
          }).catch(function (err) {
            if (version === revision) find('apiTestResult').textContent = '连接失败：' + err.message;
          }).then(function () { button.disabled = false; });
        });
      }
    });
  }
  function read(wrap) {
    var find = function (id) { return wrap.querySelector('#' + id); };
    var candidate = {
      enabled: find('apiMode').value === 'custom',
      base_url: find('apiBaseUrl').value.trim().replace(/\/+$/, ''),
      model: find('apiModel').value.trim(), api_key: find('apiKey').value.trim()
    };
    var error = '';
    if (candidate.enabled) {
      if (!candidate.base_url || !candidate.model || !candidate.api_key) error = '请完整填写服务地址、模型名称和 API Key。';
      else {
        try {
          var url = new URL(candidate.base_url);
          if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash) error = '请输入不带账号、查询参数或片段的 HTTP(S) 服务地址。';
        } catch (e) { error = '请输入有效的 HTTP(S) 服务地址。'; }
      }
    } else candidate.api_key = '';
    find('apiConfigError').textContent = error;
    find('apiConfigError').hidden = !error;
    return error ? null : candidate;
  }
  WQ.llm = {
    requestHeader: function () { return encode(profile); },
    open: open,
    init: function () {
      refresh();
      var button = document.getElementById('btnApiSettings');
      if (button) button.addEventListener('click', open);
    }
  };
})(window.WQ);
