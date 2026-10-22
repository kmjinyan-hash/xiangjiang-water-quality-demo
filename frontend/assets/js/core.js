/* ══════════════════════════════════════════════════════════════════════════
 * core.js — 命名空间、配置、接口访问、数据源、DOM 工具、状态、提示
 *
 * 前后端分离的落点就在这里：
 *   · 所有后端访问都走 WQ.api / WQ.data，地址来自 app.config.js 的 apiBase；
 *   · 前端不认识任何后端实现细节，只认 /api/* 这套 JSON 契约；
 *   · 后端不可达时明确报错并在状态栏提示 —— **不再有内置快照回退**
 *     （离线单文件版已弃用，本项目改为「Docker 部署后端 + 远程访问」）。
 * ══════════════════════════════════════════════════════════════════════════ */

window.WQ = (function () {
  'use strict';

  /* ─────────────── 配置 ─────────────── */
  var RAW = window.WQ_CONFIG || {};
  var CFG = {
    apiBase: RAW.apiBase || '',
    theme: RAW.theme || 'dark',
    basemaps: RAW.basemaps || {},
    defaultBasemap: RAW.defaultBasemap || 'vector',
    historyLimit: RAW.historyLimit || 120,
    appTitle: RAW.appTitle || '水质智能预警平台',
    appSubtitle: RAW.appSubtitle || ''
  };

  /* 接口根地址。支持三种指定方式，便于「前后端分离」后灵活部署：
   *   1) URL 查询串 ?api=http://host:5000   —— 临时指向别的后端（**优先级最高**）
   *   2) URL 查询串 ?port=5000              —— 指向本机某个端口
   *   3) app.config.js 的 apiBase（部署时的默认值）
   *
   * 为什么查询串优先：它表达的是"这一次要连哪个后端"，属于显式意图；
   * 配置文件是默认值。原来实现反了（配置命中就直接 return），
   * 导致打包好的产物无法用 ?api= 指向另一个后端做联调 —— 注释说能，
   * 实际不能。显式 > 隐式，这里按这个原则排。 */
  var API_BASE = (function () {
    var qs = window.location.search || '';
    var m = /[?&]api=([^&]+)/.exec(qs);
    if (m) {
      try {
        var q = decodeURIComponent(m[1]).replace(/\/+$/, '');
        if (q) return q;
      } catch (e) { /* 忽略非法编码，回落到配置 */ }
    }
    var p = /[?&]port=(\d{1,5})/.exec(qs);
    if (p) return 'http://127.0.0.1:' + p[1];
    var b = String(CFG.apiBase || '').trim().replace(/\/+$/, '');
    if (b) return b;
    return '';                       // 空 = 同源
  })();

  /* ─────────────── 状态与订阅 ─────────────── */
  var state = {
    online: true,                    // 后端是否可达
    view: 'map',
    theme: CFG.theme,
    filter: 'all',
    query: '',
    selected: null,                  // 当前断面 id
    stations: [],
    status: {},                      // {ST10:{latest_date,days_old}}
    predictions: {},                 // 最新一次预测结果
    predictionsAt: null,
    basemap: CFG.defaultBasemap,
    reports: [],
    currentReport: null,
    lastError: null
  };
  var subs = {};
  var bus = {
    on: function (evt, fn) { (subs[evt] = subs[evt] || []).push(fn); return bus; },
    emit: function (evt, payload) { (subs[evt] || []).forEach(function (f) { try { f(payload); } catch (e) { console.error(e); } }); }
  };

  /* ─────────────── DOM 与格式化工具 ─────────────── */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* 图标：统一从**本页内联的**精灵取（index.html 里的 .wq-sprite）。
   *
   * 为什么不引用外部 assets/icons/icons.svg：
   *   主应用是双击 dist/index.html 打开的（file://）。Chrome 把每个本地文件
   *   当作独立来源，跨文档的 <use href="xxx.svg#id"> 会被直接拦掉，
   *   表现为"所有按钮图标都没了"（实测：不带 --allow-file-access-from-files 必现）。
   *   精灵内联后引用变成同文档 #id，file:// / http(s) / 打包 WebView 都成立。
   *
   * 版本号问题也随之消失：同文档引用不经过缓存，不需要 ?v= 指纹。 */
  function icon(id, cls) {
    return '<svg class="wq-icon' + (cls ? ' ' + cls : '') + '">' +
           '<use href="#' + id + '"></use></svg>';
  }

  function num(v, d) {
    if (v === null || v === undefined || v === '' || isNaN(v)) return '--';
    return Number(v).toFixed(d === undefined ? 2 : d);
  }
  function dateOnly(s) { return s ? String(s).slice(0, 10) : '--'; }

  var LEVEL = {
    normal:  { key: 'normal',  label: '正常',  cls: 'lv-ok' },
    warning: { key: 'warning', label: '预警',  cls: 'lv-warn' },
    alarm:   { key: 'alarm',   label: '超标',  cls: 'lv-alarm' },
    unknown: { key: 'unknown', label: '未判定', cls: 'lv-unknown' }
  };
  function levelOf(key) { return LEVEL[key] || LEVEL.unknown; }

  /* ─────────────── 接口访问 ─────────────── */
  function api(path, opt) {
    /* 同源时把开头的 '/' 去掉，走**相对路径**拼接。
     * 这样在「子路径穿透」下也正确：页面在 https://host/wq/ 时
     *   'api/x'   -> https://host/wq/api/x   ✅
     *   '/api/x'  -> https://host/api/x       ❌ 会跑到域名根目录，直接 404
     * 指定了绝对的 apiBase 时仍用绝对路径。 */
    var rel = path;
    if (!API_BASE && rel.charAt(0) === '/') rel = rel.slice(1);
    var url = API_BASE + rel;
    opt = opt || {};
    opt.headers = Object.assign({ 'Accept': 'application/json' }, opt.headers || {});
    opt.credentials = 'omit';
    if (window.WQ.llm && (path.indexOf('/api/ai_report') === 0 || path.indexOf('/api/llm_status') === 0)) {
      if (!opt.headers['X-WQ-LLM-Config']) opt.headers['X-WQ-LLM-Config'] = window.WQ.llm.requestHeader();
    }
    return fetch(url, opt).then(function (res) {
      return res.text().then(function (txt) {
        var json = null;
        try { json = txt ? JSON.parse(txt) : null; } catch (e) { /* 非 JSON */ }
        if (!res.ok) {
          var msg = (json && json.message) || ('接口返回 ' + res.status);
          var err = new Error(msg);
          err.status = res.status;
          err.body = json;
          throw err;
        }
        if (json === null) throw new Error('接口返回不是合法 JSON');
        if (json && json.ok === false) {
          var e2 = new Error((json.error && json.error.message) || json.message || '请求失败');
          e2.status = res.status; e2.body = json; throw e2;
        }
        return json;
      });
    });
  }

  function post(path, payload) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(payload || {})
    });
  }

  /* ─────────────── 流式请求（SSE）───────────────
   * 与 api() 共用接口地址和当前页面的大模型配置，把响应体原样交回调用方，
   * 由它自己 reader.read() 逐块解析事件流。
   *
   * 为什么调用方不能自己 fetch：
   *   · 写死的绝对路径（以 / 开头）在 file:// 下会被解析到页面所在盘符，
   *     请求根本发不到后端 —— AI 长报告"生成失败"就是这么来的；
   *   · 裸 fetch 会遗漏当前页面的 API 设置。
   * 这里只负责请求与设置转发，不处理业务。 */
  function stream(path, payload) {
    var rel = path;
    if (!API_BASE && rel.charAt(0) === '/') rel = rel.slice(1);
    var url = API_BASE + rel;
    var headers = {
      'Accept': 'text/event-stream',
      'Content-Type': 'application/json; charset=utf-8'
    };
    if (window.WQ.llm) headers['X-WQ-LLM-Config'] = window.WQ.llm.requestHeader();
    return fetch(url, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(payload || {}),
      credentials: 'omit'
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          throw new Error(body.message || '请求失败，请检查大模型 API 设置');
        });
      }
      return res;
    });
  }

  /* ─────────────── 连通性 ─────────────── */
  /* 只跟踪「后端是否可达」，供状态栏提示。
   * 不再有内置快照回退 —— 离线单文件版已弃用，
   * 本项目的部署形态是「Docker 跑后端 + 远程访问」。 */
  function setOnline(ok) {
    if (state.online !== ok) { state.online = ok; bus.emit('online', ok); }
  }
  function track(promise) {
    return promise.then(function (v) { setOnline(true); return v; })
                  .catch(function (e) { setOnline(false); state.lastError = e.message; throw e; });
  }

  /* ─────────────── 数据源（唯一的数据出入口）───────────────
   * 前端只认 /api/* 这套 JSON 契约，不认识后端的任何实现细节。 */
  var data = {
    /* 后端是否可达；顺带把 data_status 取回来 */
    probe: function () {
      return track(api('/api/data_status'))
        .then(function (r) { state.status = r.data || {}; return state.status; });
    },

    history: function (sid, limit) {
      limit = limit || CFG.historyLimit;
      return track(api('/api/history/' + sid + '?limit=' + limit))
        .then(function (r) { return r.data || []; });
    },

    predictions: function (code, limit) {
      limit = limit || CFG.historyLimit;
      return track(api('/api/prediction_history/' + code + '?limit=' + limit))
        .then(function (r) { return r.data || []; });
    },

    predict: function () {
      /* 必须是 GET。后端 @app.route('/api/predict', methods=['GET'])，
       * 这里曾经写成 post('/api/predict')，结果一直 405，
       * 被上层 catch 吞掉后表现为"预测数据一直是空的"（KPI 卡与主界面都受影响）。 */
      return track(api('/api/predict')).then(function (r) {
        state.predictions = r.data || {};
        state.predictionsAt = new Date();
        return state.predictions;
      });
    },

    rules: function () {
      return track(api('/api/ai_warning_rules'))
        .then(function (r) { state.rules = r.data || {}; return state.rules; });
    },

    reports: function (limit) {
      limit = limit || 100;
      return track(api('/api/ai_reports?limit=' + limit))
        .then(function (r) { return r.data || []; });
    },

    report: function (id) {
      return track(api('/api/ai_report/' + encodeURIComponent(id)))
        .then(function (r) { return r.data; });
    },

    createReport: function (payload) { return post('/api/ai_report', payload).then(function (r) { return r.data; }); },

    /* 模型评估简报（只读）。数据是训练时落盘的验证集指标，接口不重算、不编造。
     * 简报里的 caveat 必须原样展示 —— 前端不做"美化"改写。 */
    modelEval: function () {
      return track(api('/api/model_eval')).then(function (r) { return r.data || {}; });
    },

    /* 流域边界（GeoJSON）。导出地图快照时用它绘制矢量轮廓；
     * 与底图瓦片无关，因此不受跨域限制。 */
    basin: function () {
      return api('/api/basin_boundary').then(function (r) { return r.data || null; });
    },

    /* 地理图层（流域边界 / 土地利用 …）
     * 清单很轻；矢量与影像本体在用户真正打开图层时才取，避免首屏被大栅格拖慢。
     * 刻意不走 track()：没有图层文件属于正常状态，不该把应用标成"离线"。 */
    geoLayers: function () {
      return api('/api/geo_layers').then(function (r) { return r.data || []; });
    },
    geoLayer: function (key) {
      return api('/api/geo_layer/' + encodeURIComponent(key)).then(function (r) { return r.data; });
    },

    /* 用户新增站点：存在后端 SQLite（挂载卷里），重建容器不丢 */
    listStations: function () {
      return track(api('/api/stations')).then(function (r) { return r.data || []; });
    },
    addStation: function (rec) {
      return post('/api/stations', rec).then(function (r) { return r.data; });
    },
    removeStation: function (sid) {
      return api('/api/stations/' + encodeURIComponent(sid), { method: 'DELETE' })
        .then(function (r) { return r.data; });
    },

    deleteReport: function (id) {
      return api('/api/ai_report/' + encodeURIComponent(id), { method: 'DELETE' }).then(function (r) { return r.data; });
    },

    /* 与后端约定保持不变：CSV 直接下载。
     * 同源时同样走相对路径，子路径穿透下才不会跑到域名根目录。 */
    downloadUrl: function (sid) {
      var p = '/api/download/' + sid;
      if (!API_BASE && p.charAt(0) === '/') p = p.slice(1);
      return API_BASE + p;
    }
  };

  /* ─────────────── 提示与对话框 ─────────────── */
  function toast(msg, kind) {
    var host = $('#toasts');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'wq-toast' + (kind ? ' wq-toast--' + kind : '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 200);
    }, kind === 'err' ? 5200 : 3000);
  }

  /* 模态对话框。返回 Promise，resolve 为被点击动作的 value（取消则 null） */
  function dialog(opts) {
    return new Promise(function (resolve) {
      var host = $('#dialogHost');
      var wrap = document.createElement('div');
      wrap.className = 'wq-overlay';
      var actions = (opts.actions || [{ label: '关闭', value: null }]).map(function (a, i) {
        return '<button class="wq-btn' + (a.primary ? ' wq-btn--primary' : '') + '" data-i="' + i + '">' +
               esc(a.label) + '</button>';
      }).join('');
      wrap.innerHTML =
        '<div class="wq-dialog' + (opts.wide ? ' wq-dialog--wide' : '') + '" role="dialog" aria-modal="true">' +
          '<div class="wq-dialog__head">' + (opts.icon ? icon(opts.icon) : '') +
            '<span>' + esc(opts.title || '') + '</span>' +
            '<span class="wq-panel__spacer"></span>' +
            '<button class="wq-ibtn" data-close="1" title="关闭">' + icon('i-close') + '</button>' +
          '</div>' +
          '<div class="wq-dialog__body">' + (opts.html || '') + '</div>' +
          (actions ? '<div class="wq-dialog__foot">' + actions + '</div>' : '') +
        '</div>';

      function done(val) {
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function onKey(e) { if (e.key === 'Escape') done(null); }
      document.addEventListener('keydown', onKey);

      wrap.addEventListener('click', function (e) {
        if (e.target === wrap) return done(null);
        if (e.target.closest('[data-close]')) return done(null);
        var b = e.target.closest('[data-i]');
        if (b) {
          var act = (opts.actions || [])[Number(b.dataset.i)];
          if (!act) return done(null);
          if (act.onClick) { var r = act.onClick(wrap); if (r === false) return; }
          done(act.value === undefined ? null : act.value);
        }
      });
      host.appendChild(wrap);
      if (opts.onOpen) opts.onOpen(wrap);
      var first = wrap.querySelector('input, textarea, select');
      if (first) first.focus();
    });
  }

  /* ─────────────── CSV 导出（前端统一实现，在线离线都可用）─────────────── */
  function downloadCsv(filename, columns, rows) {
    var line = function (arr) {
      return arr.map(function (v) {
        var s = (v === null || v === undefined) ? '' : String(v);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      }).join(',');
    };
    var text = '\ufeff' + line(columns) + '\n' + rows.map(line).join('\n');
    var blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  return {
    cfg: CFG, apiBase: API_BASE, state: state, bus: bus, data: data,
    dataLevel: LEVEL, levelOf: levelOf,
    $: $, $$: $$, esc: esc, icon: icon, num: num, dateOnly: dateOnly,
    api: api, post: post, stream: stream, toast: toast, dialog: dialog, downloadCsv: downloadCsv
  };
})();


/* ══════════════════════════════════════════════════════════════════════
 * 等级判定 —— 全站唯一口径
 *
 * 重要：这是**水质**等级，不是空气的 AQI。
 *   · 指标是 TN / TP / CODMn / NH3N / DO / PH / NTU / TDS；
 *   · 阈值**一律来自后端** /api/ai_warning_rules（即 rules.py），
 *     前端不写死任何数值 —— 空气那套（PM2.5、优/良/轻度污染）与本项目无关；
 *   · 三档：normal（正常）/ warn（预警）/ alarm（超标），无数据为 unknown。
 *
 * 两个特殊语义（空气指标里没有的）：
 *   · DO（溶解氧）**越低越差**，按 lower_is_worse 判定；
 *   · PH 是**区间型**，落在 [lowWarn, highWarn] 之外才算异常。
 * ══════════════════════════════════════════════════════════════════════ */
window.WQ.grade = (function () {
  'use strict';

  var COLORS = {
    normal:  '#3fb950',   // 正常
    warn:    '#d29922',   // 预警
    alarm:   '#e5534b',   // 超标
    unknown: '#6b7280'    // 未判 / 无数据
  };
  var LABELS = {
    normal: '正常', warn: '预警', alarm: '超标', unknown: '未判'
  };

  function levelOf(key, value, rule) {
    if (value === null || value === undefined || value === '' || isNaN(Number(value))) {
      return 'unknown';
    }
    if (!rule) { return 'unknown'; }
    var v = Number(value);

    // pH：区间型
    if (rule.isRange) {
      var lo = rule.lowAlarm, loW = rule.lowWarn, hiW = rule.highWarn, hi = rule.highAlarm;
      if ((lo !== undefined && v <= lo) || (hi !== undefined && v >= hi)) { return 'alarm'; }
      if ((loW !== undefined && v < loW) || (hiW !== undefined && v > hiW)) { return 'warn'; }
      return 'normal';
    }

    // 溶解氧：越低越差
    if (rule.lowerIsWorse) {
      if (rule.alarm !== undefined && v <= rule.alarm) { return 'alarm'; }
      if (rule.warn !== undefined && v <= rule.warn) { return 'warn'; }
      return 'normal';
    }

    // 其余：越高越差
    if (rule.alarm !== undefined && v >= rule.alarm) { return 'alarm'; }
    if (rule.warn !== undefined && v >= rule.warn) { return 'warn'; }
    return 'normal';
  }

  /* 占标率：实测 / **超标**限值，1.0 = 刚好到超标线。
   *
   * 所有指标统一以「超标阈值」为分母，雷达图、排行条、表格次行才能互相比较。
   *   · 一般指标（TN/TP/CODMn/NTU/TDS…）：v / alarm
   *   · DO 越低越差：alarm / v（同样是"越大越糟"，到 1.0 即超标）
   *   · pH 是区间型，单一比值没有意义 —— 返回 null，由调用方显示区间。
   *
   * 曾经 DO 用的是 warn / v，于是同一张雷达图上 DO 的 1.0 表示"到预警线"、
   * 其它指标的 1.0 表示"到超标线"，读图的人会误判。这里统一到 alarm，
   * 等级判定 levelOf 不受影响（它只看 warn/alarm 本身）。 */
  function ratioOf(key, value, rule) {
    var v = Number(value);
    if (!rule || isNaN(v) || v === 0) { return null; }
    if (rule.isRange) { return null; }
    if (rule.lowerIsWorse) {
      return rule.alarm ? (rule.alarm / v) : null;
    }
    return rule.alarm ? (v / rule.alarm) : null;
  }

  /* 从一组等级里取最严重的 */
  function worst(levels) {
    var rank = { alarm: 3, warn: 2, normal: 1, unknown: 0 };
    var out = 'unknown';
    (levels || []).forEach(function (l) {
      if (rank[l] > rank[out]) { out = l; }
    });
    return out;
  }

  return {
    COLORS: COLORS,
    LABELS: LABELS,
    levelOf: levelOf,
    ratioOf: ratioOf,
    worst: worst,
    color: function (lv) { return COLORS[lv] || COLORS.unknown; },
    label: function (lv) { return LABELS[lv] || '未判'; }
  };
})();

/* ══════════════════════════════════════════════════════════════════════
 * 轻量 Markdown 渲染 —— 只支持报告里实际用到的几种标记
 *
 * 为什么自己写而不用库：报告只用 # / ## / **加粗** / - 列表 / 空行分段
 * 这四五种，引一个完整 Markdown 库（含 HTML 透传）反而增加 XSS 面。
 *
 * 安全：**先整体转义，再做替换**。这样即便模型输出 <script> 也只会显示为文本，
 * 不会被执行。替换只在自己生成的标签上做，不引入任何用户可控的标签。
 * ══════════════════════════════════════════════════════════════════════ */
window.WQ.md = function (text) {
  var s = String(text === null || text === undefined ? '' : text);
  s = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  var lines = s.split(/\r?\n/);
  var out = [];
  var inList = false;

  function closeList() { if (inList) { out.push('</ul>'); inList = false; } }

  lines.forEach(function (raw) {
    var line = raw.trim();
    if (!line) { closeList(); out.push(''); return; }

    var m;
    // 标题：# / ## / ###
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList();
      var lv = Math.min(m[1].length + 2, 6);   // # -> h3，避免压过分区标题
      out.push('<h' + lv + ' class="wq-md-h">' + m[2] + '</h' + lv + '>');
      return;
    }
    // 无序列表
    if ((m = line.match(/^[-*]\s+(.*)$/))) {
      if (!inList) { out.push('<ul class="wq-md-ul">'); inList = true; }
      out.push('<li>' + m[1] + '</li>');
      return;
    }
    // 有序列表
    if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
      if (!inList) { out.push('<ul class="wq-md-ul">'); inList = true; }
      out.push('<li>' + m[1] + '</li>');
      return;
    }
    closeList();
    out.push('<p class="wq-md-p">' + line + '</p>');
  });
  closeList();

  var html = out.join('');
  // 行内强调：**粗体**、*斜体*、`代码`
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
             .replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>')
             .replace(/`([^`]+)`/g, '<code>$1</code>');
  return html;
};
