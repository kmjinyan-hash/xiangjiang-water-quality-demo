/* ══════════════════════════════════════════════════════════════════════════
 * shell.js — 应用外壳：功能区、内容目录、视图切换、状态栏、主题、动作分发
 *
 * 交互遵循桌面 GIS 习惯：
 *   · 单击左侧目录里的断面 → 选中，并同步地图与属性表；
 *   · 功能区按钮统一带 data-action，由这里集中分发（各视图再注册自己的动作）；
 *   · 状态栏常驻显示后端连通性、断面数、数据基准日、末次预测时间。
 * ══════════════════════════════════════════════════════════════════════════ */

(function (WQ) {
  'use strict';

  var $ = WQ.$, $$ = WQ.$$, esc = WQ.esc, icon = WQ.icon;
  var actions = {};
  var views = {};            // 由 views.js 注册：{ map: {show,hide,...}, ... }

  /* ─────────────── 视图切换 ─────────────── */
  function setView(name) {
    if (!views[name]) return;
    var cur = WQ.state.view;
    if (cur && views[cur] && views[cur].hide) views[cur].hide();
    WQ.state.view = name;
    $$('.wq-view').forEach(function (el) { el.dataset.active = String(el.id === 'view-' + name); });
    $$('[data-view]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.view === name)); });
    if (location.hash !== '#/' + name) history.replaceState(null, '', '#/' + name);
    if (views[name].show) views[name].show();
  }

  function setTab(name) {
    $$('#ribbonTabs .wq-tab').forEach(function (b) { b.setAttribute('aria-selected', String(b.dataset.tab === name)); });
    $$('.wq-ribbon__panel').forEach(function (p) { p.dataset.active = String(p.dataset.panel === name); });
  }

  /* ─────────────── 侧栏内容目录 ─────────────── */
  var LEVEL_DOT = { normal: 'lv-ok', warning: 'lv-warn', alarm: 'lv-alarm', unknown: 'lv-unknown' };

  function stationLevel(s) {
    var code = s.code;
    if (WQ.state.reportLevel && WQ.state.reportLevel[code]) return WQ.state.reportLevel[code];
    return null;
  }

  function visibleStations() {
    var f = WQ.state.filter, q = (WQ.state.query || '').trim().toLowerCase();
    return WQ.state.stations.filter(function (s) {
      if (f === 'target' && s.role !== 'target') return false;
      if (f === 'upstream' && s.role !== 'upstream') return false;
      if (!q) return true;
      return (s.name + ' ' + s.id + ' ' + (s.river || '') + ' ' + (s.city || '')).toLowerCase().indexOf(q) >= 0;
    });
  }

  function renderStations() {
    var host = $('#stationGroups');
    if (!host) return;
    var list = visibleStations();
    var groups = [
      { key: 'target', label: '预测断面（模型输出）', role: 'target' },
      { key: 'upstream', label: '上游输入断面', role: 'upstream' }
    ];
    var html = '';
    groups.forEach(function (g) {
      var items = list.filter(function (s) { return s.role === g.role; });
      if (!items.length) return;
      html += '<div class="wq-grouphead">' + icon('i-map') + '<span>' + esc(g.label) + '</span>' +
              '<span class="wq-grouphead__count">' + items.length + '</span></div>';
      html += '<ul class="wq-tree">' + items.map(function (s) {
        var lv = stationLevel(s);
        var dotCls = lv ? (LEVEL_DOT[lv] || 'lv-unknown') : (s.role === 'target' ? 'lv-ok' : 'lv-unknown');
        var meta = '';
        if (s.role === 'target' && WQ.state.status[s.code]) {
          var st = WQ.state.status[s.code];
          meta = st.days_old === null || st.days_old === undefined ? '' : (st.days_old + '天');
        }
        return '<li class="wq-tree__item' + (s.role === 'target' ? ' wq-tree__item--target' : '') + '"' +
               ' data-station="' + esc(s.id) + '"' +
               (WQ.state.selected === s.id ? ' aria-selected="true"' : '') + '>' +
               '<span class="wq-tree__dot ' + dotCls + '"></span>' +
               '<span class="wq-tree__name">' + esc(s.name) + '</span>' +
               '<span class="wq-tree__meta">' + esc(meta || s.code) + '</span></li>';
      }).join('') + '</ul>';
    });
    host.innerHTML = html || '<div class="wq-empty"><div class="wq-empty__title">没有匹配的断面</div></div>';
    var total = WQ.state.stations.length;
    $('#stationCount').textContent = list.length + ' / ' + total;
  }

  function select(sid, opts) {
    opts = opts || {};
    WQ.state.selected = sid;
    $$('#stationGroups [data-station]').forEach(function (el) {
      el.setAttribute('aria-selected', String(el.dataset.station === sid));
    });
    WQ.bus.emit('select', { id: sid, opts: opts });
  }

  /* ─────────────── 状态栏 ─────────────── */
  function setStatus(text, kind) {
    var led = $('#stLed');
    $('#stStatus').textContent = text;
    if (led) led.className = 'wq-status__led ' + (kind === 'err' ? 'lv-alarm' : kind === 'warn' ? 'lv-warn' : 'lv-ok');
  }

  function refreshStatusbar() {
    var st = WQ.state.status || {};
    var codes = Object.keys(st);
    var baseline = '--';
    codes.forEach(function (c) {
      var d = st[c] && st[c].latest_date;
      if (d && (baseline === '--' || d > baseline)) baseline = d;
    });
    var oldest = 0;
    codes.forEach(function (c) { var d = st[c] && st[c].days_old; if (typeof d === 'number' && d > oldest) oldest = d; });
    $('#stBaseline').textContent = baseline === '--' ? '--' : baseline + (oldest ? '（' + oldest + ' 天前）' : '');
    $('#stStations').textContent = WQ.state.stations.length + ' 个';
    $('#stPredict').textContent = WQ.state.predictionsAt
      ? WQ.state.predictionsAt.toTimeString().slice(0, 8) : '--';
    var off = !WQ.state.online;
    setStatus(off ? '后端服务不可达' : '已连接后端服务', off ? 'err' : 'ok');
  }

  function setCoords(text) { var el = $('#stCoords'); if (el) el.textContent = text; }

  /* ─────────────── 主题 ───────────────
   * 主题是**界面偏好，不是业务数据**，所以允许持久化到 localStorage。
   * 项目数据（断面、阈值、报告、用户）一律不落地，只从后端接口现取。 */
  function applyTheme(t) {
    t = t === 'light' ? 'light' : 'dark';
    WQ.state.theme = t;
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('wq.theme', t); } catch (e) { /* 隐私模式忽略 */ }
    WQ.bus.emit('theme', t);
  }
  function toggleTheme() { applyTheme(WQ.state.theme === 'dark' ? 'light' : 'dark'); }

  /* 内容目录：显式的「打开 / 关闭」，并同步按钮的按下态与提示文字。
   * 之前只是把栅格列设成 0 宽 —— 侧栏内容并没有真正消失，
   * 会出现半截露出来的「显示不全」现象（见 app.css 里的配套修改）。 */
  function toggleSide(force) {
    var body = $('#body');
    var hidden = body.dataset.side === 'hidden';
    var show = (typeof force === 'boolean') ? force : hidden;
    body.dataset.side = show ? 'shown' : 'hidden';
    var btn = $('#btnPanel');
    if (btn) {
      btn.setAttribute('aria-pressed', String(show));
      btn.title = show ? '关闭内容目录' : '打开内容目录';
      btn.setAttribute('aria-label', btn.title);
    }
    $$('[data-action="toggle-side"]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(show));
      var label = b.querySelector('span');
      if (label) label.textContent = show ? '关闭内容目录' : '打开内容目录';
    });
    /* 列宽过渡 180ms：过渡过程中与结束后各通知一次，
     * 让地图在动画期间和结束时都保持尺寸正确（只在结束前量一次会留下空白）。 */
    setTimeout(function () { WQ.bus.emit('resize'); }, 60);
    setTimeout(function () { WQ.bus.emit('resize'); }, 230);
  }

  /* ─────────────── 图例 ─────────────── */
  function renderLegend() {
    var host = $('#legendRows');
    if (!host) return;
    var rows = [
      { cls: 'lv-ok', label: '正常 / 上游输入断面' },
      { cls: 'lv-warn', label: '预警' },
      { cls: 'lv-alarm', label: '超标' },
      { cls: 'lv-unknown', label: '未判定 / 无数据' }
    ];
    host.innerHTML = rows.map(function (r) {
      return '<div class="wq-legend__row"><span class="wq-legend__sym ' + r.cls + '"></span>' + esc(r.label) + '</div>';
    }).join('');
  }

  /* ─────────────── 内置对话框动作 ─────────────── */
  actions.about = function () {
    WQ.dialog({
      title: '关于', icon: 'i-info',
      html:
        '<div class="selectable" style="font-size:var(--fs-md)">' +
        '<p><b>' + esc(WQ.cfg.appTitle) + '</b>　' + esc(WQ.cfg.appSubtitle) + '</p>' +
        '<p style="color:var(--c-text-dim)">' +
        '前端与后端是分离的两个部分：本页面是纯静态应用，只通过 <code>/api/*</code> 这一套 JSON 接口取数。' +
        '把整个 <code>frontend/</code> 目录拷到任意静态服务器上，改 <code>app.config.js</code> 里的 ' +
        '<code>apiBase</code> 指向后端即可独立部署（后端需在 <code>WQ_CORS_ORIGINS</code> 里放行本页来源）。' +
        '</p>' +
        '<p style="color:var(--c-text-dim)">' +
        '接口根地址：<code>' + esc(WQ.apiBase || '（同源）') + '</code><br>' +
        '当前状态：' + (WQ.state.online ? '已连接后端服务' : '后端服务不可达') +
        '</p></div>',
      actions: [{ label: '关闭', value: null, primary: true }]
    });
  };

  actions.rules = function () {
    WQ.data.rules().then(function (rules) {
      var keys = Object.keys(rules);
      if (!keys.length) return WQ.toast('没有取到阈值表', 'err');
      var rows = keys.map(function (k) {
        var r = rules[k];
        var range = r.isRange
          ? ('超标 &lt;' + r.lowAlarm + ' 或 &gt;' + r.highAlarm + '　预警 &lt;' + r.lowWarn + ' 或 &gt;' + r.highWarn)
          : (r.lowerIsWorse
              ? ('超标 ≤' + WQ.num(r.alarm) + '　预警 ≤' + WQ.num(r.warn))
              : ('预警 ≥' + WQ.num(r.warn) + '　超标 ≥' + WQ.num(r.alarm)));
        return '<tr><td>' + esc(r.label || k) + '</td><td>' + esc(r.unit || '') + '</td><td>' + range + '</td></tr>';
      }).join('');
      WQ.dialog({
        title: '判定阈值（GB 3838-2002 参考）', icon: 'i-threshold',
        html:
          '<p style="color:var(--c-text-dim);margin-bottom:10px">' +
          '阈值的唯一真源在后端 <code>rules.py</code>，本表由接口下发 —— 改一次后端，在线页与离线导出同时生效。</p>' +
          '<div class="wq-tablewrap" style="max-height:46vh"><table class="wq-table selectable">' +
          '<thead><tr><th>指标</th><th>单位</th><th>判定条件</th></tr></thead><tbody>' + rows + '</tbody>' +
          '</table></div>',
        actions: [{ label: '关闭', value: null, primary: true }]
      });
    }).catch(function (e) { WQ.toast('取阈值失败：' + e.message, 'err'); });
  };

  actions.theme = toggleTheme;

  /* ── 水质测量与水质评价方法说明 ──
   * 原来这里是「无底图」按钮，用途不明，负责人要求改成方法说明。
   * 内容依据项目负责人提供的官方参考标准整理，只写文档里明确给出的内容。
   *
   * 一个重要事实（原实现写错了）：官方明确把**总氮排除在地表水水质评价指标之外**
   * （表1 中除水温、总氮、粪大肠菌群以外的 21 项），且国控水站评价只用
   * pH、溶解氧、高锰酸盐指数、氨氮、总磷 5 项。本平台的「预警/超标」两级阈值
   * 是值班预警口径，**不等同于**水质类别评价，两者不能混为一谈。 */
  actions.method = function () {
    function tbl(head, rows) {
      return '<div class="wq-tablewrap"><table class="wq-table">' +
        '<thead><tr>' + head.map(function (h) { return '<th>' + h + '</th>'; }).join('') + '</tr></thead>' +
        '<tbody>' + rows.map(function (r) {
          return '<tr>' + r.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>';
        }).join('') + '</tbody></table></div>';
    }
    function swatch(hex, name) {
      return '<span style="display:inline-block;width:10px;height:10px;border-radius:2px;' +
        'background:' + hex + ';border:1px solid rgba(0,0,0,.35);' +
        'margin-right:6px;vertical-align:-1px"></span>' + name;
    }

    var limits = tbl(
      ['指标', 'Ⅰ类', 'Ⅱ类', 'Ⅲ类', 'Ⅳ类', 'Ⅴ类'],
      [
        ['pH（无量纲）', '6 ~ 9', '6 ~ 9', '6 ~ 9', '6 ~ 9', '6 ~ 9'],
        ['溶解氧 DO（mg/L）≥', '7.5<br><span style="opacity:.7">或饱和率 90%</span>', '6', '5', '3', '2'],
        ['高锰酸盐指数（mg/L）≤', '2', '4', '6', '10', '15'],
        ['氨氮 NH₃-N（mg/L）≤', '0.15', '0.5', '1.0', '1.5', '2.0'],
        ['总磷 TP（以 P 计，mg/L）≤',
          '0.02<br><span style="opacity:.7">湖库 0.01</span>',
          '0.1<br><span style="opacity:.7">湖库 0.025</span>',
          '0.2<br><span style="opacity:.7">湖库 0.05</span>',
          '0.3<br><span style="opacity:.7">湖库 0.1</span>',
          '0.4<br><span style="opacity:.7">湖库 0.2</span>']
      ]
    );

    var section = tbl(
      ['水质类别', '水质状况', '表征颜色', '水质功能类别'],
      [
        ['Ⅰ ~ Ⅱ 类', '优', swatch('#1e6fd9', '蓝色'),
          '饮用水源地一级保护区、珍稀水生生物栖息地、鱼虾类产卵场、仔稚幼鱼的索饵场等'],
        ['Ⅲ 类', '良好', swatch('#2da44e', '绿色'),
          '饮用水源地二级保护区、鱼虾类越冬场、洄游通道、水产养殖区、游泳区'],
        ['Ⅳ 类', '轻度污染', swatch('#d4a017', '黄色'),
          '一般工业用水和人体非直接接触的娱乐用水'],
        ['Ⅴ 类', '中度污染', swatch('#e07b00', '橙色'),
          '农业用水及一般景观用水'],
        ['劣 Ⅴ 类', '重度污染', swatch('#d93a2b', '红色'),
          '除调节局部气候外，使用功能较差']
      ]
    );

    var basin = tbl(
      ['水质类别比例', '水质状况', '表征颜色'],
      [
        ['Ⅰ ~ Ⅲ 类比例 ≥ 90%', '优', swatch('#1e6fd9', '蓝色')],
        ['75% ≤ Ⅰ ~ Ⅲ 类比例 &lt; 90%', '良好', swatch('#2da44e', '绿色')],
        ['Ⅰ ~ Ⅲ 类比例 &lt; 75%，且劣 Ⅴ 类比例 &lt; 20%', '轻度污染', swatch('#d4a017', '黄色')],
        ['Ⅰ ~ Ⅲ 类比例 &lt; 75%，且 20% ≤ 劣 Ⅴ 类比例 &lt; 40%', '中度污染', swatch('#e07b00', '橙色')],
        ['Ⅰ ~ Ⅲ 类比例 &lt; 60%，且劣 Ⅴ 类比例 ≥ 40%', '重度污染', swatch('#d93a2b', '红色')]
      ]
    );

    WQ.dialog({
      title: '水质测量与评价方法', icon: 'i-info', wide: true,
      html:
        '<div class="wq-doc">' +

        '<h4>一、数据来源与更新方式</h4>' +
        '<p>数据范围为<b>已建成并正式投入运行的国家地表水水质自动监测站（国控水站）</b>。' +
        '因点位调整、断流、供电、设备更新等因素，实际运行水站数量会变化，' +
        '具体数据量以实际运行水站数为准。</p>' +
        '<p>更新频次：<b>每 4 小时一次</b>；更新内容：<b>实时数据</b>。</p>' +

        '<h4>二、监测指标（9 项）</h4>' +
        '<p>水温、pH、溶解氧、电导率、浊度、高锰酸盐指数、氨氮、总磷、总氮，共 <b>9 项</b>；' +
        '湖库水站另增加 <b>叶绿素 a</b> 与 <b>藻密度</b>。</p>' +

        '<h4>三、水质评价指标范围</h4>' +
        '<p><b>地表水水质评价指标</b>：《地表水环境质量标准》（GB 3838-2002）表 1 中' +
        '<b>除水温、总氮、粪大肠菌群以外的 21 项</b>指标。</p>' +
        '<p><b>国控水站水质评价指标</b>：pH、溶解氧、高锰酸盐指数、氨氮、总磷，共 <b>5 项</b>。</p>' +
        '<p class="wq-doc__note">注意：总氮属于监测指标，但<b>不参与地表水水质类别评价</b>。' +
        '本平台以总氮（TN）作为预测对象，属于水质预测与预警范畴，' +
        '与「按 GB 3838-2002 评定水质类别」是两件事，不可混用。</p>' +

        '<h4>四、评价方法</h4>' +
        '<p>采用<b>单因子评价法</b>：逐项对照上表限值判定所属类别，' +
        '<b>取其中最差的类别</b>作为该断面的水质类别，不做平均。</p>' +

        '<h4>五、GB 3838-2002 表 1 主要限值（国控水站评价 5 项）</h4>' +
        limits +

        '<h4>六、断面水质定性评价</h4>' +
        section +

        '<h4>七、河流、流域（水系）水质定性评价分级</h4>' +
        basin +

        '<h4>八、与本平台阈值的关系</h4>' +
        '<p>本平台另有一套<b>预警 / 超标</b>两级阈值（唯一真源 <code>rules.py</code>，由接口下发），' +
        '用于值班期间的快速预警，<b>不是</b>水质类别评价。两者口径不同：' +
        '类别评价按 GB 3838-2002 限值取最差项，预警阈值则是按指标设定的运营判据。' +
        '具体数值请点功能区「判定依据」查看。</p>' +

        '<p class="wq-doc__src">依据：《地表水环境质量标准》（GB 3838-2002）表 1；' +
        '国控水站数据与评价指标说明（项目负责人提供）。</p>' +

        '</div>',
      actions: [{ label: '关闭', value: null, primary: true }]
    });
  };
  actions['toggle-side'] = toggleSide;

  /* ─────────────── 初始化 ─────────────── */
  function init() {
    $('#appTitle').textContent = WQ.cfg.appTitle;
    $('#appSub').textContent = WQ.cfg.appSubtitle;

    /* 主题：界面偏好，允许读本地存储（业务数据不落地） */
    var saved = null;
    try { saved = localStorage.getItem('wq.theme'); } catch (e) { /* 忽略 */ }
    applyTheme(saved || WQ.cfg.theme);

    /* 全局事件委托：功能区的按钮、视图按钮、选项卡、目录里的断面 */
    document.addEventListener('click', function (e) {
      var st = e.target.closest('#stationGroups [data-station]');
      if (st) { select(st.dataset.station); return; }

      var a = e.target.closest('[data-action]');
      if (a && !a.disabled) {
        var fn = actions[a.dataset.action];
        if (fn) { e.preventDefault(); fn(a); return; }
      }
      var v = e.target.closest('[data-view]');
      if (v) { e.preventDefault(); setView(v.dataset.view); return; }
      var t = e.target.closest('#ribbonTabs [data-tab]');
      if (t) { e.preventDefault(); setTab(t.dataset.tab); return; }
    });

    $('#btnTheme').addEventListener('click', toggleTheme);
    $('#btnPanel').addEventListener('click', toggleSide);
    $('#btnHelp').addEventListener('click', actions.about);

    $('#stationSearch').addEventListener('input', function (e) {
      WQ.state.query = e.target.value;
      renderStations();
    });

    actions.filter = function (btn) {
      WQ.state.filter = btn.dataset.filter;
      $$('[data-action="filter"]').forEach(function (b) {
        b.setAttribute('aria-pressed', String(b.dataset.filter === WQ.state.filter));
      });
      renderStations();
    };

    renderLegend();
    setTab('map');

    /* ── 窄屏适配：只在窄屏默认收起内容目录；**宽屏默认展开** ──
     * 关键：自动调整只在「跨过阈值」时发生一次，不会覆盖用户随后手动开合的选择，
     * 也绝不会在大屏上默认收起。 */
    var NARROW = 900;
    var wasNarrow = window.innerWidth < NARROW;
    toggleSide(!wasNarrow);          // 窄屏收起、宽屏展开（并同步按钮状态）

    /* 界面缩放时通知地图/图表重算尺寸 */
    window.addEventListener('resize', function () {
      var narrow = window.innerWidth < NARROW;
      if (narrow !== wasNarrow) {
        wasNarrow = narrow;
        toggleSide(!narrow);         // 变窄→收起；变宽→展开
      }
      WQ.bus.emit('resize');
    });
  }

  WQ.actions = actions;
  WQ.shell = {
    init: init, setView: setView, setTab: setTab, select: select,
    renderStations: renderStations, visibleStations: visibleStations,
    setStatus: setStatus, refreshStatusbar: refreshStatusbar, setCoords: setCoords,
    applyTheme: applyTheme, toggleSide: toggleSide, registerView: function (n, v) { views[n] = v; }
  };
})(window.WQ);
