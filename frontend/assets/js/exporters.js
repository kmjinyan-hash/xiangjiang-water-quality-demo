/* ══════════════════════════════════════════════════════════════════════════
 * exporters.js — ⑫ 导出：CSV（UTF-8 BOM）+ 图表 PNG + 地图 PNG
 *
 * 三条约定：
 *   · CSV 一律走 WQ.downloadCsv，它已经在开头写 UTF-8 BOM，
 *     Excel 双击打开不会乱码（不带 BOM 的 UTF-8 会被当成本地编码）；
 *   · 图表 PNG 走 ECharts 的 getDataURL，像素比 2，带底色；
 *   · 地图 PNG **必须保留底图瓦片**：底图 tileLayer 以 crossOrigin 匿名加载，
 *     画进 canvas 不会被标记为污染；若被污染（换底图源又没开 CORS），
 *     这里会给出明确提示，而不是导出一张空白图。
 * ══════════════════════════════════════════════════════════════════════════ */
(function (WQ) {
  'use strict';

  var $ = WQ.$, esc = WQ.esc, num = WQ.num;

  function stamp() {
    var d = new Date();
    return d.getFullYear() + ('0' + (d.getMonth() + 1)).slice(-2) + ('0' + d.getDate()).slice(-2) +
           '_' + ('0' + d.getHours()).slice(-2) + ('0' + d.getMinutes()).slice(-2);
  }

  function saveBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 0);
  }

  /* ── CSV：趋势图当前序列 ─────────────────────────────────────────── */
  function exportSeries() {
    var d = WQ.views.charts && WQ.views.charts.currentData && WQ.views.charts.currentData();
    if (!d || !d.cats || !d.cats.length) {
      WQ.toast('趋势图还没有数据，请先选择断面与指标', 'err');
      return;
    }
    var label = d.metric;
    var rule = d.rule;
    var cols = ['日期', '实测_' + label];
    if (d.pred) { cols.push('预测_' + label); }
    cols.push('等级', '占标率(%)', '预警阈值', '超标阈值');

    var rows = d.cats.map(function (date, i) {
      var v = d.values[i];
      var p = d.pred ? d.pred[i] : null;
      var lv = (v == null) ? 'unknown' : WQ.grade.levelOf(d.metric, v, rule);
      var ratio = (v == null) ? null : WQ.grade.ratioOf(d.metric, v, rule);
      return [
        date,
        v == null ? '' : v,
        p == null ? '' : p,
        WQ.grade.label(lv),
        ratio == null ? '' : (ratio * 100).toFixed(1),
        rule && !rule.isRange && rule.warn != null ? rule.warn : '',
        rule && !rule.isRange && rule.alarm != null ? rule.alarm : ''
      ];
    });

    WQ.downloadCsv('趋势_' + d.code + '_' + label + '_' + stamp() + '.csv', cols, rows);
    WQ.toast('已导出 ' + rows.length + ' 行（UTF-8 BOM，Excel 可直接打开）', 'ok');
  }

  /* ── CSV：分析看板台账（逐断面 × 逐指标）────────────────────────── */
  function exportInsight() {
    var api = WQ.views.insight;
    if (!api || !api.series) { WQ.toast('分析看板尚未就绪', 'err'); return; }

    api.series().then(function (series) {
      var rules = WQ.state.rules || {};
      var keys = Object.keys(rules);
      var stations = WQ.shell.visibleStations();
      var cols = ['断面编号', '断面名称', '角色', '河流', '城市', '数据日期'];
      keys.forEach(function (k) {
        cols.push(k + '值', k + '等级', k + '占标率(%)');
      });
      keys.forEach(function (k) { cols.push(k + '超标阈值'); });

      var rows = [];
      stations.forEach(function (s) {
        var list = (series[s.id] || []);
        var row = list.length ? list[list.length - 1] : null;
        if (!row) { return; }
        var line = [s.code, s.name, s.role === 'target' ? '预测断面' : '上游断面',
                    s.river || '', s.city || '', WQ.dateOnly(row.DATE)];
        keys.forEach(function (k) {
          var v = row[k];
          var has = !(v == null || v === '' || isNaN(Number(v)));
          line.push(has ? Number(v) : '',
                    has ? WQ.grade.label(WQ.grade.levelOf(k, v, rules[k])) : '',
                    has && WQ.grade.ratioOf(k, v, rules[k]) != null
                      ? (WQ.grade.ratioOf(k, v, rules[k]) * 100).toFixed(1) : '');
        });
        keys.forEach(function (k) { line.push(rules[k].alarm != null ? rules[k].alarm : ''); });
        rows.push(line);
      });

      if (!rows.length) { WQ.toast('当前筛选范围内没有可用数据', 'err'); return; }
      WQ.downloadCsv('断面台账_' + stamp() + '.csv', cols, rows);
      WQ.toast('已导出 ' + rows.length + ' 个断面（UTF-8 BOM）', 'ok');
    }).catch(function (e) {
      WQ.toast('导出失败：' + e.message, 'err');
    });
  }

  /* ── PNG：任意已登记的图表 ───────────────────────────────────────── */
  var CHART_NAME = {
    chart: '趋势图',
    chartRank: '断面排行',
    chartRadar: '占标率雷达',
    chartLevel: '等级构成',
    chartCause: '首要超标指标',
    chartCalendar: '超标日历'
  };

  function exportChartPng(btn) {
    var id = (btn && btn.dataset && btn.dataset.chart) || 'chart';
    var name = CHART_NAME[id] || id;
    if (!WQ.charts.png(id, name + '_' + stamp() + '.png')) {
      WQ.toast('这张图还没渲染完成，无法导出', 'err');
      return;
    }
    WQ.toast('已导出「' + name + '」PNG', 'ok');
  }

  /* ══════════════════════════════════════════════════════════════════
   * 地图 PNG —— 底图瓦片 + 流域边界 + 站点针脚 + 标题/图例
   *
   * 为什么不直接 canvas 截图整个 DOM：
   *   地图是 Leaflet 的 DOM 图层（tile <img> + divIcon），没有现成的
   *   "整块截图"能力；而不带 crossOrigin 的图片一旦画进 canvas，
   *   画布就被标记为污染，toBlob 会抛 SecurityError。
   * 所以这里按"当前视野"重新合成一帧：先画瓦片，再画矢量边界与针脚，
   * 最后补上标题栏与图例 —— 与屏幕上的观感一致，且完全本地完成。
   * ══════════════════════════════════════════════════════════════════ */

  function cssVar(n) {
    return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  }

  function drawTiles(ctx, host, rect) {
    var imgs = host.querySelectorAll('.leaflet-tile-pane img');
    var drawn = 0;
    Array.prototype.forEach.call(imgs, function (img) {
      if (!img.complete || !img.naturalWidth) { return; }
      var r = img.getBoundingClientRect();
      if (!r.width || !r.height) { return; }
      ctx.drawImage(img, r.left - rect.left, r.top - rect.top, r.width, r.height);
      drawn++;
    });
    return drawn;
  }

  function ringPath(ctx, map, ring) {
    var first = true;
    (ring || []).forEach(function (c) {
      if (!c || c.length < 2) { return; }
      var pt = map.latLngToContainerPoint([c[1], c[0]]);
      if (first) { ctx.moveTo(pt.x, pt.y); first = false; }
      else { ctx.lineTo(pt.x, pt.y); }
    });
  }

  function geomPath(ctx, map, geom) {
    if (!geom) { return; }
    var t = geom.type, c = geom.coordinates;
    if (t === 'Polygon') {
      c.forEach(function (ring) { ringPath(ctx, map, ring); });
    } else if (t === 'MultiPolygon') {
      c.forEach(function (poly) { poly.forEach(function (ring) { ringPath(ctx, map, ring); }); });
    } else if (t === 'LineString') {
      ringPath(ctx, map, c);
    } else if (t === 'MultiLineString') {
      c.forEach(function (line) { ringPath(ctx, map, line); });
    }
  }

  function drawBasin(ctx, map, geo) {
    if (!geo) { return false; }
    var feats = geo.features || (geo.type === 'Feature' ? [geo] : []);
    if (!feats.length) { return false; }
    ctx.save();
    ctx.beginPath();
    feats.forEach(function (f) { geomPath(ctx, map, f.geometry); });
    /* 与地图上的画法一致：先白色外描边，再黑色虚线，深浅底图上都清晰 */
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(255,255,255,.95)';
    ctx.lineWidth = 6;
    ctx.stroke();
    ctx.strokeStyle = '#000';
    ctx.lineWidth = 2.4;
    ctx.setLineDash([9, 7]);
    ctx.stroke();
    ctx.restore();
    return true;
  }

  function drawPin(ctx, x, y, w, color) {
    var h = w * 40 / 28;
    ctx.save();
    ctx.translate(x - w / 2, y - h);
    ctx.scale(w / 28, h / 40);
    var path = new Path2D('M14 1.6C7.6 1.6 2.4 6.8 2.4 13.2c0 8.6 11.6 24.4 11.6 24.4' +
                          's11.6-15.8 11.6-24.4C25.6 6.8 20.4 1.6 14 1.6z');
    ctx.fillStyle = color;
    ctx.strokeStyle = 'rgba(255,255,255,.92)';
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.fill(path);
    ctx.stroke(path);
    ctx.beginPath();
    ctx.arc(14, 13.2, 4.4, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,255,255,.95)';
    ctx.fill();
    ctx.restore();
  }

  function drawPins(ctx, map, stations) {
    stations.forEach(function (s) {
      var pt = map.latLngToContainerPoint([s.gcj.lat, s.gcj.lng]);
      var color = (WQ.views.map.pinColor ? WQ.views.map.pinColor(s) : cssVar('--c-ok'));
      var w = s.role === 'target' ? 26 : 21;
      drawPin(ctx, pt.x, pt.y, w, color);
    });
  }

  function drawLabels(ctx, map, stations) {
    ctx.save();
    ctx.font = '600 12px "Microsoft YaHei", "PingFang SC", sans-serif';
    ctx.textBaseline = 'middle';
    stations.forEach(function (s) {
      var pt = map.latLngToContainerPoint([s.gcj.lat, s.gcj.lng]);
      var label = s.name;
      var tw = ctx.measureText(label).width;
      var x = pt.x + 9, y = pt.y - 26;
      /* 标签底衬：直接在底图上写字会看不清 */
      ctx.fillStyle = 'rgba(12,16,20,.62)';
      var pad = 4;
      ctx.beginPath();
      var r = 3, w = tw + pad * 2, h = 17;
      ctx.moveTo(x + r, y - h / 2);
      ctx.lineTo(x + w - r, y - h / 2);
      ctx.quadraticCurveTo(x + w, y - h / 2, x + w, y - h / 2 + r);
      ctx.lineTo(x + w, y + h / 2 - r);
      ctx.quadraticCurveTo(x + w, y + h / 2, x + w - r, y + h / 2);
      ctx.lineTo(x + r, y + h / 2);
      ctx.quadraticCurveTo(x, y + h / 2, x, y + h / 2 - r);
      ctx.lineTo(x, y - h / 2 + r);
      ctx.quadraticCurveTo(x, y - h / 2, x + r, y - h / 2);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.fillText(label, x + pad, y);
    });
    ctx.restore();
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function drawChrome(ctx, w, h, meta) {
    var title = (WQ.cfg.appTitle || '水质智能预警平台') + ' · 断面分布';
    var when = new Date();
    var dateStr = when.getFullYear() + '-' + ('0' + (when.getMonth() + 1)).slice(-2) + '-' +
                  ('0' + when.getDate()).slice(-2) + ' ' + ('0' + when.getHours()).slice(-2) +
                  ':' + ('0' + when.getMinutes()).slice(-2);

    /* 标题栏 */
    var barH = 46;
    ctx.save();
    ctx.fillStyle = 'rgba(12,16,20,.78)';
    ctx.fillRect(0, 0, w, barH);
    ctx.font = '600 16px "Microsoft YaHei", "PingFang SC", sans-serif';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = '#fff';
    ctx.fillText(title, 16, barH / 2);
    ctx.font = '12px "Microsoft YaHei", "PingFang SC", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,.72)';
    var right = '导出 ' + dateStr;
    ctx.fillText(right, w - ctx.measureText(right).width - 16, barH / 2);
    ctx.restore();

    /* 图例（左下） */
    var items = [
      ['正常', WQ.grade.color('normal')],
      ['预警', WQ.grade.color('warn')],
      ['超标', WQ.grade.color('alarm')],
      ['未判 / 无数据', WQ.grade.color('unknown')]
    ];
    var lw = 168, lh = 22 + items.length * 20;
    var lx = 16, ly = h - lh - 34;
    ctx.save();
    ctx.fillStyle = 'rgba(12,16,20,.72)';
    roundRect(ctx, lx, ly, lw, lh, 4);
    ctx.fill();
    ctx.font = '12px "Microsoft YaHei", "PingFang SC", sans-serif';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(255,255,255,.85)';
    ctx.fillText('图例', lx + 10, ly + 14);
    items.forEach(function (it, i) {
      var yy = ly + 32 + i * 20;
      ctx.fillStyle = it[1];
      ctx.beginPath();
      ctx.arc(lx + 15, yy, 4.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,.85)';
      ctx.fillText(it[0], lx + 28, yy);
    });
    ctx.restore();

    /* 页脚：来源与口径 */
    var foot = '底图 © 高德地图　·　数据来源：平台 /api/*（导出时刻）　·　' +
               meta.stations + ' 个断面' +
               (meta.hasLanduse ? '　·　未含土地利用叠加层' : '');
    ctx.save();
    ctx.font = '11px "Microsoft YaHei", "PingFang SC", sans-serif';
    ctx.textBaseline = 'middle';
    var fw = ctx.measureText(foot).width;
    ctx.fillStyle = 'rgba(12,16,20,.72)';
    roundRect(ctx, w / 2 - fw / 2 - 10, h - 26, fw + 20, 20, 4);
    ctx.fill();
    ctx.fillStyle = 'rgba(255,255,255,.8)';
    ctx.fillText(foot, w / 2 - fw / 2, h - 16);
    ctx.restore();
  }

  function exportMapPng() {
    var mv = WQ.views.map;
    var map = mv && mv.mapObject && mv.mapObject();
    if (!map) { WQ.toast('地图尚未就绪', 'err'); return; }

    var host = map.getContainer();
    var rect = host.getBoundingClientRect();
    var w = Math.round(rect.width) || host.clientWidth;
    var h = Math.round(rect.height) || host.clientHeight;
    if (!w || !h) { WQ.toast('地图尺寸为零，无法导出', 'err'); return; }

    var stations = WQ.state.stations || [];
    if (!stations.length) { WQ.toast('断面尚未加载完成', 'err'); return; }

    /* 固定 2 倍像素比：导出图是给人贴进报告/PPT 的，不跟随屏幕 DPR，
     * 否则在 100% 缩放的普通显示器上导出的图会发虚。 */
    var dpr = 2;
    var cv = document.createElement('canvas');
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    var ctx = cv.getContext('2d');
    ctx.scale(dpr, dpr);

    /* 先铺底色：瓦片没覆盖到的边缘不会是透明 */
    ctx.fillStyle = cssVar('--c-map-void') || '#24282c';
    ctx.fillRect(0, 0, w, h);

    var tiles = drawTiles(ctx, host, rect);

    var geoTaken = false;
    var g = mv.basinGeo && mv.basinGeo();
    var p = g ? Promise.resolve(g) : WQ.data.basin().catch(function () { return null; });
    p.then(function (geo) {
      geoTaken = drawBasin(ctx, map, geo);
      drawPins(ctx, map, stations);
      drawLabels(ctx, map, stations);

      var landuseOn = !!document.querySelector('[data-action="geo-layer"][data-layer="landuse"][aria-pressed="true"]');
      drawChrome(ctx, w, h, { stations: stations.length, hasLanduse: landuseOn });

      /* 明确测一次污染：比等到 toBlob 抛异常更容易给出可行动的提示 */
      try {
        ctx.getImageData(0, 0, 1, 1);
      } catch (e) {
        WQ.toast('地图导出被浏览器拦截：底图瓦片未带 CORS 头，画布被标记为跨域。' +
                 '请换用允许跨域的底图源。', 'err');
        return;
      }

      var name = '断面分布图_' + stamp() + '.png';
      if (!tiles) {
        WQ.toast('底图瓦片尚未加载完成，本次导出只含矢量边界与站点', 'warn');
      }
      cv.toBlob(function (blob) {
        if (!blob) { WQ.toast('导出失败：画布为空', 'err'); return; }
        saveBlob(blob, name);
        WQ.toast('已导出地图 PNG（' + (tiles ? '含底图瓦片' : '无瓦片') + '）', 'ok');
      }, 'image/png');
    }).catch(function (e) {
      WQ.toast('地图导出失败：' + e.message, 'err');
    });
  }

  /* ── 动作注册 ─────────────────────────────────────────────────────── */
  WQ.actions['export-series'] = exportSeries;
  WQ.actions['export-insight'] = exportInsight;
  WQ.actions['export-chart-png'] = exportChartPng;
  WQ.actions['export-map'] = function () {
    WQ.shell.setView('map');
    /* 切到地图后容器才有尺寸，等一帧再画 */
    setTimeout(exportMapPng, 120);
  };

  WQ.exporters = {
    series: exportSeries, insight: exportInsight, chart: exportChartPng, map: exportMapPng
  };
})(window.WQ);
