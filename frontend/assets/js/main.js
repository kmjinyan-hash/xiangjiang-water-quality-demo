/* ══════════════════════════════════════════════════════════════════════════
 * main.js — 引导：注册视图、装配动作、联动事件、首次加载
 *
 * 这里也是「在线 / 离线一套源码」的收口处：所有动作都只调用 WQ.data，
 * 由 core.js 决定走 /api/* 还是回退到内置快照。
 * ══════════════════════════════════════════════════════════════════════════ */

(function (WQ) {
  'use strict';

  var $ = WQ.$, esc = WQ.esc;

  /* ─────────────── 数据加载 ─────────────── */

  function loadStatus() {
    return WQ.data.probe().then(function (st) {
      WQ.shell.refreshStatusbar();
      return st;
    }).catch(function (e) {
      WQ.shell.setStatus('后端不可达：' + e.message, 'err');
    });
  }

  function loadRules() {
    return WQ.data.rules().catch(function () { /* 阈值取不到不阻断界面 */ });
  }

  /* 取每个预测断面的最新一条预测记录（接口按日期升序返回，取末条） */
  function loadPredictions() {
    var targets = WQ.state.stations.filter(function (s) { return s.role === 'target'; });
    return Promise.all(targets.map(function (s) {
      return WQ.data.predictions(s.code, 365).then(function (rows) {
        if (!rows || !rows.length) return null;
        var r = rows[rows.length - 1];
        return { code: s.code, predicted_TN: r.predicted_TN, actual_TN: r.actual_TN, date: r.predict_date, base: r.base_date };
      }).catch(function () { return null; });
    })).then(function (list) {
      var map = {};
      list.filter(Boolean).forEach(function (x) {
        map[x.code] = { predicted_TN: x.predicted_TN, actual_TN: x.actual_TN, last_recorded_date: x.base, predict_date: x.date };
      });
      if (Object.keys(map).length) {
        WQ.state.predictions = map;
        WQ.state.predictionsAt = new Date();
      }
    });
  }

  /* 报告等级回填到目录与地图（每个断面取最近一份报告） */
  function loadReportLevels() {
    return WQ.views.report.reload().then(function () {
      var lv = {};
      (WQ.state.reports || []).forEach(function (r) {
        var code = String(r.station || '').toUpperCase();
        if (code && !lv[code]) lv[code] = r.overall;      // 列表已按时间倒序
      });
      WQ.state.reportLevel = lv;
      WQ.shell.renderStations();
      if (WQ.views.map.onData) WQ.views.map.onData();
    }).catch(function () { });
  }

  function broadcastData() {
    Object.keys(WQ.views).forEach(function (n) {
      if (WQ.views[n].onData) WQ.views[n].onData();
    });
    WQ.shell.refreshStatusbar();
  }

  /* 后端可用但一条预测都没有时（新部署 / 换了数据卷 / 库被清过），自动跑一次推理。
   * 否则界面上所有预测相关的内容都是空的，看起来像"预测功能坏了"。
   * 每次页面加载只自动跑一次。 */
  var autoPredicted = false;

  function loadAll(fresh) {
    WQ.shell.setStatus('正在加载…', 'warn');
    return Promise.all([loadStatus(), loadRules(), loadPredictions()])
      .then(function () {
        var hasPred = Object.keys(WQ.state.predictions || {}).length > 0;
        if (!fresh && !hasPred && WQ.state.online && !autoPredicted) {
          autoPredicted = true;
          WQ.toast('首次加载：正在计算模型预测…');
          return WQ.data.predict()
            .then(function () { return loadPredictions(); })
            .catch(function (e) { WQ.toast('自动预测失败：' + e.message, 'err'); });
        }
      })
      .then(function () {
        if (fresh && WQ.views.table.refresh) WQ.views.table.refresh();
        return loadReportLevels();
      })
      .then(function () {
        broadcastData();
        WQ.shell.setStatus(WQ.state.online ? '就绪' : '后端服务不可达',
                           WQ.state.online ? 'ok' : 'err');
      });
  }

  /* ─────────────── 动作 ─────────────── */

  function runPredict() {
    var btns = WQ.$$('[data-action="predict"]');
    btns.forEach(function (b) { b.disabled = true; });
    WQ.shell.setStatus('模型推理中…', 'warn');
    WQ.data.predict().then(function (res) {
      var n = Object.keys(res || {}).length;
      WQ.toast('预测完成：' + n + ' 个断面', 'ok');
      WQ.shell.setStatus('预测完成（' + n + ' 个断面）');
      return loadPredictions().then(broadcastData);
    }).catch(function (e) {
      var msg = e.status === 429 ? '上一次推理还在进行中，请稍候再试' : e.message;
      WQ.toast('预测失败：' + msg, 'err');
      WQ.shell.setStatus('预测失败', 'err');
    }).then(function () {
      btns.forEach(function (b) { b.disabled = false; });
    });
  }

  function exportStation() {
    var sid = WQ.state.selected;
    if (!sid) { WQ.toast('请先在左侧内容目录中选择一个断面'); return; }
    window.location.href = WQ.data.downloadUrl(sid);   // 后端直接给 CSV
    WQ.toast('已开始下载 ' + sid.toUpperCase() + ' 的 CSV');
  }

  function exportTable() {
    var rows = WQ.views.table.currentRows ? WQ.views.table.currentRows() : [];
    if (!rows.length) { WQ.toast('属性表当前没有数据', 'err'); return; }
    var cols = ['_code', '_name', 'DATE', 'TEM', 'PH', 'DO', 'TDS', 'NTU', 'CODMN', 'TP', 'TN'];
    WQ.downloadCsv('断面监测数据.csv', cols, rows.map(function (r) {
      return cols.map(function (c) { return r[c]; });
    }));
    WQ.toast('已导出 ' + rows.length + ' 行', 'ok');
  }

  function newReportDialog() {
    var targets = WQ.state.stations.filter(function (s) { return s.role === 'target'; });
    var opts = targets.map(function (s) {
      var sel = (WQ.state.selected === s.id) ? ' selected' : '';
      return '<option value="' + esc(s.id) + '"' + sel + '>' + esc(s.code + ' ' + s.name) + '</option>';
    }).join('');
    var today = new Date().toISOString().slice(0, 10);
    var payload = null;          // 在 onClick 里填；那时 DOM 还在，能读到输入

    WQ.dialog({
      title: '新建预警报告', icon: 'i-add',
      html:
        '<div class="wq-field"><label class="wq-field__label">断面</label>' +
          '<select class="wq-select" id="nrStation">' + opts + '</select></div>' +
        '<div class="wq-field"><label class="wq-field__label">报告日期</label>' +
          '<input class="wq-input" id="nrDate" type="date" value="' + today + '"></div>' +
        '<div class="wq-field"><label class="wq-field__label">来源</label>' +
          '<input class="wq-input" id="nrSource" placeholder="例如：自动监测 / 人工巡检 / AI 生成"></div>' +
        '<div class="wq-field"><label class="wq-field__label">报告正文' +
          '（粘贴含指标数值的文本，系统按阈值自动抽取与判定）</label>' +
          '<textarea class="wq-textarea" id="nrContent" placeholder="例：2026年4月6日监测，总氮 2.88 mg/L，总磷 0.068 mg/L，高锰酸盐指数 5.7 mg/L，pH 7.6，溶解氧 7.1 mg/L。"></textarea></div>',
      actions: [
        { label: '取消', value: null },
        { label: '提交并判定', primary: true, value: 'ok', onClick: function (wrap) {
            var content = wrap.querySelector('#nrContent').value.trim();
            if (!content) { WQ.toast('报告正文不能为空', 'err'); return false; }   // false = 不关闭
            payload = {
              station: wrap.querySelector('#nrStation').value,
              report_date: wrap.querySelector('#nrDate').value,
              source: wrap.querySelector('#nrSource').value.trim(),
              content: content
            };
          } }
      ]
    }).then(function (val) {
      if (val !== 'ok' || !payload) return;
      WQ.data.createReport(payload).then(function (r) {
        WQ.toast('报告已保存并完成判定：' + (WQ.dataLevel[r.overall] || WQ.dataLevel.unknown).label, 'ok');
        WQ.state.currentReport = r.id;
        WQ.shell.setView('report');
        return loadReportLevels().then(function () { WQ.views.report.open(r.id); });
      }).catch(function (e) {
        WQ.toast('保存失败：' + e.message, 'err');
      });
    });
  }

  /* 指定断面，主动让在线大模型生成一份报告。
   * 与「新建报告」的区别：不用粘贴文本，直接取该断面库里最新一条监测记录，
   * 由模型写结论；生成过程在右侧逐字显示（流式）。 */
  function generateReportDialog() {
    if (!WQ.state.stations || !WQ.state.stations.length) {
      WQ.toast('断面清单尚未加载完成', 'err');
      return;
    }
    var opts = WQ.state.stations.map(function (s) {
      var sel = (WQ.state.selected === s.id) ? ' selected' : '';
      return '<option value="' + esc(s.id) + '"' + sel + '>' + esc(s.code + ' ' + s.name) + '</option>';
    }).join('');
    var payload = null;

    WQ.dialog({
      title: 'AI 生成预警报告', icon: 'i-spark',
      html:
        '<div class="wq-field"><label class="wq-field__label">断面</label>' +
          '<select class="wq-select" id="grStation">' + opts + '</select></div>' +
        '<div class="wq-field"><label class="wq-field__label">补充说明（可选，会一并提供给模型）</label>' +
          '<textarea class="wq-textarea" id="grNote" rows="3" ' +
          'placeholder="例如：近期降雨偏多，需重点关注农业面源污染"></textarea></div>' +
        '<div class="wq-hint">系统取该断面最新一条监测记录做阈值判定，' +
          '<b>等级由规则决定</b>；在线模型负责撰写结论与处置建议，生成过程逐字显示。</div>',
      actions: [
        { label: '取消', value: null },
        { label: '开始生成', primary: true, value: 'ok', onClick: function (wrap) {
            payload = {
              station: wrap.querySelector('#grStation').value,
              note: wrap.querySelector('#grNote').value.trim()
            };
          } }
      ]
    }).then(function (val) {
      if (val !== 'ok' || !payload) { return; }
      WQ.state.currentReport = null;
      WQ.shell.setView('report');
      renderListSafe();
      WQ.views.report.streamGenerate(payload);
    });
  }

  /* 生成前先清掉列表选中态，避免旧报告的选中框留在未刷新的列表里 */
  function renderListSafe() {
    var host = document.getElementById('reportList');
    if (!host) { return; }
    Array.prototype.forEach.call(host.querySelectorAll('[data-report]'), function (li) {
      li.removeAttribute('aria-selected');
    });
  }

  /* ─────────────── 装配 ─────────────── */

  function bindActions() {
    WQ.actions.fit = function () { WQ.shell.setView('map'); WQ.views.map.fitAll(); };
    WQ.actions.locate = function () {
      var sid = WQ.state.selected;
      if (!sid) {
        var t = WQ.state.stations.filter(function (s) { return s.role === 'target'; })[0];
        sid = t && t.id;
      }
      if (!sid) { WQ.toast('没有可定位的断面', 'err'); return; }
      WQ.shell.setView('map');
      WQ.views.map.flyTo(sid);
      /* 定位后同时打开现场信息面板：负责人要求"点击后可以加载站点的图片
       * 和周围的实地显示信息"，照片未采集时显示空图片框。 */
      WQ.views.map.stationDetail(sid);
    };
    WQ.actions.measure = function () { WQ.shell.setView('map'); WQ.views.map.toggleMeasure(); };
    WQ.actions.basemap = function (btn) { WQ.shell.setView('map'); WQ.views.map.setBasemap(btn.dataset.basemap); };
    /* 地理图层开关：流域边界 / 土地利用（清单由后端下发，可继续扩展） */
    WQ.actions['geo-layer'] = function (btn) {
      WQ.shell.setView('map');
      WQ.views.map.toggleGeoLayer(btn.dataset.layer);
    };
    WQ.actions.refresh = function () { loadAll(true).then(function () { WQ.toast('已刷新', 'ok'); }); };
    WQ.actions.predict = runPredict;
    WQ.actions['export-station'] = exportStation;
    WQ.actions['export-table'] = exportTable;
    WQ.actions['new-report'] = newReportDialog;
    WQ.actions['generate-report'] = generateReportDialog;
    WQ.actions['refresh-docs'] = function () {
      WQ.shell.setView('docs');
      if (WQ.views.docs && WQ.views.docs.render) { WQ.views.docs.render(); }
    };
    WQ.actions['report-list'] = function () {      WQ.shell.setView('report');
      loadReportLevels().then(function () {
        if (WQ.state.reports.length) WQ.views.report.open(WQ.state.reports[0].id);
      });
    };
    WQ.actions['delete-report'] = function (btn) {
      var id = btn.dataset.id;
      WQ.dialog({
        title: '删除报告', icon: 'i-delete',
        html: '<p>确定删除这份预警报告？该操作不可撤销。</p>',
        actions: [{ label: '取消', value: null },
                  { label: '删除', primary: true, value: 'ok' }]
      }).then(function (v) {
        if (v !== 'ok') return;
        WQ.data.deleteReport(id).then(function () {
          WQ.toast('已删除', 'ok');
          /* 只有删的正是当前打开那份时才清空右侧详情；
           * 从列表里删别的报告不应该打断你正在看的内容。 */
          if (WQ.state.currentReport === id) {
            WQ.state.currentReport = null;
            WQ.views.report.clearDetail();
          }
          WQ.views.report.reload();
          loadReportLevels();
        }).catch(function (e) { WQ.toast('删除失败：' + e.message, 'err'); });
      });
    };
  }

  /* 应用里的视图清单。新增视图只要在这里加一项，
   * 地址栏 #/<view> 与首屏视图判断都自动生效。 */
  var VIEWS = ['map', 'table', 'charts', 'insight', 'report', 'docs'];

  function bindEvents() {
    WQ.bus.on('select', function (p) {
      var s = WQ.state.stations.filter(function (x) { return x.id === p.id; })[0];
      if (s) WQ.shell.setStatus(s.code + ' · ' + s.name + (s.role === 'target' ? '（预测断面）' : '（上游断面）'));
      Object.keys(WQ.views).forEach(function (n) {
        if (WQ.views[n].onSelect) WQ.views[n].onSelect(p.id, p.opts);
      });
    });
    WQ.bus.on('theme', function () {
      Object.keys(WQ.views).forEach(function (n) {
        if (WQ.views[n].onTheme) WQ.views[n].onTheme();
      });
    });
    WQ.bus.on('resize', function () {
      Object.keys(WQ.views).forEach(function (n) {
        if (WQ.views[n].onResize) WQ.views[n].onResize();
      });
    });
    /* 视图切换时让当前视图重新度量一次（地图/图表需要） */
    window.addEventListener('hashchange', function () {
      var v = (location.hash || '').replace('#/', '');
      if (VIEWS.indexOf(v) >= 0) WQ.shell.setView(v);
    });
  }

  /* ══════════════════════════════════════════════════════════════
   * 断面台账
   *
   * **唯一数据源在后端** `/api/stations`（内置 14 个 + 用户新增）。
   * 前端不再自带 stations.js，本地不存任何站点数据 —— 这正是"前端只是显示器"的含义：
   * 换一个前端、打包 APK、第三方接入，拿到的都是同一份台账。
   *
   * 前端只做一件显示层的事：把 WGS-84 转成 GCJ-02 给高德底图用。
   * 这个转换属于底图选择的结果，不是数据本身，所以留在前端（coord.js）。
   * ══════════════════════════════════════════════════════════════ */
  function toStation(r) {
    var g = window.GCJ.wgs2gcj(Number(r.lon), Number(r.lat));
    return {
      id: r.id,
      name: r.name,
      role: r.role || 'upstream',
      river: r.river || '--',
      city: r.city || '--',
      lon: Number(r.lon),
      lat: Number(r.lat),
      gcj: { lng: g.lng, lat: g.lat },
      code: r.code || String(r.id).toUpperCase(),
      custom: !r.builtin
    };
  }

  /* 从后端拉全量断面（内置 + 自定义），覆盖本地列表 */
  function loadStations() {
    return WQ.data.listStations().then(function (list) {
      WQ.state.stations = (list || []).map(toStation);
      WQ.shell.renderStations();
      if (WQ.views.map && WQ.views.map.refreshMarkers) { WQ.views.map.refreshMarkers(); }
      /* 首屏由断面范围决定地图视图（原来靠配置里硬编码的流域中心坐标，
       * 那属于把数据放在前端）。只做一次，之后用户自己拖动不被重置。 */
      if (!WQ.state.mapFitted && WQ.state.stations.length && WQ.views.map && WQ.views.map.fitAll) {
        WQ.state.mapFitted = true;
        try { WQ.views.map.fitAll(); } catch (e) { /* 地图尚未创建时忽略 */ }
      }
      return WQ.state.stations;
    }).catch(function (e) {
      WQ.toast('取断面台账失败：' + (e.message || ''), 'err');
      return [];
    });
  }

  /* 报告模板：全部用系统里已有的数据即时生成，不需要手工粘贴 */
  function buildTemplateText(tpl, sid) {
    var fmt = function (v) {
      var n = Number(v);
      return isNaN(n) ? String(v) : (Math.round(n * 1000) / 1000);
    };
    var rules = WQ.state.rules || {};

    if (tpl === 'overview') {
      var targets = WQ.state.stations.filter(function (x) { return x.role === 'target'; });
      var lines = ['【断面概览】湘江流域水质智能预警平台　' + new Date().toISOString().slice(0, 10), ''];
      targets.forEach(function (x) {
        var st = WQ.state.status[x.code] || {};
        var p = WQ.state.predictions[x.code];
        lines.push('· ' + x.name + '（' + x.code + '）' +
          '最新数据 ' + (st.latest_date || '无') +
          '；预测 TN ' + (p ? fmt(p.predicted_TN) + ' mg/L' : '暂无') +
          (p ? '；实测 TN ' + fmt(p.actual_TN) + ' mg/L' : ''));
      });
      return Promise.resolve({
        station: '', title: '断面概览',
        content: lines.join('\n')
      });
    }

    var s = WQ.state.stations.filter(function (x) { return x.id === sid; })[0];
    if (!s) { return Promise.reject(new Error('未找到断面')); }

    return WQ.data.history(sid, 1).then(function (rows) {
      var row = rows && rows[0];
      if (!row) { throw new Error('该断面暂无历史数据，无法生成报告'); }
      var date = String(row.DATE || '').slice(0, 10);
      var items = [];
      Object.keys(row).forEach(function (k) {
        if (k === 'DATE') { return; }
        var v = row[k];
        if (v == null || v === '') { return; }
        var r = rules[k];
        /* 阈值表里有的按中文名 + 单位写；没有的直接用列名，保证不丢数据 */
        items.push(r
          ? (r.label || k) + ' ' + fmt(v) + (r.unit ? ' ' + r.unit : '')
          : k + ' ' + fmt(v));
      });
      var isAlarm = tpl === 'alarm';
      var body = (isAlarm ? '【超标预警专报】' : '【日常简报】') +
        s.name + '（' + s.code + '）\n' +
        '监测日期：' + date + '\n' +
        '监测结果：\n' + items.join('、') + '\n' +
        (isAlarm
          ? '处置建议：加密监测频次，核查上游来水与周边污染源，必要时启动应急响应。\n'
          : '说明：数据来自自动监测站上报，判定由系统按阈值自动给出。\n');
      return {
        station: s.id,
        title: (isAlarm ? '超标预警专报 · ' : '日常简报 · ') + s.name,
        content: body
      };
    });
  }

  WQ.actions['add-station'] = function () {
    var payload = null;
    WQ.dialog({
      title: '新增站点', icon: 'i-station',
      html:
        '<div class="wq-field"><label class="wq-field__label">站点名称 *</label>' +
          '<input class="wq-input" id="asName" placeholder="例如：樟树湾"></div>' +
        '<div class="wq-field"><label class="wq-field__label">站点编号（留空自动分配，st90 起）</label>' +
          '<input class="wq-input" id="asId" placeholder="留空即可"></div>' +
        '<div class="wq-field" style="display:flex;gap:8px">' +
          '<div style="flex:1"><label class="wq-field__label">经度 *</label>' +
            '<input class="wq-input" id="asLon" placeholder="112.90000"></div>' +
          '<div style="flex:1"><label class="wq-field__label">纬度 *</label>' +
            '<input class="wq-input" id="asLat" placeholder="28.10000"></div>' +
        '</div>' +
        '<div class="wq-field" style="display:flex;gap:8px">' +
          '<div style="flex:1"><label class="wq-field__label">所在河流</label>' +
            '<input class="wq-input" id="asRiver" placeholder="例如：湘江"></div>' +
          '<div style="flex:1"><label class="wq-field__label">所在城市</label>' +
            '<input class="wq-input" id="asCity" placeholder="例如：长沙"></div>' +
        '</div>' +
        '<div class="wq-field"><label class="wq-field__label">站点角色</label>' +
          '<select class="wq-select" id="asRole">' +
            '<option value="upstream">上游输入断面</option>' +
            '<option value="target">预测断面（模型输出）</option></select></div>' +
        '<p style="color:var(--c-text-faint);font-size:var(--fs-xs);margin:0">' +
          '经纬度按 WGS-84 填写（可在地图上用右下角坐标读数）。新站点暂无历史数据，' +
          '可用「新建报告」粘贴监测文本，或后续经采集/导入补入。</p>',
      actions: [
        { label: '取消', value: null },
        { label: '保存', primary: true, value: 'ok', onClick: function (wrap) {
            var name = wrap.querySelector('#asName').value.trim();
            var lon = wrap.querySelector('#asLon').value.trim();
            var lat = wrap.querySelector('#asLat').value.trim();
            if (!name) { WQ.toast('站点名称不能为空', 'err'); return false; }
            if (!lon || !lat || isNaN(Number(lon)) || isNaN(Number(lat))) {
              WQ.toast('经纬度必须是数字', 'err'); return false;
            }
            payload = {
              name: name,
              id: wrap.querySelector('#asId').value.trim() || undefined,
              lon: Number(lon), lat: Number(lat),
              river: wrap.querySelector('#asRiver').value.trim(),
              city: wrap.querySelector('#asCity').value.trim(),
              role: wrap.querySelector('#asRole').value
            };
          } }
      ]
    }).then(function (val) {
      if (val !== 'ok' || !payload) { return; }
      WQ.data.addStation(payload).then(function (rec) {
        WQ.state.stations.push(toStation(rec));
        WQ.shell.renderStations();
        if (WQ.views.map && WQ.views.map.refreshMarkers) { WQ.views.map.refreshMarkers(); }
        WQ.toast('已新增站点 ' + rec.name + '（' + rec.id.toUpperCase() + '）', 'ok');
      }).catch(function (e) {
        WQ.toast('新增失败：' + e.message, 'err');
      });
    });
  };

  WQ.actions['report-templates'] = function () {
    var targets = WQ.state.stations.filter(function (s) { return s.role === 'target'; });
    if (!targets.length) { WQ.toast('没有可用的预测断面', 'err'); return; }
    var opts = targets.map(function (s) {
      return '<option value="' + esc(s.id) + '">' + esc(s.code + ' ' + s.name) + '</option>';
    }).join('');
    var payload = null;
    WQ.dialog({
      title: '预警报告 · 参考模板', icon: 'i-report',
      html:
        '<p style="color:var(--c-text-dim);font-size:var(--fs-sm);margin:0 0 10px">' +
        '三套模板都用系统里已有的数据即时生成，不需要手工粘贴文本。' +
        '生成后的报告可在报告列表中查看与删除。</p>' +
        '<div class="wq-field"><label class="wq-field__label">断面（概览模板不需要选）</label>' +
          '<select class="wq-select" id="rtStation">' + opts + '</select></div>' +
        '<div class="wq-field"><label class="wq-field__label">模板</label>' +
          '<select class="wq-select" id="rtTpl">' +
            '<option value="daily">日常简报 —— 单断面最新监测数据与逐项判定</option>' +
            '<option value="alarm">超标预警专报 —— 单断面数据 + 处置建议</option>' +
            '<option value="overview">断面概览 —— 全部预测断面实测与预测对照</option>' +
          '</select></div>',
      actions: [
        { label: '取消', value: null },
        { label: '生成报告', primary: true, value: 'ok', onClick: function (wrap) {
            payload = {
              tpl: wrap.querySelector('#rtTpl').value,
              sid: wrap.querySelector('#rtStation').value
            };
          } }
      ]
    }).then(function (val) {
      if (val !== 'ok' || !payload) { return; }
      buildTemplateText(payload.tpl, payload.sid).then(function (built) {
        return WQ.data.createReport({
          station: built.station,
          report_date: new Date().toISOString().slice(0, 10),
          source: '模板生成',
          content: built.content,
          title: built.title
        });
      }).then(function (r) {
        WQ.toast('报告已生成：' + (WQ.dataLevel[r.overall] || WQ.dataLevel.unknown).label, 'ok');
        WQ.state.currentReport = r.id;
        WQ.shell.setView('report');
        return loadReportLevels().then(function () { WQ.views.report.open(r.id); });
      }).catch(function (e) {
        WQ.toast('生成失败：' + e.message, 'err');
      });
    });
  };

  function boot() {
    WQ.state.stations = [];
    WQ.state.reportLevel = {};
    WQ.shell.init();
    Object.keys(WQ.views).forEach(function (n) { WQ.shell.registerView(n, WQ.views[n]); });
    bindActions();
    bindEvents();
    WQ.shell.renderStations();
    WQ.shell.refreshStatusbar();
    var hash = (location.hash || '').replace('#/', '');
    WQ.state.initialView = VIEWS.indexOf(hash) >= 0 ? hash : 'map';
    WQ.state.booted = true;
    WQ.bus.emit('boot');
    loadStations().then(function () {
      WQ.state.viewReady = true;
      WQ.shell.setView(WQ.state.initialView);
      loadAll(false);
    }).catch(function (err) {
      WQ.toast('无法加载断面：' + err.message, 'err');
    });
    WQ.llm.init();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window.WQ);
