/* ══════════════════════════════════════════════════════════════════════════
 * docs.js — ⑤ 数据说明页
 *
 * 硬性要求：**评价标准表必须由 /api/ai_warning_rules 渲染**，不能写死在前端。
 * 这里连"有哪些字段、字段叫什么、单位是什么"都尽量从接口取：
 *   · 阈值/单位/名称 -> /api/ai_warning_rules
 *   · 断面台账       -> /api/stations
 *   · 实际字段清单   -> /api/history/<sid>?limit=1 的列名
 *   · 模型诚实边界   -> /api/model_eval 的 caveat 原文
 * 取不到的（例如 data_status 为空）就如实写"接口未返回"，不编一个数出来。
 * ══════════════════════════════════════════════════════════════════════════ */
(function (WQ) {
  'use strict';

  var $ = WQ.$, esc = WQ.esc;

  function swatch(color, label) {
    return '<span class="wq-swatch"><i style="background:' + esc(color) + '"></i>' + esc(label) + '</span>';
  }

  /* 阈值行的文字化：三种判定方向分别写成"≥ / ≤ / 区间外" */
  function ruleCells(r) {
    var u = r.unit ? ' ' + r.unit : '';
    if (r.isRange) {
      return {
        warn: '&lt; ' + r.lowWarn + ' 或 &gt; ' + r.highWarn + u,
        alarm: '≤ ' + r.lowAlarm + ' 或 ≥ ' + r.highAlarm + u,
        dir: '区间型：落在 [' + r.lowWarn + ', ' + r.highWarn + '] 内为正常'
      };
    }
    if (r.lowerIsWorse) {
      return {
        warn: '≤ ' + r.warn + u,
        alarm: '≤ ' + r.alarm + u,
        dir: '越低越差（溶解氧）'
      };
    }
    return {
      warn: '≥ ' + r.warn + u,
      alarm: '≥ ' + r.alarm + u,
      dir: '越高越差'
    };
  }

  function build(ctx) {
    var rules = ctx.rules || {};
    var keys = Object.keys(rules);
    var stations = ctx.stations || [];
    var targets = stations.filter(function (s) { return s.role === 'target'; }).length;
    var upstream = stations.length - targets;

    /* ── 字段清单：以接口真实的列名为准，单位/名称能对上规则的用规则表 ── */
    var fieldKeys = [];
    (ctx.columns || []).forEach(function (c) { if (c !== 'DATE') { fieldKeys.push(c); } });
    keys.forEach(function (k) { if (fieldKeys.indexOf(k) < 0) { fieldKeys.push(k); } });

    var fieldRows = fieldKeys.map(function (k) {
      var r = rules[k];
      var inData = (ctx.columns || []).indexOf(k) >= 0;
      return '<tr>' +
        '<td><code>' + esc(k) + '</code></td>' +
        '<td>' + esc(r ? (r.label || k) : (k + '（阈值表未收录）')) + '</td>' +
        '<td>' + esc(r && r.unit ? r.unit : (k === 'TEM' ? '℃' : '—')) + '</td>' +
        '<td style="white-space:normal">' + (r
          ? '参与预警判定，阈值见下表'
          : '仅展示，不参与预警判定') + '</td>' +
        '<td>' + (inData ? '有' : '<span style="color:var(--c-warn)">本批数据未见</span>') + '</td>' +
      '</tr>';
    }).join('');

    var ruleRows = keys.map(function (k) {
      var r = rules[k];
      var c = ruleCells(r);
      return '<tr>' +
        '<td>' + esc(r.label || k) + '</td>' +
        '<td><code>' + esc(k) + '</code></td>' +
        '<td>' + esc(r.unit || '—') + '</td>' +
        '<td class="wq-lv--text lv-warn">' + c.warn + '</td>' +
        '<td class="wq-lv--text lv-alarm">' + c.alarm + '</td>' +
        '<td style="white-space:normal">' + esc(c.dir) + '</td>' +
      '</tr>';
    }).join('');

    var levelRow =
      '<div class="wq-doc__levels">' +
        swatch(WQ.grade.color('normal'), '正常') +
        swatch(WQ.grade.color('warn'), '预警') +
        swatch(WQ.grade.color('alarm'), '超标') +
        swatch(WQ.grade.color('unknown'), '未判 / 无数据') +
      '</div>';

    /* 已知限制里的数字全部来自接口，没有就写"接口未返回" */
    var predictFields = (ctx.predictFields || []).join('、') || '接口未返回';
    var caveat = ctx.caveat
      ? '<span class="wq-doc__quote">' + esc(ctx.caveat) + '</span>'
      : '<span style="color:var(--c-text-faint)">模型简报接口未返回说明字段</span>';

    return '' +
      '<h1 class="wq-doc__h1">数据说明</h1>' +
      '<p class="wq-doc__lead">本页说明平台里的数据从哪来、字段什么意思、按什么标准判级，' +
        '以及现在还做不到什么。页面自身不保存任何业务数据，所有内容都是打开时从 ' +
        '<code>/api/*</code> 现取的。</p>' +

      '<div class="wq-doc__stats">' +
        '<div><b>' + stations.length + '</b><span>断面（预测 ' + targets + ' · 上游 ' + upstream + '）</span></div>' +
        '<div><b>' + keys.length + '</b><span>阈值表指标</span></div>' +
        '<div><b>' + (ctx.latestDate || '—') + '</b><span>最新监测日期</span></div>' +
        '<div><b>' + (ctx.readAt || '—') + '</b><span>本页读取时间</span></div>' +
      '</div>' +

      '<h4>一、数据来源</h4>' +
      '<p>监测数据来自流域内的国控 / 省控水质自动监测站，按站点逐日序列存放于后端数据库，' +
        '前端只通过接口读取：</p>' +
      '<ul class="wq-doc__ul">' +
        '<li><code>GET /api/stations</code> —— 断面台账（编号、名称、河流、城市、经纬度、角色）</li>' +
        '<li><code>GET /api/history/&lt;断面&gt;</code> —— 单断面历史序列（属性表、趋势图、本页字段清单）</li>' +
        '<li><code>GET /api/predict</code> / <code>GET /api/prediction_history/&lt;断面&gt;</code> —— 模型预测与预测历史</li>' +
        '<li><code>GET /api/ai_warning_rules</code> —— 本页「评价标准」与全站等级判定的唯一来源</li>' +
        '<li><code>GET /api/model_eval</code> —— 模型简报（训练期指标，只读）</li>' +
      '</ul>' +
      '<p class="wq-doc__note">接口地址与字段以服务端为准；前端不内置任何站点坐标、阈值或监测值。' +
        '换一个前端（Web / 打包 App / 第三方对接）拿到的都是同一份数据。</p>' +

      '<h4>二、字段含义与单位</h4>' +
      '<div class="wq-tablewrap"><table class="wq-table">' +
        '<thead><tr><th>字段</th><th>名称</th><th>单位</th><th>用途</th><th>本批数据</th></tr></thead>' +
        '<tbody>' + (fieldRows || '<tr><td colspan="5">接口未返回字段</td></tr>') + '</tbody>' +
      '</table></div>' +

      '<h4>三、更新机制</h4>' +
      '<ul class="wq-doc__ul">' +
        '<li>监测数据由后端的采集 / 清洗流程写入数据库，再由各 <code>/api/*</code> 接口提供给前端。</li>' +
        '<li>前端<strong>不做任何缓存写入</strong>：每次打开页面、切换断面或点「刷新数据」都会重新请求接口，' +
          '所以看到的永远是后端当时的值。</li>' +
        '<li>模型预测由 <code>GET /api/predict</code> 触发（功能区「运行预测」），结果落库后可通过' +
          '「预测历史」按日期回放。</li>' +
        '<li>本页顶部的「最新监测日期」取自接口返回值，不是写死的示例值。</li>' +
      '</ul>' +

      '<h4>四、评价标准（由接口渲染）</h4>' +
      '<p>下表逐项来自 <code>GET /api/ai_warning_rules</code>。改后端规则文件，这里与全站的颜色、' +
        '等级判定会同时生效 —— 前端没有第二份阈值。</p>' +
      '<div class="wq-tablewrap"><table class="wq-table">' +
        '<thead><tr><th>指标</th><th>字段</th><th>单位</th><th>预警</th><th>超标</th><th>判定方向</th></tr></thead>' +
        '<tbody>' + (ruleRows || '<tr><td colspan="6">阈值表为空</td></tr>') + '</tbody>' +
      '</table></div>' +
      '<p>等级色块（全站统一，地图圆点、表格、环形图、排行条同色）：</p>' + levelRow +
      '<p class="wq-doc__note">「预警 / 超标」是值班预警口径，<strong>不等同于</strong>' +
        '《地表水环境质量标准》（GB 3838-2002）的水质类别评价。' +
        '类别评价按单因子最差项定类，且总氮不参与类别评价；两者不可混用。' +
        'GB 标准限值与评价方法见功能区「方法说明」。</p>' +

      '<h4>五、已知限制</h4>' +
      '<ul class="wq-doc__ul">' +
        '<li><strong>模型只预测总氮（TN）。</strong>当前 <code>/api/predict</code> 返回的字段为：' +
          esc(predictFields) + '。总磷、高锰酸盐指数等指标只有实测值，没有预测值。</li>' +
        '<li><strong>预测断面 ' + targets + ' 个、上游输入断面 ' + upstream + ' 个。</strong>' +
          '排行与环形图按全部断面统计实测值，预测值只出现在预测断面上。</li>' +
        '<li><strong>模型指标是训练期验证集结果，不是上线实测。</strong>' + caveat + '</li>' +
        '<li><strong>单断面单次最多读取 200 条历史记录</strong>（接口上限）。' +
          '日历热力图与时间轴因此覆盖的是"最近这批记录"的时间跨度，图上标了实际起止日期，' +
          '空白格表示当天没有数据，不是"当天正常"。</li>' +
        '<li><strong>数据新鲜度依赖接口。</strong>' + (ctx.statusEmpty
          ? '<code>/api/data_status</code> 当前返回空对象，所以页面改用各断面最新记录日期推算新鲜度。'
          : '<code>/api/data_status</code> 正常返回各断面最新日期与滞后天数。') + '</li>' +
        '<li><strong>占标率的算法。</strong>实测值 ÷ 同一指标的<em>超标</em>限值，100% 即刚好超标；' +
          '溶解氧越低越差，取"超标限值 ÷ 实测值"，方向与其它指标一致；pH 为区间型，不计算单一比值。</li>' +
      '</ul>' +

      '<p class="wq-doc__src">本页内容随接口变化，不含硬编码的阈值、断面或监测值。</p>';
  }

  function render() {
    var host = $('#docsBody');
    var meta = $('#docsMeta');
    if (!host) { return Promise.resolve(); }
    host.innerHTML = '<p style="color:var(--c-text-dim)">正在读取接口…</p>';
    if (meta) { meta.textContent = '正在读取接口…'; }

    return Promise.all([
      WQ.data.listStations().catch(function () { return []; }),
      WQ.data.rules().catch(function () { return {}; }),
      WQ.data.modelEval().catch(function () { return {}; }),
      WQ.data.probe().catch(function () { return {}; })
    ]).then(function (res) {
      var stations = res[0] || [];
      var rules = res[1] || {};
      var evalData = res[2] || {};
      var status = res[3] || {};

      /* 取一个断面的真实列名作为"本批字段"的依据；取不到就退回规则表键集 */
      var sample = (stations.filter(function (s) { return s.role === 'target'; })[0] || stations[0]) || null;
      var columnsP = sample ? WQ.data.history(sample.id, 1).catch(function () { return []; })
                            : Promise.resolve([]);

      return columnsP.then(function (rows) {
        var columns = (rows && rows[0]) ? Object.keys(rows[0]) : [];

        var latestDate = '—';
        Object.keys(status || {}).forEach(function (k) {
          var d = status[k] && status[k].latest_date;
          if (d && (latestDate === '—' || d > latestDate)) { latestDate = d; }
        });
        if (latestDate === '—') {
          Object.keys(WQ.state.predictions || {}).forEach(function (code) {
            var d = (WQ.state.predictions[code] || {}).last_recorded_date;
            if (d && (latestDate === '—' || d > latestDate)) { latestDate = d; }
          });
        }

        var predictFields = [];
        var firstPred = WQ.state.predictions || {};
        var anyCode = Object.keys(firstPred)[0];
        if (anyCode) { predictFields = Object.keys(firstPred[anyCode]); }

        var now = new Date();
        var readAt = now.getFullYear() + '-' +
          ('0' + (now.getMonth() + 1)).slice(-2) + '-' +
          ('0' + now.getDate()).slice(-2) + ' ' +
          ('0' + now.getHours()).slice(-2) + ':' + ('0' + now.getMinutes()).slice(-2);

        host.innerHTML = build({
          rules: rules,
          stations: stations,
          columns: columns,
          latestDate: latestDate,
          readAt: readAt,
          predictFields: predictFields,
          caveat: evalData && evalData.caveat,
          statusEmpty: !(status && Object.keys(status).length)
        });

        if (meta) {
          meta.textContent = '阈值表 ' + Object.keys(rules).length + ' 项 · 断面 ' +
            stations.length + ' 个 · 渲染自 /api/*';
        }
      });
    }).catch(function (e) {
      host.innerHTML = '<p style="color:var(--c-alarm)">数据说明读取失败：' + esc(e.message) + '</p>';
      if (meta) { meta.textContent = '读取失败'; }
    });
  }

  WQ.views = WQ.views || {};
  WQ.views.docs = {
    show: function () { render(); },
    hide: function () { },
    onResize: function () { },
    onTheme: function () { if (WQ.state.view === 'docs') render(); },
    onData: function () { if (WQ.state.view === 'docs') render(); },
    onSelect: function () { },
    render: render
  };
})(window.WQ);
