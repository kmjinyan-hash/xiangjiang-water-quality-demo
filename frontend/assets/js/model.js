/* ══════════════════════════════════════════════════════════════════════════
 * model.js — ⑥ 模型简报弹窗（只读）
 *
 * 数据只来自 GET /api/model_eval，它读的是训练时落盘的验证集指标，不重算。
 *
 * 诚实边界（负责人明确要求，不许美化）：
 *   · caveat 字段**原样显示**，不改写、不省略；
 *   · 页面不写"按时间切分验证"这类元数据里没有的结论；
 *   · 指标是训练期验证集结果，不是上线实测 —— 页面上明说。
 * ══════════════════════════════════════════════════════════════════════════ */
(function (WQ) {
  'use strict';

  var $ = WQ.$, esc = WQ.esc, num = WQ.num;

  var METRIC_COLS = [
    { k: 'MAE', label: 'MAE', d: 3, hint: '平均绝对误差（mg/L）' },
    { k: 'RMSE', label: 'RMSE', d: 3, hint: '均方根误差（mg/L）' },
    { k: 'R2', label: 'R²', d: 3, hint: '决定系数，越接近 1 越好' },
    { k: 'MAPE', label: 'MAPE', d: 1, hint: '平均绝对百分比误差（%）' },
    { k: 'Pearson_r', label: 'Pearson r', d: 3, hint: '皮尔逊相关系数' }
  ];

  function mean(rows, key) {
    var vals = rows.map(function (r) { return Number(r[key]); })
                   .filter(function (v) { return !isNaN(v); });
    if (!vals.length) { return null; }
    return vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
  }

  function tableHtml(d) {
    var metrics = d.metrics || {};
    var codes = Object.keys(metrics).sort();
    if (!codes.length) { return '<p>接口未返回指标。</p>'; }

    var rows = codes.map(function (code) {
      var m = metrics[code] || {};
      return '<tr><td><code>' + esc(code) + '</code></td>' +
        METRIC_COLS.map(function (c) {
          return '<td class="num">' + num(m[c.k], c.d) + '</td>';
        }).join('') + '</tr>';
    }).join('');

    var avg = METRIC_COLS.map(function (c) {
      var v = mean(codes.map(function (code) { return metrics[code] || {}; }), c.k);
      return '<td class="num">' + (v == null ? '--' : num(v, c.d)) + '</td>';
    }).join('');

    return '<div class="wq-tablewrap" style="max-height:34vh">' +
      '<table class="wq-table">' +
      '<thead><tr><th>站点</th>' +
        METRIC_COLS.map(function (c) {
          return '<th title="' + esc(c.hint) + '">' + esc(c.label) + '</th>';
        }).join('') +
      '</tr></thead>' +
      '<tbody>' + rows +
        '<tr class="wq-model__avg"><td><b>5 站点均值</b></td>' + avg + '</tr>' +
      '</tbody></table></div>';
  }

  function scatterHtml(d) {
    var sample = d.sample || {};
    var codes = Object.keys(sample).sort();
    if (!codes.length) {
      return '<p style="color:var(--c-text-dim)">接口未返回预测 / 实测序列，无法画散点。</p>';
    }
    return '<div class="wq-model__chart" id="meScatter"></div>';
  }

  function drawScatter(d) {
    var host = $('#meScatter');
    if (!host) { return; }
    var chart = WQ.charts.get('meScatter');
    var p = WQ.charts.palette();
    var sample = d.sample || {};
    var metrics = d.metrics || {};
    var codes = Object.keys(sample).sort();
    var lo = Infinity, hi = -Infinity;
    var series = [];

    codes.forEach(function (code) {
      var s = sample[code] || {};
      var t = s.true || [], pr = s.pred || [];
      var n = Math.min(t.length, pr.length);
      var data = [];
      for (var i = 0; i < n; i++) {
        var x = Number(t[i]), y = Number(pr[i]);
        if (isNaN(x) || isNaN(y)) { continue; }
        data.push([x, y]);
        if (x < lo) lo = x; if (x > hi) hi = x;
        if (y < lo) lo = y; if (y > hi) hi = y;
      }
      var r2 = (metrics[code] || {}).R2;
      series.push({
        name: code.replace('_TN', '') + (r2 == null ? '' : '（R²=' + num(r2, 2) + '）'),
        type: 'scatter',
        symbolSize: 7,
        data: data,
        itemStyle: { opacity: 0.8 },
        emphasis: { focus: 'series' }
      });
    });

    if (!isFinite(lo) || !isFinite(hi)) {
      host.innerHTML = '<p style="color:var(--c-text-dim)">序列为空，无法画散点。</p>';
      return;
    }
    var pad = (hi - lo) * 0.08 || 0.1;
    var from = Math.max(0, lo - pad), to = hi + pad;

    chart.setOption(Object.assign(WQ.charts.base(), {
      grid: { left: 52, right: 16, top: 34, bottom: 40 },
      legend: { top: 0, textStyle: { color: p.dim, fontSize: 11 }, itemWidth: 12, itemHeight: 8 },
      xAxis: {
        type: 'value', name: '实测 TN（mg/L）', min: from, max: to,
        nameTextStyle: { color: p.faint, fontSize: 10 },
        axisLabel: { color: p.dim, fontSize: 10 },
        splitLine: { lineStyle: { color: p.border, type: 'dashed' } }
      },
      yAxis: {
        type: 'value', name: '预测 TN（mg/L）', min: from, max: to,
        nameTextStyle: { color: p.faint, fontSize: 10 },
        axisLabel: { color: p.dim, fontSize: 10 },
        splitLine: { lineStyle: { color: p.border, type: 'dashed' } }
      },
      tooltip: {
        formatter: function (q) {
          var v = q.value || [];
          var err = Number(v[1]) - Number(v[0]);
          return '<b>' + esc(q.seriesName) + '</b><br>实测 ' + num(v[0], 3) +
                 '<br>预测 ' + num(v[1], 3) +
                 '<br>误差 ' + (err >= 0 ? '+' : '') + num(err, 3) + ' mg/L';
        }
      },
      series: series.concat([{
        name: '1:1 参考线', type: 'line', symbol: 'none', silent: true,
        data: [[from, from], [to, to]],
        lineStyle: { color: p.faint, width: 1, type: 'dashed' },
        tooltip: { show: false },
        z: 1
      }])
    }), true);
  }

  function sampleCount(d) {
    /* 不写死"60"：接口截断多少就用多少，字段变了也不会显示错 */
    var n = 0;
    Object.keys(d.sample || {}).forEach(function (k) {
      var s = d.sample[k] || {};
      n = Math.max(n, (s.true || []).length, (s.pred || []).length);
    });
    return n;
  }

  function open() {
    WQ.data.modelEval().then(function (d) {
      d = d || {};
      var caveat = d.caveat || '（接口未返回 caveat 字段）';
      var note =
        '<div class="wq-model__caveat">' +
          '<div class="wq-model__caveat-h">' +
            WQ.icon('i-warn') +
            '诚实边界（接口原文）' +
          '</div>' +
          '<div class="wq-model__caveat-b">' + esc(caveat) + '</div>' +
          '<div class="wq-model__caveat-f">' +
            '因此本页不声称"按时间切分验证"：元数据里没有记录切分方式，' +
            '任何更具体的验证方式描述都是推测。' +
          '</div>' +
        '</div>';

      var intro =
        '<p class="wq-model__lead">下面的数值是模型<strong>训练时</strong>在验证集上算好、' +
        '随权重一起落盘的，接口只读不算 —— 不重新推理，也不补齐缺失字段。' +
        '单位：MAE / RMSE 为 mg/L，MAPE 为 %。</p>';

      WQ.dialog({
        title: '模型简报 · 总氮预测',
        icon: 'i-model',
        wide: true,
        html: note + intro +
          '<div class="wq-model__sec">指标汇总（按站点）</div>' + tableHtml(d) +
          '<div class="wq-model__sec">预测值与实测值（每站点最近 ' +
            String(sampleCount(d)) + ' 个样本）</div>' +
          scatterHtml(d) +
          '<p class="wq-model__src">模型 <code>' + esc(d.model || '—') + '</code>' +
            '　来源 <code>' + esc(d.source || '—') + '</code></p>',
        actions: [{ label: '关闭', value: null, primary: true }],
        onOpen: function () {
          /* 弹窗插入 DOM 后容器才有尺寸，此时再 init 图表 */
          setTimeout(function () { drawScatter(d); }, 30);
        }
      });
    }).catch(function (e) {
      WQ.toast('模型简报读取失败：' + e.message, 'err');
    });
  }

  WQ.actions['model-eval'] = open;
  WQ.model = { open: open };
})(window.WQ);
