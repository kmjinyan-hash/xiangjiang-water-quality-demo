/* ══════════════════════════════════════════════════════════════════════════
 * views.js — 四个视图：地图、属性表、图表、预警报告
 *
 * 每个视图向 WQ.shell 注册 { show, hide, onSelect, onTheme, onResize }，
 * 由外壳在切换视图 / 选中断面 / 换主题 / 改窗口大小时回调。
 * 所有数据都经 WQ.data 获取 —— 在线走 /api/*，离线自动回退内置快照，
 * 因此这套界面在「服务端托管」和「单文件离线导出」两种投放下一模一样。
 * ══════════════════════════════════════════════════════════════════════════ */

(function (WQ) {
  'use strict';

  var $ = WQ.$, $$ = WQ.$$, esc = WQ.esc, icon = WQ.icon, num = WQ.num;
  var FEATURES = ['TEM', 'PH', 'DO', 'TDS', 'NTU', 'CODMN', 'TP', 'TN'];
  var FEATURE_LABEL = {
    TEM: '水温(℃)', PH: 'pH', DO: '溶解氧(mg/L)', TDS: '电导率(μS/cm)',
    NTU: '浊度(NTU)', CODMN: '高锰酸盐(mg/L)', TP: '总磷(mg/L)', TN: '总氮(mg/L)'
  };
  var LEVEL_CLS = { normal: 'lv-ok', warning: 'lv-warn', alarm: 'lv-alarm', unknown: 'lv-unknown' };

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function levelOfValue(key, v) {
    var r = (WQ.state.rules || {})[key];
    if (!r) return 'unknown';
    /* 等级判定只走 WQ.grade —— 全站唯一口径。
     * 这里只做一次键名翻译：WQ.grade 用 warn，界面样式表用 warning。
     * 以前这里自己抄了一份阈值比较，两份口径迟早会走偏。 */
    var lv = WQ.grade.levelOf(key, v, r);
    return lv === 'warn' ? 'warning' : lv;
  }

  /* ══════════════════════════════════════════════════════════════════════
   * 地图视图
   * ══════════════════════════════════════════════════════════════════════ */
  var mapView = (function () {
    var map = null, layer = null, baseLayer = null, measure = null;
    var markers = {};

    function basemapLayer(name) {
      var cfg = WQ.cfg.basemaps[name];
      if (!cfg || !cfg.url) return null;
      return L.tileLayer(cfg.url, {
        subdomains: cfg.subdomains || 'abc',
        maxZoom: cfg.maxZoom || 18,
        attribution: cfg.attribution || '',
        /* 导出地图 PNG 要把瓦片画进 canvas。图片**加载时**没带 crossOrigin，
         * 即使服务端发了 CORS 头，浏览也会把画布标成"被污染"，
         * toBlob 直接抛 SecurityError。这里显式走匿名模式。
         * 该底图（高德）实测返回 Access-Control-Allow-Origin: *，可以取。 */
        crossOrigin: 'anonymous',
        /* 与土地利用图层一致：拖动过程中不请求新瓦片，视野外多留几圈。
         * 底图瓦片在土地利用打开时其实被完全遮住，减少它的请求同样能提速。 */
        updateWhenIdle: true,
        keepBuffer: 3
      });
    }

    function setBasemap(name) {
      if (!map) return;
      if (baseLayer) { map.removeLayer(baseLayer); baseLayer = null; }
      WQ.state.basemap = name;
      if (name !== 'none') {
        baseLayer = basemapLayer(name);
        if (baseLayer) {
          baseLayer.on('tileerror', function () { /* 底图取不到时保持纯色底，界面照常可用 */ });
          baseLayer.addTo(map);
          baseLayer.bringToBack();
        }
      }
      $$('[data-action="basemap"]').forEach(function (b) {
        b.setAttribute('aria-pressed', String(b.dataset.basemap === name));
      });
    }

    /* 站点颜色按预警等级取；无等级时预测断面用绿、上游断面用灰蓝。
     *
     * 优先级：时间轴回放帧 > 该断面最近一份报告的 overall > 断面角色。
     * 回放时圆点颜色必须跟着帧走，否则「播放」看不出任何变化。 */
    var LV_VAR = {
      normal: '--c-ok', warning: '--c-warn', warn: '--c-warn',
      alarm: '--c-alarm', unknown: '--c-unknown'
    };

    function pinLevel(s) {
      var fv = (WQ.state.frameValues || {})[s.id];
      if (fv && fv.lv) { return fv.lv === 'warn' ? 'warning' : fv.lv; }
      return (WQ.state.reportLevel || {})[s.code] || null;
    }

    function levelColor(s) {
      var lv = pinLevel(s);
      if (lv) { return cssVar(LV_VAR[lv] || '--c-unknown'); }
      return cssVar(s.role === 'target' ? '--c-ok' : '--c-unknown');
    }

    /* 水滴形定位针。
     * 原来用圆点，负责人反馈「不够突出」。改成定位针：针尖精确指向坐标点，
     * 视觉上明确是「监测点位」，比圆点更容易在地图上一眼找到。
     * 内联 SVG + divIcon，任何缩放都清晰；白色描边保证深浅底图上都可见。 */
    function pinSvg(s, color) {
      return '<svg viewBox="0 0 28 40" width="100%" height="100%" preserveAspectRatio="xMidYMid meet">' +
        '<path d="M14 1.6C7.6 1.6 2.4 6.8 2.4 13.2c0 8.6 11.6 24.4 11.6 24.4s11.6-15.8 11.6-24.4' +
        'C25.6 6.8 20.4 1.6 14 1.6z" fill="' + color + '"' +
        ' stroke="rgba(255,255,255,.92)" stroke-width="1.8" stroke-linejoin="round"/>' +
        '<circle cx="14" cy="13.2" r="4.4" fill="rgba(255,255,255,.95)"/>' +
        '</svg>';
    }

    function pinIcon(s) {
      var target = s.role === 'target';
      var lv = pinLevel(s);
      var w = target ? 30 : 24;
      var h = Math.round(w * 40 / 28);
      return L.divIcon({
        html: pinSvg(s, levelColor(s)),
        className: 'wq-pin' +
                   (target ? ' wq-pin--target' : '') +
                   (lv === 'alarm' ? ' wq-pin--alarm' : ''),
        iconSize: [w, h],
        iconAnchor: [w / 2, h]          // 针尖对准坐标点
      });
    }

    /* 命中区域与视觉大小分离：视觉是一个针形图标，另铺半径 16px 的透明圆
     * 承担全部鼠标交互。点选轻松，又不必把图形做大。 */
    var HIT_RADIUS = 16;

    function buildMarkers() {
      if (layer) { map.removeLayer(layer); }
      layer = L.layerGroup();
      markers = {};
      WQ.state.stations.forEach(function (s) {
        var vis = L.marker([s.gcj.lat, s.gcj.lng], {
          icon: pinIcon(s),
          /* 必须可交互：否则针身上半截（落在站点坐标 16px 命中圆之外）点不动。
           * keyboard:false 关掉键盘焦点 —— 它正是"点击后出现黑框"的来源之一。 */
          interactive: true,
          keyboard: false,
          zIndexOffset: s.role === 'target' ? 200 : 0
        });
        layer.addLayer(vis);

        var hit = L.circleMarker([s.gcj.lat, s.gcj.lng], {
          radius: HIT_RADIUS,
          stroke: false,
          fill: true,
          fillColor: '#000',
          fillOpacity: 0.001,           // 必须有非零不透明度才可点
          interactive: true,
          className: 'wq-hit'
        });
        hit.bindPopup(popupHtml(s), { minWidth: 230 });
        hit.bindTooltip(s.name, { direction: 'top', offset: [0, -(HIT_RADIUS + 24)], opacity: 0.92 });
        hit.on('click', function () { WQ.shell.select(s.id, { from: 'map' }); });

        /* 针身本身也要能点。
         * 原来视觉层是 interactive:false，交互只靠站点坐标处那个半径 16px 的透明圆；
         * 而水滴向上伸出 40 多像素，上半截落在圆外 —— 于是"点针头没反应"。
         * 这里把针设为可交互，点击/悬停都转到同一套逻辑（弹窗仍只挂在 hit 上，
         * 避免出现两个弹窗实例）。 */
        var hoverOn = function () {
          var el = vis.getElement();
          if (el) { el.classList.add('wq-pin--hover'); }
        };
        var hoverOff = function () {
          var el = vis.getElement();
          if (el) { el.classList.remove('wq-pin--hover'); }
        };
        vis.on('click', function () {
          hit.openPopup();
          WQ.shell.select(s.id, { from: 'map' });
        });
        vis.on('mouseover', hoverOn);
        vis.on('mouseout', hoverOff);
        hit.on('mouseover', hoverOn);
        hit.on('mouseout', hoverOff);

        markers[s.id] = { vis: vis, hit: hit };
        layer.addLayer(hit);
      });
      layer.addTo(map);
      /* 给针形图标打上站点编号：便于自动化核对截图（裁到站点周围）
       * 与排查"某根针是哪个站的"问题，不参与业务逻辑。 */
      Object.keys(markers).forEach(function (sid) {
        var el = markers[sid].vis.getElement();
        if (el) { el.setAttribute('data-sid', sid); }
      });
      /* 首次建标记时加载"默认开启"的地理图层（流域边界等） */
      ensureDefaultGeoLayers();
    }

    /* ══ 地理图层（流域边界 / 土地利用 / 以后新增的）═══════════════
     * 清单来自 /api/geo_layers，载荷按需加载 —— 土地利用那张 PNG 有 1.5 MB，
     * 不能塞进首屏。新增图层只要后端 data/geo 放好文件，这里不用改代码。 */
    var geoDefs = null;
    var geoState = {};                    // key -> { layer, def }
    var ASSET_V = (function () {
      var m = document.querySelector('meta[name="wq-asset-v"]');
      var v = m && m.getAttribute('content');
      return v ? '?v=' + v : '';
    })();

    function ensureGeoDefs() {
      if (geoDefs) { return Promise.resolve(geoDefs); }
      return WQ.data.geoLayers().then(function (list) {
        geoDefs = list || [];
        return geoDefs;
      }).catch(function () { geoDefs = []; return geoDefs; });
    }

    function syncGeoBtn(key, on) {
      $$('[data-action="geo-layer"]').forEach(function (b) {
        if (b.dataset.layer === key) {
          b.setAttribute('aria-pressed', String(!!on));
        }
      });
    }

    /* ── 土地利用类型图例 ──
     * 类别名称尚未拿到官方对照表，所以图例给的是**编码 + 配色 + 面积占比**，
     * 并在底部注明"名称待补"。数据来自后端 landuse.json 的 classes 字段，
     * 一旦补上名称，这里会自动显示名称而不用改代码。 */
    function renderLanduseLegend(def) {
      var host = document.getElementById('landuseLegend');
      var rows = document.getElementById('landuseLegendRows');
      if (!host || !rows) { return; }
      var cls = (def && def.classes) || [];
      if (!cls.length) { host.hidden = true; return; }
      var title = document.getElementById('landuseLegendTitle');
      if (title) { title.textContent = def.name || '土地利用'; }
      rows.innerHTML = cls.map(function (c) {
        var nm = c.name || '';
        var code = String(c.code);
        var pct = (c.share == null) ? ''
                : (c.share >= 1 ? c.share.toFixed(1) : c.share.toFixed(2)) + '%';
        var label = nm ? (nm + ' ' + code) : ('编码 ' + code);
        return '<div class="wq-lu__row" title="' + esc(label) + '　' + pct +
               '　' + (c.area_km2 == null ? '' : c.area_km2 + ' km²') + '">' +
               '<i class="wq-lu__sw" style="background:' + esc(c.color) + '"></i>' +
               '<span class="wq-lu__name">' + esc(label) + '</span>' +
               '<span class="wq-lu__pct">' + esc(pct) + '</span></div>';
      }).join('');
      var note = document.getElementById('landuseLegendNote');
      if (note) {
        note.hidden = !def.name_pending;
        note.textContent = def.name_pending
          ? '类型为 L1 级分组；细分亚类与官方对照表待补'
          : '';
      }
      host.hidden = false;
    }

    function hideLanduseLegend() {
      var host = document.getElementById('landuseLegend');
      if (host) { host.hidden = true; }
    }

    function buildGeoLayer(key, payload) {
      if (payload.type === 'composite') {
        /* 土地利用：只叠瓦片影像（河流边界已按要求撤除）。
         * 不透明度 1 —— 完全盖住下方底图，地类配色不被干扰。
         * 瓦片放在 tilePane（底图所在层），流域边界与站点标记在更上层，层级天然正确。 */
        var grp = L.layerGroup();
        var t = payload.tiles;
        L.tileLayer(t.url + ASSET_V, {
          minZoom: 0,
          maxZoom: 18,
          maxNativeZoom: t.maxNativeZoom || 12,   // 超过原生层级由 Leaflet 放大
          tileSize: t.tileSize || 256,
          opacity: 1,
          noWrap: true,
          /* 拖动流畅度的三个关键项：
           *  · updateWhenIdle —— 拖动**过程中不请求新瓦片**，松手后才加载。
           *    默认是 false（边拖边请求），走内网穿透时会把通道占满，表现为卡顿。
           *  · keepBuffer —— 视野外多保留几圈瓦片，小幅拖动就无需任何新请求。
           *  · detectRetina 关闭 —— 避免在高分屏请求 2 倍尺寸瓦片（我们只有一套）。 */
          updateWhenIdle: true,
          keepBuffer: 4,
          detectRetina: false
        }).addTo(grp);
        return grp;
      }
      /* 矢量面：**不填充**，只画边界。
       * 「黑底白边虚线」用两层叠加实现：先一条较粗的白线作外描边（casing），
       * 再叠一条黑虚线 —— 这样在深浅底图上都清晰，也不会用填充色糊住底图。
       * 原文件的面填充色按负责人要求整层去掉。
       *
       * 用 Canvas 渲染而不是 SVG：流域边界有 5000+ 顶点、且要画两遍，
       * SVG 在每次缩放时都要重建几千个 DOM 路径，拖动/缩放会明显发涩。
       * 两层共用同一个 canvas renderer，只产生一个 canvas 元素。 */
      var group = L.layerGroup();
      var cvs = L.canvas({ padding: 0.4 });
      L.geoJSON(payload.geojson, {
        interactive: false, renderer: cvs,
        style: { color: 'rgba(255,255,255,.95)', weight: 6, opacity: 1,
                 fill: false, fillOpacity: 0 }
      }).addTo(group);
      L.geoJSON(payload.geojson, {
        interactive: false, renderer: cvs,
        style: { color: '#000', weight: 2.4, opacity: 1,
                 dashArray: '9 7', fill: false, fillOpacity: 0 }
      }).addTo(group);
      return group;
    }

    function toggleGeoLayer(key) {
      ensureGeoDefs().then(function (defs) {
        var def = defs.filter(function (d) { return d.key === key; })[0];
        if (!def) {
          WQ.toast('未找到图层「' + key + '」：请确认 data/geo 下有对应文件', 'err');
          return;
        }
        var st = geoState[key];
        if (st && st.layer) {
          if (map.hasLayer(st.layer)) {
            map.removeLayer(st.layer);
            syncGeoBtn(key, false);
            if (key === 'landuse') { hideLanduseLegend(); }
          } else {
            st.layer.addTo(map);
            if (st.layer.bringToBack) { st.layer.bringToBack(); }
            syncGeoBtn(key, true);
            if (key === 'landuse') { renderLanduseLegend(st.def); }
          }
          return;
        }
        WQ.data.geoLayer(key).then(function (payload) {
          var lyr = buildGeoLayer(key, payload);
          geoState[key] = { layer: lyr, def: def, payload: payload };
          lyr.addTo(map);
          /* 影像类图层压在矢量边界之下，边界线始终可见 */
          if (lyr.bringToBack) { lyr.bringToBack(); }
          syncGeoBtn(key, true);
          /* 土地利用打开时同步显示类型图例（关闭时一并收起） */
          if (key === 'landuse') { renderLanduseLegend(def); }
          if (payload.name_pending) {
            WQ.toast(def.name + ' 已加载：类别名称待官方对照表，图例暂给编码与占比', 'ok');
          }
        }).catch(function (e) {
          WQ.toast('图层加载失败：' + e.message, 'err');
        });
      });
    }

    /* 默认开启的图层（目前只有流域边界），首次进地图时自动加载 */
    function ensureDefaultGeoLayers() {
      ensureGeoDefs().then(function (defs) {
        defs.forEach(function (d) { if (d.default) { toggleGeoLayer(d.key); } });
      });
    }

    function popupHtml(s) {
      var st = WQ.state.status[s.code] || {};
      var pred = WQ.state.predictions[s.code];
      var rows = [
        ['编号', s.code],
        ['河流 / 城市', (s.river || '--') + ' / ' + (s.city || '--')],
        ['角色', s.role === 'target' ? '预测断面（模型输出）' : '上游输入断面'],
        ['最新数据', st.latest_date ? (st.latest_date + '（' + (st.days_old == null ? '?' : st.days_old) + ' 天前）') : '--']
      ];
      /* 预测断面的预测值**始终占一行**：没有值时写"暂无"，而不是整行消失 ——
       * 整行消失会让人以为"这个功能不存在"（用户报的第一个问题）。 */
      if (s.role === 'target') {
        rows.push(['预测 TN（次日）', pred ? (num(pred.predicted_TN) + ' mg/L') : '暂无（点「运行预测」）']);
        rows.push(['实测 TN', pred ? (num(pred.actual_TN) + ' mg/L') : '暂无']);
        if (pred && pred.predict_date) rows.push(['预测日期', String(pred.predict_date).slice(0, 10)]);
      }
      rows.push(['坐标(WGS-84)', num(s.lat, 5) + ', ' + num(s.lon, 5)]);
      return '<div class="wq-popup">' +
        '<div class="wq-popup__title">' + icon('i-station') + esc(s.name) + '</div>' +
        photoFrame(s, false) +
        '<dl class="wq-popup__grid">' +
        rows.map(function (r) { return '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>'; }).join('') +
        '</dl></div>';
    }

    /* 现场照片框。
     * 负责人说明实地照片稍后再拍，先留空框；照片就绪后放到
     * frontend/assets/photos/ 并在 stations.js 里加 photo 字段即可自动显示，
     * 不用改这里的代码。 */
    function photoFrame(s, big) {
      var h = big ? 200 : 108;
      if (s.photo) {
        return '<div class="wq-photo" style="height:' + h + 'px">' +
          '<img src="' + esc(s.photo) + '" alt="' + esc(s.name) + '"></div>';
      }
      return '<div class="wq-photo wq-photo--empty" style="height:' + h + 'px">' +
        icon('i-camera') +
        '<div class="wq-photo__hint">实地照片待采集</div></div>';
    }

    /* ── 一句话解读（短评）──
     * 与「AI 生成预警报告」同一条 SSE 流水线，只是 style='brief'：
     * 提示词换成 2-4 句、max_tokens 260、**不落库**（不占报告列表）。
     * 位置按方案放在断面详情卡「指标卡」下方，点一下才生成 —— 打开详情就自动
     * 调模型会让每次点断面都产生一次费用，这个代价不该由用户无意承担。 */
    var briefBusy = {};

    function streamBrief(s, bodyEl, btnEl) {
      if (!bodyEl || briefBusy[s.id]) { return; }
      briefBusy[s.id] = true;
      if (btnEl) { btnEl.disabled = true; btnEl.textContent = '生成中…'; }
      bodyEl.textContent = '';
      bodyEl.classList.add('wq-stream--live');

      var text = '';

      WQ.stream('/api/ai_report/generate', { station: s.id, style: 'brief' }).then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (j) {
            throw new Error(j.message || ('HTTP ' + resp.status));
          });
        }
        if (!resp.body || !resp.body.getReader) { throw new Error('当前浏览器不支持流式响应'); }
        var reader = resp.body.getReader();
        var dec = new TextDecoder('utf-8');
        var buf = '';

        function handle(block) {
          var ev = 'message', data = [];
          block.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) { ev = ln.slice(6).trim(); }
            else if (ln.indexOf('data:') === 0) { data.push(ln.slice(5).trim()); }
          });
          if (!data.length) { return; }
          var obj;
          try { obj = JSON.parse(data.join('\n')); } catch (e) { return; }
          if (ev === 'delta') {
            text += (obj.t || '');
            bodyEl.textContent = text;
          } else if (ev === 'done') {
            var r = obj.report || {};
            bodyEl.textContent = String(r.summary || text || '').trim() || '（模型未返回内容）';
          }
        }

        function pump() {
          return reader.read().then(function (res) {
            if (res.done) { return; }
            buf += dec.decode(res.value, { stream: true });
            var i;
            while ((i = buf.indexOf('\n\n')) >= 0) {
              handle(buf.slice(0, i));
              buf = buf.slice(i + 2);
            }
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        bodyEl.textContent = '解读失败：' + e.message;
      }).then(function () {
        briefBusy[s.id] = false;
        bodyEl.classList.remove('wq-stream--live');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = '重新解读'; }
      });
    }

    /* 站点现场信息面板：由「定位断面」或弹窗里的按钮打开 */
    function stationDetail(sid) {
      var s = WQ.state.stations.filter(function (x) { return x.id === sid; })[0];
      if (!s) { return; }
      var st = WQ.state.status[s.code] || {};
      var pred = WQ.state.predictions[s.code];
      var rows = [
        ['断面编号', s.code],
        ['所在河流', s.river || '--'],
        ['所在城市', s.city || '--'],
        ['站点角色', s.role === 'target' ? '预测断面（模型输出）' : '上游输入断面'],
        ['坐标(WGS-84)', num(s.lat, 5) + ', ' + num(s.lon, 5)],
        ['坐标(GCJ-02)', num(s.gcj.lat, 5) + ', ' + num(s.gcj.lng, 5)],
        ['最新数据', st.latest_date ? (st.latest_date + '（' + (st.days_old == null ? '?' : st.days_old) + ' 天前）') : '--']
      ];
      if (s.role === 'target' && pred) {
        rows.push(['预测 TN（次日）', num(pred.predicted_TN) + ' mg/L']);
      }
      WQ.dialog({
        title: s.name + ' · 现场信息', icon: 'i-station',
        html: photoFrame(s, true) +
          '<dl class="wq-popup__grid" style="margin-top:10px">' +
          rows.map(function (r) { return '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>'; }).join('') +
          '</dl>' +
          '<div class="wq-brief">' +
            '<div class="wq-brief__h">' + icon('i-spark') +
              '<span>AI 一句话解读</span>' +
              '<span class="wq-panel__spacer"></span>' +
              '<button class="wq-btn wq-brief__btn" id="wqBriefBtn">生成</button>' +
            '</div>' +
            '<div class="wq-brief__b" id="wqBriefBody">' +
              '用 2-4 句说明「这个断面为什么是这个等级」，引用具体数值；' +
              '由在线模型即时生成，不保存到报告列表。' +
            '</div>' +
          '</div>' +
          '<p style="margin-top:10px;color:var(--c-text-faint);font-size:var(--fs-xs)">' +
          '实地照片与周边环境描述待现场采集后补充。</p>',
        actions: [{ label: '关闭', value: null, primary: true }],
        onOpen: function (wrap) {
          var btn = wrap.querySelector('#wqBriefBtn');
          var body = wrap.querySelector('#wqBriefBody');
          if (btn && body) {
            btn.addEventListener('click', function () { streamBrief(s, body, btn); });
          }
        }
      });
    }

    function restyle() {
      /* 站点是 divIcon，改颜色要换图标而不是 setStyle */
      WQ.state.stations.forEach(function (s) { if (markers[s.id]) markers[s.id].vis.setIcon(pinIcon(s)); });
    }

    function refreshPopups() {
      WQ.state.stations.forEach(function (s) {
        if (markers[s.id]) markers[s.id].hit.setPopupContent(popupHtml(s));
      });
    }

    function fitAll() {
      if (!map || !WQ.state.stations.length) return;
      var b = L.latLngBounds(WQ.state.stations.map(function (s) { return [s.gcj.lat, s.gcj.lng]; }));
      map.fitBounds(b, { padding: [48, 48] });
    }

    function flyTo(sid) {
      var s = WQ.state.stations.filter(function (x) { return x.id === sid; })[0];
      if (!s || !map) return;
      map.setView([s.gcj.lat, s.gcj.lng], Math.max(map.getZoom(), 11), { animate: true });
      if (markers[sid]) markers[sid].hit.openPopup();
    }

    /* ═══════════ 距离量算 ═══════════
     * 交互：点一下定**起点**（立即出现一个可见顶点）→ 移动鼠标**实时预览**
     * 一条橡皮筋线并在状态栏显示当前距离 → 再点一下定终点（补一个顶点 + 距离标签）。
     * Esc 退出。旧实现是「点两个点才画线、中间零反馈」，
     * 用户根本不知道自己有没有选中起点 —— 这才是它"极其难用"的原因。 */
    var mst = null;   // measure state

    function distText(a, b) {
      var d = map.distance(a, b);
      return d >= 1000 ? (d / 1000).toFixed(3) + ' km' : d.toFixed(1) + ' m';
    }

    function vertexDot(latlng) {
      return L.circleMarker(latlng, {
        radius: 4, color: cssVar('--c-accent'), weight: 2,
        fillColor: cssVar('--c-accent'), fillOpacity: 1, interactive: false
      }).addTo(map);
    }

    function distanceLabel(latlng, text) {
      return L.marker(latlng, {
        interactive: false,
        icon: L.divIcon({
          className: 'wq-measure-label',
          html: '<span>' + esc(text) + '</span>',
          iconSize: [1, 1]
        })
      }).addTo(map);
    }

    function stopMeasure(silent) {
      if (!mst) return;
      map.off('click', mst.onClick);
      map.off('mousemove', mst.onMove);
      document.removeEventListener('keydown', mst.onKey);
      map.getContainer().classList.remove('wq-measuring');
      mst.layers.forEach(function (l) { map.removeLayer(l); });
      mst = null;
      if (!silent) WQ.shell.setStatus('已退出量算');
    }

    function startMeasure() {
      stopMeasure(true);
      var st = { start: null, rubber: null, layers: [], onClick: null, onMove: null, onKey: null };

      st.onClick = function (e) {
        if (!st.start) {
          st.start = e.latlng;
          st.layers.push(vertexDot(e.latlng));
          WQ.shell.setStatus('量算：移动鼠标预览距离，点击确定终点（Esc 取消）');
          return;
        }
        /* 定终点 */
        var end = e.latlng;
        var text = distText(st.start, end);
        if (st.rubber) { map.removeLayer(st.rubber); st.layers = st.layers.filter(function (l) { return l !== st.rubber; }); st.rubber = null; }
        st.layers.push(L.polyline([st.start, end], {
          color: cssVar('--c-accent'), weight: 2, dashArray: '7 5'
        }).addTo(map));
        st.layers.push(vertexDot(end));
        st.layers.push(distanceLabel(
          L.latLng((st.start.lat + end.lat) / 2, (st.start.lng + end.lng) / 2), text));
        st.start = null;
        WQ.shell.setStatus('量算结果 ' + text + '　｜　再点一下开始新的一次（Esc 退出）');
        WQ.toast('量算距离 ' + text, 'ok');
      };

      st.onMove = function (e) {
        if (!st.start) return;
        if (st.rubber) { map.removeLayer(st.rubber); st.layers = st.layers.filter(function (l) { return l !== st.rubber; }); }
        st.rubber = L.polyline([st.start, e.latlng], {
          color: cssVar('--c-accent'), weight: 1.5, dashArray: '4 4', opacity: 0.8
        }).addTo(map);
        st.layers.push(st.rubber);
        WQ.shell.setStatus('量算：' + distText(st.start, e.latlng) + '（点击确定终点，Esc 取消）');
      };

      st.onKey = function (ev) { if (ev.key === 'Escape') { stopMeasure(); } };

      mst = st;
      map.getContainer().classList.add('wq-measuring');
      map.on('click', st.onClick);
      map.on('mousemove', st.onMove);
      document.addEventListener('keydown', st.onKey);
      WQ.shell.setStatus('量算：点击地图确定起点（Esc 取消）');
    }

    function toggleMeasure() {
      if (mst) { stopMeasure(); return; }
      startMeasure();
    }

    return {
      show: function () {
        if (!map) {
          map = L.map('map', {
            /* 刻意**不给 center/zoom**：那会是硬编码的流域坐标，属于业务数据。
             * 但 Leaflet 在没有任何视图时**拒绝添加图层**（抛 "Set map center and zoom first"），
             * 所以建图后必须先定位一次，再挂底图与图层：
             *   有断面 -> 用后端返回的断面范围 fitBounds（真实数据驱动）
             *   没断面 -> 退到一个中性的全球视图占位，随后拿到数据会覆盖
             */
            zoomControl: true,
            attributionControl: true,
            /* 只有 14 个断面，走 SVG 而不是 canvas：
             * 标记成为独立的 DOM 元素，便于单测断言、CSS 主题化与悬停交互；
             * canvas 那点性能优势在这个量级上完全用不到。 */
            preferCanvas: false
          });
          /* **建图后必须立刻给一个视图**：Leaflet 在没有 center/zoom 时
           * 添加任何图层都会抛 "Set map center and zoom first"。
           * 有断面就用后端给的断面范围定位；没有就退到中性全球视图占位
           * （这个占位不含任何项目数据，拿到断面后会被覆盖）。 */
          if (WQ.state.stations && WQ.state.stations.length) {
            map.fitBounds(
              L.latLngBounds(WQ.state.stations.map(function (s) { return [s.gcj.lat, s.gcj.lng]; })),
              { padding: [48, 48] });
          } else {
            map.setView([0, 0], 2);
          }
          /* 比例尺：GB/T 20257 要求「直线比例尺」+「数字比例尺」两者齐备，
           * 所以这里是**两个控件并存** —— 先 Leaflet 的图形条，再补 1:xxxxx 数值，
           * 不能用自定义控件替换掉自带的那根条。
           *
           * 数字比例尺换算：米/像素 = 156543.03392·cos(纬度)/2^zoom，
           * 再除以 0.00028 m/px（制图惯用的屏幕像素物理尺寸）得分母，
           * 最后吸附到制图标准的分母数列，避免出现 1:47321 这种非标准值。 */
          /* 比例尺：黑白交错的分段刻度条 + 数字比例尺，合成一个控件。
           * 不用 L.control.scale —— 它只能画"带文字的方框"，
           * 做不出常见地图那种黑白相间的标尺；合并成一个控件也少一次
           * DOM 创建与事件注册（本次一并做了精简）。 */
          var CartoScale = L.Control.extend({
            options: { position: 'bottomright' },
            onAdd: function (m) {
              this._map = m;
              this._el = L.DomUtil.create('div', 'wq-scale');
              m.on('zoomend moveend', this._update, this);
              this._update();
              return this._el;
            },
            _update: function () {
              var m = this._map;
              if (!m) return;
              /* 每像素米数：在地图中部横向取两点实测，比按 zoom 套公式更稳 */
              var y = m.getSize().y / 2;
              var mpp = m.distance(m.containerPointToLatLng([0, y]),
                                   m.containerPointToLatLng([1, y]));
              if (!(mpp > 0)) return;

              /* 在 1/2/5 × 10^n 里挑一个让条宽最接近 90px 的距离。
               * 只挑"不小于目标"的做法常会 overshoot 到快 2 倍（实测 186px），
               * 这里改成取最接近者。 */
              var target = mpp * 90;
              var pow = Math.pow(10, Math.floor(Math.log(target) / Math.LN10));
              var meters = pow, best = Infinity;
              var steps = [1, 2, 5];
              for (var d = -1; d <= 1; d++) {
                for (var i = 0; i < steps.length; i++) {
                  var v = steps[i] * pow * Math.pow(10, d);
                  var wpx = v / mpp;
                  if (Math.abs(wpx - 90) < best) { best = Math.abs(wpx - 90); meters = v; }
                }
              }
              var px = Math.round(meters / mpp);
              var seg = (px / 4).toFixed(2);
              var bars = '';
              for (var k = 0; k < 4; k++) {
                bars += '<i class="' + (k % 2 ? 'w' : 'b') + '" style="width:' + seg + 'px"></i>';
              }
              var label = meters >= 1000 ? (meters / 1000) + ' km' : meters + ' m';

              /* 数字比例尺：分母吸附到标准数列 */
              var raw = mpp / 0.00028;
              var nice = [500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000,
                          100000, 200000, 250000, 500000, 1000000, 2000000, 5000000];
              var pick = nice[0];
              for (var j = 0; j < nice.length; j++) {
                if (Math.abs(nice[j] - raw) < Math.abs(pick - raw)) pick = nice[j];
              }

              this._el.innerHTML =
                '<div class="wq-scale__bar" style="width:' + px + 'px">' + bars + '</div>' +
                '<div class="wq-scale__lbl" style="width:' + px + 'px">' +
                  '<span>0</span><span>' + label + '</span></div>' +
                '<div class="wq-scale__num">1:' + pick.toLocaleString('en-US') + '</div>';
            }
          });
          map.addControl(new CartoScale());
          setBasemap(WQ.state.basemap);
          buildMarkers();
          map.on('mousemove', function (e) {
            /* 高德底图上的 e.latlng 是 **GCJ-02**，不是 WGS-84。
             * 之前直接把 e.latlng 标成 "WGS-84" 输出 —— 标签是错的，
             * 照这个读数去填 stations.js（那里存 WGS-84）会整体偏约 600 米。
             * 这里反算出真正的 WGS-84 再显示，同时把 GCJ-02 一并给出。 */
            var w = window.GCJ.gcj2wgs(e.latlng.lng, e.latlng.lat);
            WQ.shell.setCoords('WGS-84 ' + w.lat.toFixed(5) + ', ' + w.lng.toFixed(5));
          });
          map.on('mouseout', function () { WQ.shell.setCoords('WGS-84'); });
        }
        setTimeout(function () { map.invalidateSize(); }, 60);
      },
      hide: function () { },
      onResize: function () { if (map) map.invalidateSize(); },
      onTheme: function () { restyle(); },
      onSelect: function (sid, opts) {
        if (opts && opts.from === 'map') return;
        if (WQ.state.view === 'map') flyTo(sid);
      },
      onData: function () { refreshPopups(); restyle(); },
      fitAll: fitAll, setBasemap: setBasemap, toggleMeasure: toggleMeasure,
      flyTo: flyTo,
      /* 站点列表变化（用户新增/删除站点）后重建标记 */
      refreshMarkers: buildMarkers,
      stationDetail: stationDetail,
      toggleGeoLayer: toggleGeoLayer,
      /* ── 给「导出地图 PNG」「时间轴回放」用的只读句柄 ──
       * 只暴露实例与重绘入口，导出/回放的逻辑不写进地图闭包，
       * 免得地图视图越长越难维护。 */
      restyle: restyle,
      mapObject: function () { return map; },
      pinColor: levelColor,
      pinLevel: pinLevel,
      basinGeo: function () {
        var st = geoState['basin'];
        return (st && st.payload && st.payload.geojson) || null;
      }
    };
  })();

  /* ══════════════════════════════════════════════════════════════════════
   * 属性表视图
   * ══════════════════════════════════════════════════════════════════════ */
  var tableView = (function () {
    var cache = {};              // sid -> rows
    var sortKey = 'DATE', sortDir = 'desc', filterText = '';
    /* 显示列用断面名（用户在目录里看到的就是名字），
     * 但搜索列要连编号一起带上 —— 有人按名字找，也有人按 ST13 这样的编号找。 */
    var SEARCH_COLS = ['_code', '_name', 'DATE'].concat(FEATURES);

    function load() {
      var list = WQ.shell.visibleStations();
      return Promise.all(list.map(function (s) {
        if (cache[s.id]) return Promise.resolve(cache[s.id]);
        return WQ.data.history(s.id).then(function (rows) {
          cache[s.id] = (rows || []).map(function (r) {
            return Object.assign({ _sid: s.id, _code: s.code, _name: s.name }, r);
          });
          return cache[s.id];
        }).catch(function () { return []; });
      })).then(function () {
        var all = [];
        list.forEach(function (s) { all = all.concat(cache[s.id] || []); });
        return all;
      });
    }

    /* 阈值说明文字：鼠标停在单元格上就能看到判定口径，不必去翻规则表。 */
    function thresholdText(r) {
      var u = r.unit ? ' ' + r.unit : '';
      if (r.isRange) {
        return '正常 ' + r.lowWarn + ' ~ ' + r.highWarn + u +
               '；预警 < ' + r.lowWarn + ' 或 > ' + r.highWarn +
               '；超标 ≤ ' + r.lowAlarm + ' 或 ≥ ' + r.highAlarm;
      }
      if (r.lowerIsWorse) {
        return '预警 ≤ ' + r.warn + u + '；超标 ≤ ' + r.alarm + u;
      }
      return '预警 ≥ ' + r.warn + u + '；超标 ≥ ' + r.alarm + u;
    }

    /* 指标单元格：主行 = 实测值，次行小字 = 占标率（实测 / 超标限值）。
     *
     * 占标率让"离超标还有多远"一眼可读 —— 单看 3.14 不知道算不算高，
     * 看 157% 就知道已经超了。pH 是区间型，没有单一比值，次行改显区间。
     * 等级仍由 WQ.grade 判定，颜色一律用色块（不只是文字变色）。 */
    function metricCell(c, v, rule) {
      var has = !(v === null || v === undefined || v === '' || isNaN(Number(v)));
      var lv = rule ? levelOfValue(c, v) : 'unknown';
      var digits = c === 'PH' ? 2 : (c === 'TP' ? 3 : 2);
      var main = has ? num(v, digits) : '--';
      var sub = '';

      if (has && rule) {
        var ratio = WQ.grade.ratioOf(c, v, rule);
        if (ratio != null) {
          var pct = ratio * 100;
          sub = '<span class="wq-cell__sub' + (lv === 'alarm' ? ' is-alarm' : '') +
                '" title="占标率 = 实测 / 超标限值">占标 ' +
                (pct >= 100 ? pct.toFixed(0) : pct.toFixed(1)) + '%</span>';
        } else if (rule.isRange) {
          sub = '<span class="wq-cell__sub">区间 ' + esc(rule.lowWarn + '~' + rule.highWarn) + '</span>';
        }
      }

      var cls = 'num' + (lv === 'unknown' ? '' : ' wq-lv--text ' + LEVEL_CLS[lv]);
      var tip = rule ? thresholdText(rule) : '';
      return '<td class="' + cls + '"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>' +
             '<span class="wq-cell__main">' + main + '</span>' + sub + '</td>';
    }

    function render(rows) {
      var head = $('#tableHead'), body = $('#tableBody'), empty = $('#tableEmpty');
      var cols = ['_name', 'DATE'].concat(FEATURES);
      var titles = { _name: '断面', DATE: '日期' };

      var filtered = rows.filter(function (r) {
        if (!filterText) return true;
        return SEARCH_COLS.some(function (c) { return String(r[c] == null ? '' : r[c]).toLowerCase().indexOf(filterText) >= 0; });
      });
      filtered.sort(function (a, b) {
        var x = a[sortKey], y = b[sortKey];
        if (x == null) return 1; if (y == null) return -1;
        if (typeof x === 'number' || sortKey === 'DATE') { if (x < y) return sortDir === 'asc' ? -1 : 1; if (x > y) return sortDir === 'asc' ? 1 : -1; return 0; }
        return String(x).localeCompare(String(y)) * (sortDir === 'asc' ? 1 : -1);
      });

      head.innerHTML = '<tr><th class="wq-table__rowno">#</th>' + cols.map(function (c) {
        return '<th data-col="' + c + '"' + (c === sortKey ? ' data-sort="' + sortDir + '"' : '') + '>' +
               esc(titles[c] || FEATURE_LABEL[c] || c) + '</th>';
      }).join('') + '</tr>';

      if (!filtered.length) {
        body.innerHTML = '';
        empty.hidden = false;
        $('#tableCount').textContent = '0 行';
        return;
      }
      empty.hidden = true;

      var LIMIT = 1200;      // 单次渲染上限，避免十万行时界面卡住
      var shown = filtered.slice(0, LIMIT);
      body.innerHTML = shown.map(function (r, i) {
        return '<tr data-station="' + esc(r._sid) + '"><td class="wq-table__rowno">' + (i + 1) + '</td>' +
          cols.map(function (c) {
            if (c === '_name') return '<td>' + esc(r._name || '') +
              '<span style="color:var(--c-text-faint);margin-left:8px;font-family:var(--font-mono)">' +
              esc(r._code || '') + '</span></td>';
            if (c === 'DATE') return '<td>' + esc(WQ.dateOnly(r[c])) + '</td>';
            return metricCell(c, r[c], (WQ.state.rules || {})[c]);
          }).join('') + '</tr>';
      }).join('');

      $('#tableCount').textContent = filtered.length + ' 行' +
        (filtered.length > shown.length ? '（显示前 ' + shown.length + '）' : '') +
        (filterText ? ' · 已筛选' : '');
      $('#tableTitle').textContent = '属性表 · ' + WQ.shell.visibleStations().length + ' 个断面';
    }

    function refresh() {
      cache = {};
      $('#tableBody').innerHTML = '<tr><td colspan="10" style="padding:16px;color:var(--c-text-dim)">正在读取数据…</td></tr>';
      return load().then(render).catch(function (e) { WQ.toast('属性表读取失败：' + e.message, 'err'); });
    }

    /* 表头排序与表内筛选：只绑一次。
     * 这里引用 tableView 是安全的 —— 处理函数要等模块装配完成后才会被触发。 */
    var bound = false;
    function initToolbar() {
      if (bound) return;
      bound = true;
      $('#tableHead').addEventListener('click', function (e) {
        var th = e.target.closest('th[data-col]');
        if (!th) return;
        var c = th.dataset.col;
        if (sortKey === c) { sortDir = sortDir === 'asc' ? 'desc' : 'asc'; } else { sortKey = c; sortDir = 'asc'; }
        tableView.show();
      });
      $('#tableSearch').addEventListener('input', function (e) {
        filterText = e.target.value.trim().toLowerCase();
        tableView.show();
      });
    }

    return {
      show: function () { initToolbar(); refresh(); },
      hide: function () { },
      onResize: function () { },
      onTheme: function () { },
      onSelect: function (sid) {
        if (WQ.state.view !== 'table') return;
        var tr = $('#tableBody tr[data-station="' + sid + '"]');
        if (tr) { tr.scrollIntoView({ block: 'center' }); }
      },
      onData: function () { if (WQ.state.view === 'table') refresh(); },
      refresh: refresh,
      currentRows: function () {
        var out = [];
        WQ.shell.visibleStations().forEach(function (s) { out = out.concat(cache[s.id] || []); });
        return out.filter(function (r) {
          if (!filterText) return true;
          return SEARCH_COLS.some(function (c) {
            return String(r[c] == null ? '' : r[c]).toLowerCase().indexOf(filterText) >= 0;
          });
        });
      }
    };
  })();
  /* ══════════════════════════════════════════════════════════════════════
   * 图表视图
   * ══════════════════════════════════════════════════════════════════════ */
  var chartView = (function () {
    var chart = null;
    var lastData = null;         // 最近一次真正画上去的序列（供导出 CSV 用）

    function metricOptions() {
      var opts = ['TN'].concat(FEATURES.filter(function (f) { return f !== 'TN'; }));
      return opts;
    }

    function initSelectors() {
      var ms = $('#chartMetric');
      if (!ms.options.length) {
        ms.innerHTML = metricOptions().map(function (k) {
          return '<option value="' + k + '">' + esc(FEATURE_LABEL[k] || k) + (k === 'TN' ? '（含预测）' : '') + '</option>';
        }).join('');
      }
      var ss = $('#chartStation');
      /* 断面下拉**只在首次填充**。原先每次进图表都重建 option，
       * 会把用户（或左侧目录）已经选好的断面重置回第一项 ——
       * 表现为「在地图上点了站点，切到图表还得再选一次」。 */
      if (!ss.options.length) {
        var targets = WQ.state.stations.filter(function (s) { return s.role === 'target'; });
        ss.innerHTML = targets.map(function (s) {
          return '<option value="' + s.id + '">' + esc(s.name) + '</option>';
        }).join('');
      }
      /* 与左侧目录 / 地图上的当前选中断面保持一致 */
      var sel = WQ.state.selected;
      if (sel) {
        var cur = WQ.state.stations.filter(function (x) { return x.id === sel; })[0];
        if (cur && cur.role === 'target') ss.value = cur.id;
      }
      if (!ss.value && ss.options.length) ss.value = ss.options[0].value;
    }

    /* 把区间吸附到 1/2/5 倍的整齐刻度上，轴端才不会出现浮点尾数 */
    function niceBounds(lo, hi) {
      var span = hi - lo;
      if (!(span > 0)) { span = Math.abs(hi) || 1; }
      var raw = span / 5;
      var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
      var norm = raw / mag;
      var step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
      var min = Math.floor(lo / step) * step;
      var max = Math.ceil(hi / step) * step;
      /* 非负指标不把轴拉到 0 以下（水质指标没有负值，留负区间是噪音） */
      if (lo >= 0 && min < 0) { min = 0; }
      var fix = function (v) { return Number(v.toFixed(10)); };
      return [fix(min), fix(max)];
    }

    /* ── 时间范围（③ 近 7 / 30 天 / 全部）──
     * 设计原则：**叠加、不取代**。滑块与滚轮缩放全部保留，
     * 日期框、快捷区间、滑块三者双向联动：
     *   点快捷区间 → 按**日期**换算成索引（不是按记录条数）
     *   改日期     → dispatchAction(dataZoom startValue/endValue)
     *   拖滑块     → chart.on('datazoom') 回写日期框
     *
     * 为什么按日期而不按条数：这批数据并非逐日连续，同样 "30 条" 可能横跨
     * 两个月，写成"近 30 天"就是假的。 */
    var catsRef = null;
    var curSpan = 0;                 // 0 = 全部；其余为天数

    function zoomToDays(days) {
      curSpan = days || 0;
      $$('#chartSpanSeg button').forEach(function (b) {
        b.setAttribute('aria-pressed', String(Number(b.dataset.days) === curSpan));
      });
      if (!chart || !catsRef || !catsRef.length) return;
      var n = catsRef.length;
      if (!curSpan) { chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 }); return; }
      var last = Date.parse(catsRef[n - 1] + 'T00:00:00');
      if (isNaN(last)) { chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 }); return; }
      var from = last - (curSpan - 1) * 86400000;
      var i = 0;
      for (var k = n - 1; k >= 0; k--) {
        var t = Date.parse(catsRef[k] + 'T00:00:00');
        if (!isNaN(t) && t >= from) { i = k; }
      }
      chart.dispatchAction({ type: 'dataZoom', startValue: i, endValue: n - 1 });
      syncRangeInputs((i / Math.max(1, n - 1)) * 100, 100);
    }

    /* 把主题色（#rrggbb 或 #rgb）转成 rgba，用于图表面积渐变与阴影。
     * echarts.graphic.LinearGradient 需要能解析的颜色串，CSS 变量给的是 hex。 */
    function withAlpha(c, a) {
      c = String(c || '').trim();
      if (c.charAt(0) === '#') {
        var h = c.slice(1);
        if (h.length === 3) { h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2]; }
        var n = parseInt(h, 16);
        if (!isNaN(n)) {
          return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
        }
      }
      if (/^rgba?\(/.test(c)) { return c; }
      return 'rgba(10,132,208,' + a + ')';
    }

    function syncRangeInputs(startPct, endPct) {
      var from = $('#chartFrom'), to = $('#chartTo');
      if (!from || !to || !catsRef || !catsRef.length) return;
      var n = catsRef.length;
      var i = Math.round((startPct / 100) * (n - 1));
      var j = Math.round((endPct / 100) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      j = Math.max(0, Math.min(n - 1, j));
      from.value = catsRef[i] || '';
      to.value = catsRef[j] || '';
    }

    function applyDateRange() {
      if (!chart || !catsRef || !catsRef.length) return;
      var a = $('#chartFrom').value, b = $('#chartTo').value;
      var n = catsRef.length;
      var i = a ? catsRef.indexOf(a) : 0;
      var j = b ? catsRef.lastIndexOf(b) : n - 1;
      if (i < 0) i = 0;
      if (j < 0) j = n - 1;
      if (j < i) { var t = i; i = j; j = t; }
      chart.dispatchAction({ type: 'dataZoom', startValue: i, endValue: j });
    }

    function ensureRangeControls() {
      if (document.getElementById('chartFrom')) return;
      var st = document.getElementById('chartStation');
      var bar = st && st.closest('.wq-toolbar');
      var spacer = bar && bar.querySelector('.wq-toolbar__spacer');
      if (!spacer) return;
      spacer.insertAdjacentHTML('beforebegin',
        '<span class="wq-toolbar__sep"></span><span>范围</span>' +
        '<span class="wq-seg" id="chartSpanSeg" role="group" aria-label="时间范围快捷切换">' +
          '<button data-days="7">近 7 天</button>' +
          '<button data-days="30">近 30 天</button>' +
          '<button data-days="0" aria-pressed="true">全部</button>' +
        '</span>' +
        '<input class="wq-input" id="chartFrom" type="date" style="width:140px" title="起始日期">' +
        '<span>至</span>' +
        '<input class="wq-input" id="chartTo" type="date" style="width:140px" title="结束日期">');
      $('#chartFrom').addEventListener('change', applyDateRange);
      $('#chartTo').addEventListener('change', applyDateRange);
      $('#chartSpanSeg').addEventListener('click', function (e) {
        var b = e.target.closest('button[data-days]');
        if (b) zoomToDays(Number(b.dataset.days));
      });
    }

    function draw() {
      var host = $('#chart');
      if (!host) return;
      if (!chart) {
        chart = echarts.init(host, null, { renderer: 'canvas' });
        /* 登记到公共图表层：这样「导出 PNG」不需要知道实例在哪创建 */
        if (WQ.charts && WQ.charts.adopt) { WQ.charts.adopt('chart', chart); }
        chart.on('datazoom', function () {
          var dz = chart.getOption().dataZoom;
          if (!dz || !dz.length) return;
          syncRangeInputs(dz[0].start == null ? 0 : dz[0].start,
                          dz[0].end == null ? 100 : dz[0].end);
        });
        ensureRangeControls();
      }
      var sid = $('#chartStation').value;
      var metric = $('#chartMetric').value;
      var showTh = $('#chartThresholds').checked;
      var rule = (WQ.state.rules || {})[metric] || null;
      var station = WQ.state.stations.filter(function (s) { return s.id === sid; })[0];
      if (!station) return;

      chart.showLoading({ text: '读取中', color: cssVar('--c-accent'), textColor: cssVar('--c-text-dim'), maskColor: 'transparent' });

      var jobs = [WQ.data.history(sid)];
      if (metric === 'TN' && station.role === 'target') jobs.push(WQ.data.predictions(station.code));
      Promise.all(jobs).then(function (res) {
        var rows = res[0] || [];
        var preds = res[1] || [];
        chart.hideLoading();

        var cats = rows.map(function (r) { return WQ.dateOnly(r.DATE); });
        var series = rows.map(function (r) { var v = r[metric]; return (v === '' || v == null) ? null : Number(v); });
        var pdata = null;
        var text = cssVar('--c-text'), dim = cssVar('--c-text-dim'), border = cssVar('--c-border');
        var accent = cssVar('--c-accent');
        var options = {
          backgroundColor: 'transparent',
          animationDuration: 220,
          /* 底部纵向预算（从容器底边往上）：
           *   0–20   留白
           *   20–46  范围条滑块
           *   60–72  说明文字
           *   82–96  X 轴日期
           * 三块各占一段、互不重叠。之前 grid.bottom=74、说明文字 bottom=54，
           * 说明正好压到日期上 —— 这就是"注释与日期重合"。 */
          grid: { left: 62, right: 30, top: 34, bottom: 96, containLabel: false },
          tooltip: {
            trigger: 'axis',
            backgroundColor: cssVar('--c-panel'),
            borderColor: border, borderWidth: 1,
            textStyle: { color: text, fontSize: 12 }
          },
          legend: { top: 4, textStyle: { color: dim, fontSize: 12 }, itemWidth: 16, itemHeight: 8 },
          dataZoom: [
            { type: 'inside', throttle: 60 },
            { type: 'slider', height: 26, bottom: 20, borderColor: border,
              /* 范围条两端原先印完整日期会被裁掉，这里只印 MM-DD */
              labelFormatter: function (v) { return String(v).slice(5); },
              textStyle: { color: dim, fontSize: 10 },
              fillerColor: 'rgba(10,132,208,.18)',
              brushSelect: false,
              showDetail: true,            // 拖动时在中间实时显示起止区间
              handleSize: '88%' }
          ],
          /* 范围条上方给一行说明，否则那条滑块没有任何提示、不知道能拖。
           * 阈值线的图例说明也放在这里：比塞进工具栏更不容易被窄屏挤掉。 */
          graphic: [{
            type: 'text', left: 62, bottom: 60, silent: true,
            style: {
              text: '时间范围：拖动两端手柄或滚轮缩放（双击还原）　·　' +
                    (showTh && rule
                      ? '参考线：黄色虚线 = 预警阈值，红色实线 = 超标阈值'
                      : '参考线已隐藏（可用工具栏复选框打开）'),
              fill: dim, fontSize: 10
            }
          }],
          xAxis: {
            type: 'category', data: cats, boundaryGap: false,
            axisLine: { lineStyle: { color: border } },
            axisLabel: {
              color: dim, fontSize: 11, hideOverlap: true,
              showMinLabel: true, showMaxLabel: true,
              formatter: function (v) { return String(v); }
            },
            splitLine: { show: false }
          },
          yAxis: {
            type: 'value', scale: true, name: FEATURE_LABEL[metric] || metric,
            nameTextStyle: { color: dim, fontSize: 11 },
            axisLine: { show: false }, axisTick: { show: false },
            axisLabel: { color: dim, fontSize: 11 },
            splitLine: { lineStyle: { color: border, type: 'dashed' } }
          },
          series: [{
            name: '实测 ' + metric, type: 'line', data: series,
            showSymbol: series.length <= 60, symbolSize: 5, symbol: 'circle',
            /* 轻微平滑 + 面积渐变 + 柔和阴影：观感更像专业仪表，而不是一根裸折线 */
            smooth: 0.26,
            lineStyle: {
              width: 2.2, color: accent,
              shadowColor: withAlpha(accent, 0.40), shadowBlur: 8, shadowOffsetY: 3
            },
            itemStyle: { color: accent, borderColor: '#fff', borderWidth: 0.8 },
            emphasis: { focus: 'series', scale: 1.7 },
            areaStyle: {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: withAlpha(accent, 0.30) },
                { offset: 0.62, color: withAlpha(accent, 0.06) },
                { offset: 1, color: withAlpha(accent, 0) }
              ])
            },
            connectNulls: true
          }]
        };

        if (preds.length) {
          var lastDate = cats.length ? cats[cats.length - 1] : null;
          preds.forEach(function (p) {
            var d = WQ.dateOnly(p.predict_date);
            if (cats.indexOf(d) < 0) { cats.push(d); series.push(null); }
          });
          pdata = cats.map(function (d) {
            var hit = preds.filter(function (p) { return WQ.dateOnly(p.predict_date) === d; })[0];
            return hit ? Number(hit.predicted_TN) : null;
          });
          options.xAxis.data = cats;
          options.series[0].data = series;
          options.series.push({
            name: '预测 TN', type: 'line', data: pdata,
            symbol: 'diamond', symbolSize: 9,
            lineStyle: {
              width: 2, type: 'dashed', color: cssVar('--c-warn'),
              shadowColor: withAlpha(cssVar('--c-warn'), 0.35), shadowBlur: 6
            },
            itemStyle: { color: cssVar('--c-warn'), borderColor: '#fff', borderWidth: 0.8 },
            emphasis: { focus: 'series', scale: 1.7 },
            connectNulls: true
          });
          if (lastDate) { /* 预测点接在实测末尾，视觉上是延续 */ }
        }

        if (showTh && rule) {
          var marks = [];
          if (rule.isRange) {
            /* pH 是区间型：两侧各两条，低值侧与高值侧都要标出来 */
            marks = [
              { v: rule.lowAlarm,  kind: 'alarm', name: '低超标' },
              { v: rule.lowWarn,   kind: 'warn',  name: '低预警' },
              { v: rule.highWarn,  kind: 'warn',  name: '高预警' },
              { v: rule.highAlarm, kind: 'alarm', name: '高超标' }
            ];
          } else {
            marks = [
              { v: rule.warn,  kind: 'warn',  name: '预警' },
              { v: rule.alarm, kind: 'alarm', name: '超标' }
            ];
          }
          var unit = rule.unit ? ' ' + rule.unit : '';
          options.series[0].markLine = {
            silent: true, symbol: 'none', animation: false,
            emphasis: { disabled: true },
            data: marks.map(function (m) {
              /* 预警 = 黄色虚线，超标 = 红色实线。
               * 颜色 + 线型两个维度同时区分：黑白打印、色弱用户也读得出来。 */
              var c = cssVar(m.kind === 'alarm' ? '--c-alarm' : '--c-warn');
              return {
                yAxis: m.v, name: m.name,
                lineStyle: m.kind === 'alarm'
                  ? { color: c, type: 'solid', width: 1.8, opacity: 0.95 }
                  : { color: c, type: 'dashed', width: 1.2, opacity: 0.9 },
                label: {
                  color: c, fontSize: 11, position: 'insideEndTop',
                  formatter: m.name + ' ' + WQ.num(m.v, 2) + unit
                }
              };
            })
          };
        }

        /* 阈值线必须在可视范围内。
         * yAxis.scale:true 会把视野收紧到数据区间，阈值线落在区间外就被裁掉 ——
         * 表现为"勾了显示阈值线，却一条都看不到"。所以显示阈值时，
         * 把阈值一起纳入 Y 轴范围（数据本身仍然清晰，只是多留出到阈值的距离）。 */
        if (showTh && rule) {
          var ths = rule.isRange
            ? [rule.lowAlarm, rule.lowWarn, rule.highWarn, rule.highAlarm]
            : [rule.warn, rule.alarm];
          var nums = (options.series[0].data || []).slice();
          for (var si = 1; si < options.series.length; si++) {
            if (options.series[si].data) { nums = nums.concat(options.series[si].data); }
          }
          nums = nums.concat(ths).filter(function (v) { return v != null && !isNaN(v); });
          if (nums.length) {
            var lo = Math.min.apply(null, nums);
            var hi = Math.max.apply(null, nums);
            if (hi === lo) { hi = lo + 1; }
            var pad = (hi - lo) * 0.08;
            /* 吸附到"整齐"的上下界。直接用 lo-pad 会得到 0.573199999964 这种值，
             * ECharts 会把它原样印在轴端 —— 实测就是轴两端出现一串浮点尾数。 */
            var b = niceBounds(lo - pad, hi + pad);
            options.yAxis.min = b[0];
            options.yAxis.max = b[1];
          }
        }

        /* 把这次真正画上去的序列留一份：导出 CSV 时直接用。
         * 从 getOption() 反解要处理 null 对齐、dataZoom、双 Y 轴，反而更脆。 */
        lastData = {
          sid: sid, code: station.code, name: station.name, metric: metric,
          cats: cats.slice(), values: series.slice(),
          pred: pdata ? pdata.slice() : null,
          rule: rule
        };

        chart.setOption(options, true);
        /* 重画后缩放会回到全量范围：日期框同步成首尾日期，
         * 若用户之前选了「近 7/30 天」，把该区间重新套用上，不打断他的选择。 */
        catsRef = cats;

        /* 图表说明：和看板里的卡片一样，每张图都能一眼看出"画的是谁、什么口径" */
        var cap = $('#chartCap');
        if (cap) {
          var unitTxt = rule && rule.unit ? '（' + rule.unit + '）' : '';
          cap.textContent = (FEATURE_LABEL[metric] || metric) + unitTxt + ' 趋势 · ' +
            station.name + '（' + station.code + '）· ' + rows.length + ' 条实测记录' +
            (preds.length ? ' + ' + preds.length + ' 条预测' : '') +
            '　·　' + (showTh ? '参考线为后端下发阈值' : '参考线已关闭');
        }
        syncRangeInputs(0, 100);
        if (curSpan) { setTimeout(function () { zoomToDays(curSpan); }, 0); }
      }).catch(function (e) {
        chart.hideLoading();
        WQ.toast('图表数据读取失败：' + e.message, 'err');
      });
    }

    return {
      show: function () { initSelectors(); setTimeout(function () { if (chart) chart.resize(); draw(); }, 40); },
      hide: function () { },
      onResize: function () { if (chart) chart.resize(); },
      onTheme: function () { if (chart) draw(); },
      onSelect: function (sid) {
        var s = WQ.state.stations.filter(function (x) { return x.id === sid; })[0];
        if (s && s.role === 'target' && $('#chartStation')) { $('#chartStation').value = sid; if (WQ.state.view === 'charts') draw(); }
      },
      onData: function () { if (WQ.state.view === 'charts') draw(); },
      draw: draw,
      currentData: function () { return lastData; }
    };
  })();
  WQ.bus.on('boot', function () {
    $('#chartMetric').addEventListener('change', chartView.draw);
    $('#chartStation').addEventListener('change', chartView.draw);
    $('#chartThresholds').addEventListener('change', chartView.draw);
  });

  /* ══════════════════════════════════════════════════════════════════════
   * 预警报告视图
   * ══════════════════════════════════════════════════════════════════════ */
  var reportView = (function () {
    /* 列表里的一行摘要：模型写的 summary 是 Markdown，直接显示会带一堆
     * "#"、"**"、"---"。这里只做"去掉标记 + 压成一行 + 截断"，不渲染。 */
    function abstract(s, n) {
      var t = String(s == null ? '' : s)
        .replace(/\r/g, '')
        .replace(/^#{1,6}\s*/gm, '')
        .replace(/\*\*|__|`/g, '')
        .replace(/^\s*-{3,}\s*$/gm, '')
        .replace(/^\s*[-*+]\s+/gm, '')
        .replace(/\s+/g, ' ')
        .trim();
      n = n || 90;
      return t.length > n ? t.slice(0, n) + '…' : t;
    }

    function renderList() {
      var host = $('#reportList');
      var list = WQ.state.reports || [];
      if (!list.length) {
        host.innerHTML = '<li style="padding:14px;color:var(--c-text-dim);font-size:var(--fs-sm)">' +
          '暂无报告。点「新建报告」粘贴监测文本即可自动抽取指标并按阈值判定。</li>';
        return;
      }
      host.innerHTML = list.map(function (r) {
        var cls = LEVEL_CLS[r.overall] || 'lv-unknown';
        return '<li class="wq-replist__item" data-report="' + esc(r.id) + '"' +
          (WQ.state.currentReport === r.id ? ' aria-selected="true"' : '') + '>' +
          '<div class="wq-replist__top">' +
            '<span class="wq-lv wq-lv--text ' + cls + '"><span class="wq-lv__dot"></span></span>' +
            '<span class="wq-replist__station">' + esc((r.station || '').toUpperCase()) + '</span>' +
            '<span class="wq-replist__date">' + esc(WQ.dateOnly(r.report_date)) + '</span>' +
            /* 每条都带删除，省得先进详情再删。
             * data-action 交给全局委托处理；冒泡到 li 时会被下面的监听器拦住，
             * 不会顺带打开报告。 */
            '<button class="wq-replist__del" data-action="delete-report" data-id="' + esc(r.id) + '"' +
              ' title="删除这份报告" aria-label="删除报告">' + icon('i-delete') + '</button>' +
          '</div>' +
          '<div class="wq-replist__sum">' + esc(abstract(r.summary || r.title || '', 88) || '（无摘要）') + '</div>' +
        '</li>';
      }).join('');
    }

    function renderDetail(r) {
      var host = $('#reportDetail');
      if (!r) {
        host.innerHTML = '<div class="wq-empty">' + icon('i-report') +
          '<div class="wq-empty__title">未选择报告</div><div>从左侧选择一份查看逐项判定结果</div></div>';
        return;
      }
      var cls = LEVEL_CLS[r.overall] || 'lv-unknown';
      var inds = r.indicators || [];
      var table = inds.length ? (
        '<table class="wq-table selectable"><thead><tr><th>指标</th><th>数值</th><th>单位</th><th>判定</th><th>说明</th></tr></thead><tbody>' +
        inds.map(function (i) {
          var lv = (WQ.dataLevel[i.level] || WQ.dataLevel.unknown);
          return '<tr><td>' + esc(i.label || i.key) + '</td>' +
            '<td class="num">' + esc(i.value) + '</td>' +
            '<td>' + esc(i.unit || '') + '</td>' +
            '<td class="wq-lv--text ' + lv.cls + '">' + lv.label + '</td>' +
            '<td style="white-space:normal">' + esc(i.note || '') + '</td></tr>';
        }).join('') + '</tbody></table>'
      ) : '<div style="color:var(--c-text-dim)">未能从正文中抽取到具体指标数值。</div>';

      /* 生成来源标注：验收时要能区分「在线大模型写的」和「规则引擎拼的」。
       * 历史报告没有 generated_by 字段，按规则生成处理（当时的真实情况）。 */
      var gen = (r.generated_by === 'llm')
        ? '<span class="wq-gen wq-gen--llm">在线大模型 ' +
            esc((r.llm && r.llm.model) || 'LLM') +
            ((r.llm && r.llm.ms) ? ' · ' + esc(String(r.llm.ms)) + ' ms' : '') +
          '</span>'
        : '<span class="wq-gen wq-gen--rules">规则引擎生成</span>';

      /* 长短两种形态：
       *   · 在线模型生成的报告把整篇文章放在 summary 里（1800+ 字、带 Markdown），
       *     这时它**就是**正文，按正文排版；
       *   · 规则/模板生成的报告 summary 是一句摘要，保持小字摘要框样式。
       * 无论哪种都必须过 WQ.md：直接 esc() 会把 "##"、"**" 和换行原样暴露出来
       * （实测：AI 报告在页面上就是一大段带井号的文字，看着像"报告没了"）。 */
      var summaryTxt = String(r.summary || '');
      var longSummary = summaryTxt.length >= 400;
      var summaryHtml = longSummary
        ? '<div class="wq-report__body selectable">' + WQ.md(summaryTxt) + '</div>'
        : '<div class="wq-report__summary selectable">' + WQ.md(summaryTxt) + '</div>';

      /* content 在 AI 报告里是"数据摘录"（151 字的指标行），在手工录入/模板报告里
       * 才是正文。按长度区分标题，避免把 151 字的摘录标成"报告正文"。 */
      var contentTxt = String(r.content || '').trim();
      var contentSection = '';
      if (contentTxt.length >= 300) {
        contentSection = '<div class="wq-report__section">报告正文</div>' +
          '<div class="wq-report__body selectable">' + WQ.md(contentTxt) + '</div>';
      } else if (contentTxt.length) {
        contentSection = '<div class="wq-report__section">数据摘录</div>' +
          '<div class="wq-report__body selectable">' + WQ.md(contentTxt) + '</div>';
      }

      host.innerHTML =
        '<div class="wq-report__head">' +
          '<span class="wq-lv wq-lv--text ' + cls + '"><span class="wq-lv__dot"></span>' +
          (WQ.dataLevel[r.overall] || WQ.dataLevel.unknown).label + '</span>' +
          '<span class="wq-report__title">' + esc(r.title || ((r.station || '').toUpperCase() + ' 水质预警报告')) + '</span>' +
          '<span class="wq-panel__spacer"></span>' +
          '<button class="wq-btn wq-btn--danger" data-action="delete-report" data-id="' + esc(r.id) + '">' +
            icon('i-delete') + '删除</button>' +
        '</div>' +
        '<div class="wq-report__meta">断面 ' + esc((r.station || '').toUpperCase()) +
          '　报告日期 ' + esc(WQ.dateOnly(r.report_date)) +
          '　来源 ' + esc(r.source || '未标注') +
          '　录入 ' + esc(String(r.created_at || '').replace('T', ' ')) +
          '　' + gen + '</div>' +
        summaryHtml +
        '<div class="wq-report__section">逐项判定</div>' + table +
        contentSection;
    }

    /* ── 主动让在线模型生成报告（SSE 流式，逐字出现）──
     * 服务端取该断面最新一条监测记录做阈值判定，再把「指标 + 判定」交给模型写结论。
     * 前端只负责把流读出来显示，**不做任何判定**，避免出现第二套等级口径。 */
    function streamGenerate(payload) {
      var host = $('#reportDetail');
      var sid = String(payload.station || '').toLowerCase();
      var label = sid.toUpperCase();
      host.innerHTML =
        '<div class="wq-report__head">' +
          '<span class="wq-lv wq-lv--text lv-unknown" id="grBadge">' +
            '<span class="wq-lv__dot"></span>生成中</span>' +
          '<span class="wq-report__title">' + esc(label) + ' 智能预警报告</span>' +
        '</div>' +
        '<div class="wq-report__meta" id="grMeta">正在读取该断面最新监测数据…</div>' +
        '<div class="wq-report__summary selectable wq-stream wq-stream--live" id="grText"></div>';

      var textEl = $('#grText');
      var metaEl = $('#grMeta');
      var badge = $('#grBadge');
      var pending = '';        // 已收到、待显示的字符（网络分块不均匀）
      var shown = '';          // 已显示
      var finalReport = null;
      var finished = false;
      var stopped = false;

      function setMeta(t) { if (metaEl) { metaEl.textContent = t; } }

      /* 固定速率吐字：网络一次可能送来整段，直接塞进 DOM 会"跳字"，
       * 看起来不像在写。这里用队列按 ~55 字/秒匀速显示，
       * 收到 done 后把剩余字符快速补完，再切到完整报告视图。 */
      var timer = setInterval(function () {
        if (stopped) { return; }
        if (!pending.length) {
          if (finished && finalReport) {
            clearInterval(timer);
            renderDetail(finalReport);      // 收尾：渲染完整报告（含逐项判定表）
          }
          return;
        }
        var n = Math.max(1, Math.ceil(55 * 0.03));
        shown += pending.slice(0, n);
        pending = pending.slice(n);
        if (textEl) { textEl.innerHTML = WQ.md(shown); }
      }, 30);

      function handle(block) {
        var ev = 'message';
        var dataLines = [];
        block.split('\n').forEach(function (ln) {
          if (ln.indexOf('event:') === 0) { ev = ln.slice(6).trim(); }
          else if (ln.indexOf('data:') === 0) { dataLines.push(ln.slice(5).trim()); }
        });
        if (!dataLines.length) { return; }
        var obj;
        try { obj = JSON.parse(dataLines.join('\n')); } catch (e) { return; }

        if (ev === 'meta') {
          var lv = obj.overall || 'unknown';
          setMeta('读取到 ' + (obj.count || 0) + ' 项指标' +
                  (obj.date ? '（数据日期 ' + obj.date + '）' : '') +
                  '　模型 ' + (obj.model || '在线模型') + ' 正在撰写…');
          if (badge) {
            badge.className = 'wq-lv wq-lv--text ' + (LEVEL_CLS[lv] || 'lv-unknown');
            badge.innerHTML = '<span class="wq-lv__dot"></span>' +
              ((WQ.dataLevel[lv] || WQ.dataLevel.unknown).label);
          }
        } else if (ev === 'delta') {
          pending += (obj.t || '');
        } else if (ev === 'done') {
          finished = true;
          finalReport = obj.report || {};
          var r = finalReport;
          if (textEl) { textEl.classList.remove('wq-stream--live'); }
          setMeta('断面 ' + label +
                  '　报告日期 ' + WQ.dateOnly(r.report_date) +
                  '　来源 ' + esc(r.source || '模型生成') + '　' +
                  (r.generated_by === 'llm' ? '在线大模型生成' : '规则引擎生成'));
          WQ.state.currentReport = r.id;
          WQ.data.reports().then(function (list) {
            WQ.state.reports = list || [];
            renderList();
          });
          /* 列表刷新会重排 renderList，但详情由吐字队列接管，等它放完再切 */
        }
      }

      /* 必须走 WQ.stream：它在闭包里拼 apiBase 并带 Authorization。
       * 原来这里直接 fetch('/api/ai_report/generate')，而主应用是 file:// 打开的，
       * 相对绝对路径会被解析成 file:///G:/api/... —— 请求根本到不了后端，
       * 表现为「点 AI 生成报告一直失败」。 */
      WQ.stream('/api/ai_report/generate', payload).then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (j) {
            throw new Error(j.message || ('HTTP ' + resp.status));
          });
        }
        if (!resp.body || !resp.body.getReader) { throw new Error('当前浏览器不支持流式响应'); }
        var reader = resp.body.getReader();
        var dec = new TextDecoder('utf-8');
        var buf = '';
        function pump() {
          return reader.read().then(function (res) {
            if (res.done) {
              if (!finished) {
                stopped = true;
                clearInterval(timer);
                if (textEl) { textEl.classList.remove('wq-stream--live'); }
                setMeta('生成中断：模型未返回完整内容');
              }
              return;
            }
            buf += dec.decode(res.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) {
              handle(buf.slice(0, idx));
              buf = buf.slice(idx + 2);
            }
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        stopped = true;
        clearInterval(timer);
        host.innerHTML = '<div class="wq-empty">' + icon('i-warn') +
          '<div class="wq-empty__title">生成失败</div><div>' + esc(e.message) + '</div></div>';
        WQ.toast('生成失败：' + e.message, 'err');
      });
    }

    function open(id) {
      WQ.state.currentReport = id;
      renderList();
      WQ.data.report(id).then(renderDetail).catch(function (e) { WQ.toast('取报告失败：' + e.message, 'err'); });
    }

    WQ.bus.on('boot', function () {
      $('#reportList').addEventListener('click', function (e) {
        // 点的是删除按钮就到此为止：交给全局 data-action 委托处理，
        // 否则会顺着冒泡把报告也打开一次，删完反而弹出一份不存在的详情。
        if (e.target.closest('[data-action="delete-report"]')) { return; }
        var li = e.target.closest('[data-report]');
        if (li) open(li.dataset.report);
      });
    });

    return {
      show: function () { renderList(); if (WQ.state.currentReport) open(WQ.state.currentReport); },
      hide: function () { },
      onResize: function () { },
      onTheme: function () { },
      onSelect: function () { },
      onData: function () { if (WQ.state.view === 'report') renderList(); },
      reload: function () {
        return WQ.data.reports().then(function (list) {
          WQ.state.reports = list || [];
          renderList();
          if (WQ.state.currentReport && !WQ.state.reports.some(function (r) { return r.id === WQ.state.currentReport; })) {
            WQ.state.currentReport = null;
            renderDetail(null);
          }
        }).catch(function (e) { WQ.toast('取报告列表失败：' + e.message, 'err'); });
      },
      open: open,
      streamGenerate: streamGenerate,
      clearDetail: function () { renderDetail(null); }
    };
  })();

  WQ.views = { map: mapView, table: tableView, charts: chartView, report: reportView };
})(window.WQ);
