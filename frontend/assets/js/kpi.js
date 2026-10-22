/* KPI 指标条 —— 地图上的悬浮卡片
 * ══════════════════════════════════════════════════════════════════════
 * 参考系统每页顶部有一排指标卡；这里做同一件事，但**指标是水质的**。
 *
 * 数据形状（实测，2026-10-21）：
 *   /api/stations  -> [{id:'st10', code:'ST10', name:'涟水', role:'target', ...}]
 *   /api/predict   -> {ST10: {actual_TN, predicted_TN, last_recorded_date}, ...}  只有 5 个预测断面
 *   /api/data_status -> {}   目前是空的，所以新鲜度从 last_recorded_date 自己算
 *
 * 因此：
 *   · 平均总氮 / 最差断面 只用 predict 里的 actual_TN（没有值就用 predicted_TN 并标注）；
 *   · 超标判定只按 TN（后端目前只给 TN 这一项，其它指标没有数据，不能假装有）；
 *   · 新鲜度取最旧的 last_recorded_date，超过 7 天标黄。
 * ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var ID = 'wqKpiBar';
  var rules = null;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function daysSince(iso) {
    if (!iso) { return null; }
    var t = Date.parse(iso + 'T00:00:00');
    if (isNaN(t)) { return null; }
    return Math.floor((Date.now() - t) / 86400000);
  }

  function card(icon, title, value, unit, sub, color) {
    return '<div class="wq-kpi">' +
      '<div class="wq-kpi__h"><span class="wq-kpi__i">' + icon + '</span>' + esc(title) + '</div>' +
      '<div class="wq-kpi__v"' + (color ? ' style="color:' + color + '"' : '') + '>' + esc(value) +
      (unit ? '<span class="wq-kpi__u">' + esc(unit) + '</span>' : '') + '</div>' +
      '<div class="wq-kpi__s">' + (sub || '') + '</div>' +
      '</div>';
  }

  function render(data) {
    var host = document.getElementById(ID);
    if (!host) { return; }

    var stations = data.stations || [];
    var predict = data.predict || {};
    var tnRule = (rules && rules.TN) || null;

    var target = stations.filter(function (s) { return s.role === 'target'; }).length;
    var upstream = stations.length - target;

    /* 把 predict（键是 ST10 这种码）和 stations（id 是 st10）对上 */
    var byCode = {};
    stations.forEach(function (s) { byCode[String(s.code || s.id).toUpperCase()] = s; });

    var rows = [];
    Object.keys(predict).forEach(function (code) {
      var r = predict[code] || {};
      var tn = (r.actual_TN !== undefined && r.actual_TN !== null) ? Number(r.actual_TN)
                                                                : Number(r.predicted_TN);
      if (isNaN(tn)) { return; }
      var isPred = (r.actual_TN === undefined || r.actual_TN === null);
      var st = byCode[String(code).toUpperCase()] || {};
      rows.push({
        code: code,
        name: st.name || code,
        tn: tn,
        predicted: isPred,
        date: r.last_recorded_date || null,
        level: tnRule ? WQ.grade.levelOf('TN', tn, tnRule) : 'unknown'
      });
    });

    var alarmRows = rows.filter(function (x) { return x.level === 'alarm'; });
    var avg = rows.length ? rows.reduce(function (a, b) { return a + b.tn; }, 0) / rows.length : null;
    var worst = rows.slice().sort(function (a, b) { return b.tn - a.tn; })[0] || null;

    /* 新鲜度：取最旧的那条记录 */
    var oldest = null;
    rows.forEach(function (r) {
      var d = daysSince(r.date);
      if (d === null) { return; }
      if (!oldest || d > oldest.days) { oldest = { days: d, date: r.date, name: r.name }; }
    });

    var alarmColor = alarmRows.length ? WQ.grade.COLORS.alarm : WQ.grade.COLORS.normal;
    var freshColor = (oldest && oldest.days > 7) ? WQ.grade.COLORS.warn : null;

    host.innerHTML =
      card('▣', '断面总数', stations.length, '个',
           '预测 ' + target + ' · 上游 ' + upstream) +
      card('!', '总氮超标', alarmRows.length, '个',
           (rows.length ? ('判定 ' + rows.length + ' 个断面') : '暂无数据'), alarmColor) +
      card('◈', '平均总氮', avg === null ? '--' : avg.toFixed(2), 'mg/L',
           avg === null ? '暂无数据'
             : ('参与统计 ' + rows.length + ' 个' + (rows.some(function (x) { return x.predicted; }) ? '（含预测值）' : ''))) +
      card('◉', '最差断面', worst ? worst.name : '--', '',
           worst ? ('总氮 ' + worst.tn.toFixed(2) + ' mg/L · ' + WQ.grade.label(worst.level)) : '暂无数据',
           worst ? WQ.grade.color(worst.level) : null) +
      card('◷', '数据新鲜度', oldest ? oldest.days : '--', oldest ? '天前' : '',
           oldest ? ('最旧 ' + oldest.date) : '暂无数据', freshColor);
  }

  /* 必须走 WQ.data / WQ.api：它们在闭包里自动带 Authorization 头。
   * 直接 fetch 拿不到 token，会全部 401（踩过）。 */
  function load() {
    return Promise.all([
      WQ.data.listStations().catch(function () { return []; }),
      WQ.data.rules().catch(function () { return {}; }),
      WQ.data.predict().catch(function () { return {}; })
    ]).then(function (r) {
      /* 防御性解包：不同方法有的返回裸数据、有的返回 {success,data}，
       * 这里统一成裸数据，避免"卡片一直是 --"这种静默失败（踩过）。 */
      function unwrap(x) {
        if (x && !Array.isArray(x) && x.data !== undefined) { return x.data; }
        return x;
      }
      rules = unwrap(r[1]) || {};
      render({ stations: unwrap(r[0]) || [], predict: unwrap(r[2]) || {} });
    }).catch(function () { /* 任一失败就不显示，不影响主界面 */ });
  }

  function mount() {
    if (document.getElementById(ID)) { return; }
    var mapEl = document.getElementById('map');
    if (!mapEl) { return; }
    var bar = document.createElement('div');
    bar.id = ID;
    bar.className = 'wq-kpi-bar';
    mapEl.parentNode.insertBefore(bar, mapEl);
  }

  /* 登录后才能拿到数据：先挂载，boot 后**必须重新 load**，
   * 否则卡片会一直停在登录前的空值（踩过）。 */
  if (window.WQ && WQ.bus && WQ.bus.on) {
    WQ.bus.on('boot', function () { setTimeout(function () { mount(); load(); }, 300); });
  }
  document.addEventListener('DOMContentLoaded', function () {
    setTimeout(mount, 1500);
  });
})();