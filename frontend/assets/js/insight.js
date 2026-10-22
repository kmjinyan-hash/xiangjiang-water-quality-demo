/* ══════════════════════════════════════════════════════════════════════════
 * insight.js — 分析看板（④ 断面排行 / ⑧ 雷达 / ⑨ 环形 / ⑩ 日历热力图）
 *
 * 数据纪律：
 *   · 所有数值来自 /api/history（每断面最近 200 条）与 /api/ai_warning_rules；
 *   · 等级一律 WQ.grade.levelOf，占标率一律 WQ.grade.ratioOf —— 前端不写死阈值；
 *   · 预测值只从已加载的 WQ.state.predictions 取（来自 prediction_history），
 *     这里**不触发** /api/predict，避免打开一次看板就跑一遍推理。
 *
 * 口径（与页面说明文字一一对应）：
 *   占标率 = 实测 / 超标限值，100% 即刚好超标；DO 取 alarm/实测（反向）；
 *   等级构成 = 每个断面「最新一条记录」里最差的等级；
 *   首要超标指标 = 超标断面里占标率最高的那一项；
 *   日历热力 = 当天「存在任一指标超标」的断面个数。
 * ══════════════════════════════════════════════════════════════════════════ */
(function (WQ) {
  'use strict';

  var $ = WQ.$, esc = WQ.esc;
  var SERIES = {};          // sid -> rows
  var pending = null;

  var METRIC_ORDER = ['TN', 'TP', 'CODMN', 'NH3N', 'DO', 'NTU', 'TDS', 'PH'];
  var RADAR_KEYS = ['TN', 'TP', 'CODMN', 'NH3N', 'DO', 'NTU', 'TDS'];
  var NAME_FALLBACK = { TEM: '水温', NH3N: '氨氮' };

  function rules() { return WQ.state.rules || {}; }
  function ruleOf(k) { return rules()[k] || null; }
  function labelOf(k) {
    var r = ruleOf(k);
    return (r && r.label) || NAME_FALLBACK[k] || k;
  }
  function unitOf(k) {
    var r = ruleOf(k);
    return (r && r.unit) || '';
  }

  /* ── 数据 ─────────────────────────────────────────────────────────── */

  function loadSeries(force) {
    if (pending && !force) { return pending; }
    var list = WQ.state.stations || [];
    SERIES = {};
    pending = Promise.all(list.map(function (s) {
      return WQ.data.history(s.id, 200).then(function (rows) {
        SERIES[s.id] = rows || [];
      }).catch(function () { SERIES[s.id] = []; });
    })).then(function () { return SERIES; });
    return pending;
  }

  function latestRow(sid) {
    var rows = SERIES[sid] || [];
    return rows.length ? rows[rows.length - 1] : null;
  }

  function hasNumber(v) {
    return !(v === null || v === undefined || v === '' || isNaN(Number(v)));
  }

  /* 一行记录里每个「有规则且有值」的指标：值、等级、占标率 */
  function rowMetrics(row) {
    var out = [];
    if (!row) { return out; }
    Object.keys(rules()).forEach(function (k) {
      if (!hasNumber(row[k])) { return; }
      var r = rules()[k];
      out.push({
        key: k,
        value: Number(row[k]),
        lv: WQ.grade.levelOf(k, row[k], r),
        ratio: WQ.grade.ratioOf(k, row[k], r)
      });
    });
    return out;
  }

  function worstOf(row) {
    if (!row) { return 'unknown'; }
    return WQ.grade.worst(rowMetrics(row).map(function (m) { return m.lv; }));
  }

  function visibleStations() {
    return (WQ.shell.visibleStations ? WQ.shell.visibleStations() : (WQ.state.stations || []));
  }

  function showEmpty(chart, text) {
    if (!chart) { return; }
    chart.clear();
    chart.setOption({
      graphic: [{
        type: 'text', left: 'center', top: 'middle', silent: true,
        style: { text: text, fill: WQ.charts.palette().dim, fontSize: 13 }
      }]
    });
  }

  /* ── ④ 断面排行 ───────────────────────────────────────────────────── */

  function rankEntries(metric) {
    var out = [];
    visibleStations().forEach(function (s) {
      var row = latestRow(s.id);
      if (!row || !hasNumber(row[metric])) { return; }
      out.push({
        station: s,
        value: Number(row[metric]),
        lv: WQ.grade.levelOf(metric, row[metric], ruleOf(metric)),
        ratio: WQ.grade.ratioOf(metric, row[metric], ruleOf(metric)),
        date: WQ.dateOnly(row.DATE)
      });
    });
    return out.sort(function (a, b) { return b.value - a.value; });
  }

  function drawRank() {
    var host = $('#chartRank');
    if (!host) { return; }
    var metric = $('#insMetric').value || 'TN';
    var chart = WQ.charts.get('chartRank');
    var entries = rankEntries(metric);
    var desc = $('#rankDesc');

    if (!entries.length) {
      showEmpty(chart, '暂无' + labelOf(metric) + '数据');
      if (desc) { desc.textContent = '所选指标在当前筛选范围内没有可用记录'; }
      return;
    }

    var p = WQ.charts.palette();
    var unit = unitOf(metric);
    var cats = entries.map(function (e) { return e.station.name; });
    var bars = entries.map(function (e) {
      return {
        value: e.value,
        itemStyle: { color: WQ.grade.color(e.lv), borderRadius: [0, 2, 2, 0] }
      };
    });

    /* TN 额外叠加「预测值」点：只有预测断面有，其余断面留空。
     * 用点而不是第二根条 —— 两种口径（实测/预测）混在同一个长度轴上会误导。 */
    var predSeries = null;
    if (metric === 'TN') {
      var predData = entries.map(function (e) {
        var pr = (WQ.state.predictions || {})[e.station.code];
        return (pr && hasNumber(pr.predicted_TN)) ? Number(pr.predicted_TN) : null;
      });
      if (predData.some(function (v) { return v != null; })) {
        predSeries = {
          name: '预测 TN（次日）', type: 'scatter', data: predData,
          symbol: 'diamond', symbolSize: 9,
          /* 抬高一点：同一分类上条末数值标签与预测点会撞在一起 */
          symbolOffset: [0, -10],
          itemStyle: { color: p.warn, borderColor: '#fff', borderWidth: 1 },
          tooltip: { formatter: function (q) { return q.name + '<br>预测 ' + WQ.num(q.value, 2) + ' mg/L'; } },
          z: 5
        };
      }
    }

    var series = [{
      name: '实测 ' + labelOf(metric), type: 'bar', data: bars,
      barMaxWidth: 14,
      label: {
        show: true, position: 'right', color: p.dim, fontSize: 11,
        formatter: function (q) { return WQ.num(q.value, metric === 'PH' ? 2 : 2); }
      },
      tooltip: {
        formatter: function (q) {
          var e = entries[q.dataIndex];
          var lines = [
            '<b>' + esc(e.station.name) + '</b>（' + esc(e.station.code) + '）',
            labelOf(metric) + ' ' + WQ.num(e.value, 2) + (unit ? ' ' + unit : ''),
            '等级 ' + WQ.grade.label(e.lv) + (e.ratio == null ? '' : '　占标 ' + (e.ratio * 100).toFixed(0) + '%'),
            '数据日期 ' + e.date
          ];
          return lines.join('<br>');
        }
      }
    }];
    if (predSeries) { series.push(predSeries); }

    chart.setOption(Object.assign(WQ.charts.base(), {
      grid: { left: 66, right: 56, top: predSeries ? 30 : 12, bottom: 8, containLabel: false },
      legend: predSeries
        ? { top: 0, right: 4, textStyle: { color: p.dim, fontSize: 11 }, itemWidth: 14, itemHeight: 8 }
        : { show: false },
      xAxis: {
        type: 'value',
        name: unit ? unit : '',
        nameTextStyle: { color: p.faint, fontSize: 10 },
        axisLabel: { color: p.dim, fontSize: 10 },
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { lineStyle: { color: p.border, type: 'dashed' } }
      },
      yAxis: {
        type: 'category', inverse: true, data: cats,
        axisLine: { lineStyle: { color: p.border } },
        axisTick: { show: false },
        axisLabel: { color: p.dim, fontSize: 11 },
        splitLine: { show: false }
      },
      series: series
    }), true);

    if (desc) {
      var alarmN = entries.filter(function (e) { return e.lv === 'alarm'; }).length;
      desc.textContent = '按' + labelOf(metric) + '最新监测值降序 · 共 ' + entries.length +
        ' 个断面 · 超标 ' + alarmN + ' 个' +
        (predSeries ? ' · 菱形为预测值' : '');
    }
  }

  /* ── ⑧ 占标率雷达 ─────────────────────────────────────────────────── */

  function radarValues(sid) {
    var row = latestRow(sid);
    var vals = [], missing = [];
    RADAR_KEYS.forEach(function (k) {
      if (!ruleOf(k)) { return; }               // 规则表里没有的指标不上雷达
      var ratio = row ? WQ.grade.ratioOf(k, row[k], ruleOf(k)) : null;
      if (ratio == null) { missing.push(k); ratio = 0; }
      vals.push({ key: k, ratio: ratio });
    });
    return { vals: vals, missing: missing, row: row };
  }

  function drawRadar() {
    var host = $('#chartRadar');
    if (!host) { return; }
    var sid = $('#insStation').value;
    var chart = WQ.charts.get('chartRadar');
    var desc = $('#radarDesc');
    var s = (WQ.state.stations || []).filter(function (x) { return x.id === sid; })[0];

    if (!s) {
      showEmpty(chart, '请选择断面');
      return;
    }
    var mine = radarValues(sid);
    if (!mine.row) {
      showEmpty(chart, s.name + ' 暂无历史数据');
      return;
    }

    /* 全站均值作为对照线：单站雷达缺参照，看不出这个站是好还是坏 */
    var avg = {};
    var n = 0;
    visibleStations().forEach(function (st) {
      var row = latestRow(st.id);
      if (!row) { return; }
      n++;
      RADAR_KEYS.forEach(function (k) {
        var ratio = WQ.grade.ratioOf(k, row[k], ruleOf(k));
        if (ratio == null) { return; }
        avg[k] = (avg[k] || 0) + ratio;
      });
    });

    var p = WQ.charts.palette();
    var maxR = 1;
    mine.vals.forEach(function (v) { maxR = Math.max(maxR, v.ratio); });
    RADAR_KEYS.forEach(function (k) { if (avg[k]) { maxR = Math.max(maxR, avg[k] / Math.max(1, n)); } });

    /* 轴上限封顶 200%：有一个指标（常见是浊度）冲到 300% 以上时，
     * 不封顶会把其余六个轴全压成一条线，图形失去意义。
     * 超出的值按轴上限画，并在说明里写清实际值 —— 不静默截断。 */
    var CAP = 2;
    var over = maxR > CAP;
    var axisMax = over ? CAP : Math.ceil(maxR * 2) / 2;

    var ind = [], mineData = [], avgData = [], clipped = null;
    mine.vals.forEach(function (v) {
      var miss = mine.missing.indexOf(v.key) >= 0;
      ind.push({
        name: labelOf(v.key) + (miss ? ' ✻' : ''),
        max: axisMax
      });
      if (v.ratio > axisMax && (!clipped || v.ratio > clipped.ratio)) {
        clipped = { key: v.key, ratio: v.ratio };
      }
      mineData.push(Number(Math.min(v.ratio, axisMax).toFixed(3)));
      avgData.push(avg[v.key] ? Number(Math.min(avg[v.key] / Math.max(1, n), axisMax).toFixed(3)) : 0);
    });

    chart.setOption(Object.assign(WQ.charts.base(), {
      legend: {
        top: 0, right: 4, textStyle: { color: p.dim, fontSize: 11 },
        itemWidth: 14, itemHeight: 8, data: [s.name, '全站均值']
      },
      tooltip: {
        trigger: 'item',
        formatter: function (q) {
          return '<b>' + esc(q.name) + '</b><br>' + q.seriesName + '：' + (q.value * 100).toFixed(0) + '%';
        }
      },
      radar: {
        center: ['50%', '55%'],
        radius: '64%',
        indicator: ind,
        splitNumber: 4,
        axisName: { color: p.dim, fontSize: 10 },
        splitLine: { lineStyle: { color: p.border } },
        splitArea: { areaStyle: { color: ['transparent'] } },
        axisLine: { lineStyle: { color: p.border } }
      },
      series: [{
        type: 'radar',
        symbolSize: 4,
        data: [
          {
            name: s.name,
            value: mineData,
            lineStyle: { color: p.accent, width: 2 },
            itemStyle: { color: p.accent },
            areaStyle: { color: p.accent, opacity: 0.22 }
          },
          {
            name: '全站均值',
            value: avgData,
            lineStyle: { color: p.faint, width: 1.2, type: 'dashed' },
            itemStyle: { color: p.faint },
            areaStyle: { opacity: 0 }
          }
        ]
      }]
    }), true);

    if (desc) {
      var worst = mine.vals.slice().sort(function (a, b) { return b.ratio - a.ratio; })[0];
      desc.textContent = s.name + ' · 100% = 刚好超标' +
        (worst ? ' · 最高 ' + labelOf(worst.key) + ' ' + (worst.ratio * 100).toFixed(0) + '%' : '') +
        (clipped ? '（超出轴上限 ' + (axisMax * 100).toFixed(0) + '%，按上限绘制）' : '') +
        (mine.missing.length ? ' · ✻ 本批数据未上报（按 0 计）' : '');
    }
  }

  /* ── ⑨ 环形图 ─────────────────────────────────────────────────────── */

  function levelCounts() {
    var c = { normal: 0, warn: 0, alarm: 0, unknown: 0 };
    visibleStations().forEach(function (s) {
      var row = latestRow(s.id);
      c[worstOf(row)] = (c[worstOf(row)] || 0) + 1;
    });
    return c;
  }

  function drawLevelDonut() {
    var chart = WQ.charts.get('chartLevel');
    var desc = $('#levelDesc');
    var c = levelCounts();
    var total = c.normal + c.warn + c.alarm + c.unknown;
    var p = WQ.charts.palette();

    if (!total) {
      showEmpty(chart, '暂无数据');
      return;
    }
    var order = ['alarm', 'warn', 'normal', 'unknown'];
    var data = order.filter(function (k) { return c[k] > 0; }).map(function (k) {
      return {
        name: WQ.grade.label(k),
        value: c[k],
        itemStyle: { color: WQ.grade.color(k) }
      };
    });

    chart.setOption(Object.assign(WQ.charts.base(), {
      tooltip: { trigger: 'item', formatter: '{b}：{c} 个断面（{d}%）' },
      legend: { bottom: 0, textStyle: { color: p.dim, fontSize: 11 }, itemWidth: 12, itemHeight: 8 },
      title: {
        text: String(total),
        subtext: '断面',
        left: 'center', top: '36%',
        textStyle: { color: p.text, fontSize: 22, fontWeight: 600 },
        subtextStyle: { color: p.faint, fontSize: 11 }
      },
      series: [{
        type: 'pie',
        radius: ['48%', '70%'],
        center: ['50%', '46%'],
        avoidLabelOverlap: true,
        label: { color: p.dim, fontSize: 11, formatter: '{b} {c}' },
        labelLine: { lineStyle: { color: p.border }, length: 8, length2: 8 },
        itemStyle: { borderColor: p.panel, borderWidth: 2 },
        data: data
      }]
    }), true);

    if (desc) {
      desc.textContent = '共 ' + total + ' 个断面 · 超标 ' + c.alarm + ' · 预警 ' + c.warn +
        ' · 正常 ' + c.normal + (c.unknown ? ' · 未判 ' + c.unknown : '');
    }
  }

  function causeCounts() {
    var counts = {};
    var alarmStations = 0;
    visibleStations().forEach(function (s) {
      var row = latestRow(s.id);
      if (!row) { return; }
      var list = rowMetrics(row);
      var worst = WQ.grade.worst(list.map(function (m) { return m.lv; }));
      if (worst !== 'alarm') { return; }
      alarmStations++;
      var pick = list.filter(function (m) { return m.ratio != null; })
                     .sort(function (a, b) { return b.ratio - a.ratio; })[0];
      if (!pick) { return; }
      counts[pick.key] = (counts[pick.key] || 0) + 1;
    });
    return { counts: counts, stations: alarmStations };
  }

  function drawCauseDonut() {
    var chart = WQ.charts.get('chartCause');
    var desc = $('#causeDesc');
    var res = causeCounts();
    var p = WQ.charts.palette();
    var keys = Object.keys(res.counts).sort(function (a, b) { return res.counts[b] - res.counts[a]; });

    if (!keys.length) {
      showEmpty(chart, res.stations ? '超标断面无可比占标率' : '本期无超标断面');
      if (desc) { desc.textContent = '当前筛选范围内没有判定为超标的断面'; }
      return;
    }

    /* 首项用超标红，其余用一组中性色：这一圈表示"原因构成"，不是等级，
     * 全用红色会让人误以为是四个不同严重度。 */
    var rest = ['#d29922', '#4c8dff', '#9b7bd8', '#5aa9a3', '#b0703a'];
    var data = keys.map(function (k, i) {
      return {
        name: labelOf(k),
        value: res.counts[k],
        itemStyle: { color: i === 0 ? p.alarm : rest[(i - 1) % rest.length] }
      };
    });

    chart.setOption(Object.assign(WQ.charts.base(), {
      tooltip: { trigger: 'item', formatter: '{b}：{c} 个断面（{d}%）' },
      legend: { bottom: 0, textStyle: { color: p.dim, fontSize: 11 }, itemWidth: 12, itemHeight: 8 },
      title: {
        text: String(res.stations),
        subtext: '超标断面',
        left: 'center', top: '36%',
        textStyle: { color: p.text, fontSize: 22, fontWeight: 600 },
        subtextStyle: { color: p.faint, fontSize: 11 }
      },
      series: [{
        type: 'pie',
        radius: ['48%', '70%'],
        center: ['50%', '46%'],
        label: { color: p.dim, fontSize: 11, formatter: '{b} {c}' },
        labelLine: { lineStyle: { color: p.border }, length: 8, length2: 8 },
        itemStyle: { borderColor: p.panel, borderWidth: 2 },
        data: data
      }]
    }), true);

    if (desc) {
      desc.textContent = res.stations + ' 个超标断面 · 首要超标指标：' +
        keys.map(function (k) { return labelOf(k) + ' ' + res.counts[k]; }).join('、');
    }
  }

  /* ── ⑩ 日历热力图 ─────────────────────────────────────────────────── */

  function calendarData() {
    var byDate = {};          // 'YYYY-MM-DD' -> {alarm, total}
    visibleStations().forEach(function (s) {
      (SERIES[s.id] || []).forEach(function (row) {
        var d = WQ.dateOnly(row.DATE);
        if (!d || d === '--') { return; }
        var rec = byDate[d] || (byDate[d] = { alarm: 0, total: 0 });
        rec.total++;
        if (worstOf(row) === 'alarm') { rec.alarm++; }
      });
    });
    var dates = Object.keys(byDate).sort();
    var maxAlarm = 0;
    dates.forEach(function (d) { maxAlarm = Math.max(maxAlarm, byDate[d].alarm); });
    return { byDate: byDate, dates: dates, maxAlarm: maxAlarm };
  }

  function drawCalendar() {
    var chart = WQ.charts.get('chartCalendar');
    var desc = $('#calDesc');
    var d = calendarData();
    var p = WQ.charts.palette();

    if (!d.dates.length) {
      showEmpty(chart, '暂无按日数据');
      return;
    }

    var data = d.dates.map(function (day) {
      return { value: [day, d.byDate[day].alarm], total: d.byDate[day].total };
    });
    var span = d.dates.length;
    var alarmDays = d.dates.filter(function (x) { return d.byDate[x].alarm > 0; }).length;

    chart.setOption(Object.assign(WQ.charts.base(), {
      tooltip: {
        formatter: function (q) {
          var v = q.value || [];
          return '<b>' + v[0] + '</b><br>超标断面 ' + v[1] + ' 个<br>有数据断面 ' +
                 ((q.data && q.data.total) || 0) + ' 个';
        }
      },
      visualMap: {
        min: 0, max: Math.max(1, d.maxAlarm),
        orient: 'horizontal', left: 'center', bottom: 0,
        itemWidth: 10, itemHeight: 90,
        text: ['多', '少'],
        textStyle: { color: p.dim, fontSize: 10 },
        inRange: { color: [p.ok, p.warn, p.alarm] },
        calculable: false
      },
      calendar: {
        range: [d.dates[0], d.dates[span - 1]],
        cellSize: ['auto', 15],
        top: 26, left: 34, right: 12, bottom: 30,
        orient: 'horizontal',
        splitLine: { show: false },
        itemStyle: { color: 'transparent', borderWidth: 1, borderColor: p.border },
        dayLabel: { color: p.dim, fontSize: 10, nameMap: 'ZH', firstDay: 1 },
        monthLabel: { color: p.dim, fontSize: 10, nameMap: 'ZH' },
        yearLabel: { show: false }
      },
      series: [{
        type: 'heatmap',
        coordinateSystem: 'calendar',
        data: data,
        itemStyle: { borderColor: p.panel, borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: p.text, borderWidth: 1 } }
      }]
    }), true);

    if (desc) {
      desc.textContent = d.dates[0] + ' ~ ' + d.dates[span - 1] + ' · 覆盖 ' + span +
        ' 天 · 有超标 ' + alarmDays + ' 天 · 单日最多 ' + d.maxAlarm + ' 个断面';
    }
  }

  /* ── 视图装配 ─────────────────────────────────────────────────────── */

  function initSelectors() {
    var ms = $('#insMetric');
    if (ms && !ms.options.length) {
      var keys = METRIC_ORDER.filter(function (k) {
        if (!ruleOf(k)) { return false; }
        return true;
      });
      ms.innerHTML = keys.map(function (k) {
        return '<option value="' + k + '">' + esc(labelOf(k)) + '</option>';
      }).join('');
    }
    var ss = $('#insStation');
    if (ss && !ss.options.length) {
      var targets = (WQ.state.stations || []).filter(function (s) { return s.role === 'target'; });
      var pool = targets.length ? targets : (WQ.state.stations || []);
      ss.innerHTML = pool.map(function (s) {
        return '<option value="' + esc(s.id) + '">' + esc(s.name) + '</option>';
      }).join('');
    }
    /* 左侧目录选中断面时，雷达图跟随 —— 与地图/图表的联动方式保持一致 */
    if (ss && WQ.state.selected) {
      var cur = (WQ.state.stations || []).filter(function (x) { return x.id === WQ.state.selected; })[0];
      if (cur) { ss.value = cur.id; }
    }
  }

  function renderAll() {
    var meta = $('#insMeta');
    var n = visibleStations().length;
    var withData = visibleStations().filter(function (s) { return !!latestRow(s.id); }).length;
    if (meta) { meta.textContent = '断面 ' + n + ' 个 · 有数据 ' + withData + ' 个'; }
    drawRank();
    drawRadar();
    drawLevelDonut();
    drawCauseDonut();
    drawCalendar();
  }

  var reloading = null;
  function refresh(force) {
    reloading = loadSeries(force).then(function () {
      renderAll();
      return true;
    }).catch(function (e) {
      WQ.toast('分析看板读取失败：' + e.message, 'err');
    });
    return reloading;
  }

  var bound = false;
  function bindOnce() {
    if (bound) return;
    bound = true;
    var m = $('#insMetric'), s = $('#insStation');
    if (m) m.addEventListener('change', function () { drawRank(); });
    if (s) s.addEventListener('change', function () { drawRadar(); });
  }

  WQ.views = WQ.views || {};
  WQ.views.insight = {
    show: function () {
      initSelectors();
      bindOnce();
      /* 等视图真正可见（display:flex）再量尺寸，否则 ECharts 得到 0 宽 */
      setTimeout(function () { refresh(false); }, 40);
    },
    hide: function () { },
    onResize: function () { if (WQ.state.view === 'insight') WQ.charts.resizeAll(); },
    onTheme: function () { if (WQ.state.view === 'insight') { setTimeout(renderAll, 0); } },
    onSelect: function (sid) {
      var s = $('#insStation');
      if (s) {
        var cur = (WQ.state.stations || []).filter(function (x) { return x.id === sid; })[0];
        if (cur && Array.prototype.some.call(s.options, function (o) { return o.value === sid; })) {
          s.value = sid;
          if (WQ.state.view === 'insight') drawRadar();
        }
      }
    },
    onData: function () { if (WQ.state.view === 'insight') refresh(true); },
    refresh: refresh,
    /* 给导出/时间轴复用：同一份历史数据不重复拉取 */
    series: function () { return loadSeries(false); },
    rankEntries: rankEntries,
    calendarData: calendarData
  };
})(window.WQ);
