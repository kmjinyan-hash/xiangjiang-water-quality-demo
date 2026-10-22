/* ══════════════════════════════════════════════════════════════════════════
 * charts.js — 图表公共件（仅前端）
 *
 * 这一层只做三件事，业务逻辑一律留在各视图里：
 *   1. 按容器 id 复用 ECharts 实例（视图切来切去不重复 init，避免内存泄漏）；
 *   2. 统一取主题色 —— 颜色全部来自 CSS 变量，深浅主题自动跟随；
 *   3. 统一的 PNG 导出（像素比 2，带底色，否则透明底贴到白纸上等于看不见）。
 *
 * 等级颜色只有一个来源：WQ.grade.COLORS。这里不另定义色板。
 * ══════════════════════════════════════════════════════════════════════════ */
(function (WQ) {
  'use strict';

  var inst = {};

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* 一套图表用色。ok/warn/alarm 取自 WQ.grade，保证与地图圆点、色块一致。 */
  function palette() {
    return {
      text: cssVar('--c-text'),
      dim: cssVar('--c-text-dim'),
      faint: cssVar('--c-text-faint'),
      border: cssVar('--c-border'),
      panel: cssVar('--c-panel'),
      panelAlt: cssVar('--c-panel-alt'),
      accent: cssVar('--c-accent'),
      ok: WQ.grade.COLORS.normal,
      warn: WQ.grade.COLORS.warn,
      alarm: WQ.grade.COLORS.alarm,
      unknown: WQ.grade.COLORS.unknown
    };
  }

  /* 容器不可见时（视图未激活）ECharts 量到的是 0 宽，画出来是空白。
   * 调用方负责在视图 show 之后调 get()，这里再做一次尺寸兜底。 */
  function get(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    var c = inst[id];
    if (!c || c.isDisposed()) {
      c = echarts.init(el, null, { renderer: 'canvas' });
      inst[id] = c;
    }
    if (el.clientWidth > 0 && el.clientHeight > 0) c.resize();
    return c;
  }

  function resizeAll() {
    Object.keys(inst).forEach(function (id) {
      var c = inst[id];
      if (c && !c.isDisposed()) c.resize();
    });
  }

  /* 通用 tooltip / 文字样式，省得每张图抄一遍 */
  function base() {
    var p = palette();
    return {
      backgroundColor: 'transparent',
      animationDuration: 260,
      textStyle: { color: p.text, fontSize: 12 },
      tooltip: {
        backgroundColor: p.panel,
        borderColor: p.border,
        borderWidth: 1,
        textStyle: { color: p.text, fontSize: 12 }
      }
    };
  }

  function png(id, filename) {
    var c = inst[id];
    if (!c || c.isDisposed()) { return false; }
    var url;
    try {
      url = c.getDataURL({
        type: 'png',
        pixelRatio: 2,
        backgroundColor: cssVar('--c-panel')
      });
    } catch (e) {
      WQ.toast('导出失败：' + e.message, 'err');
      return false;
    }
    var a = document.createElement('a');
    a.href = url;
    a.download = filename || (id + '.png');
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { a.remove(); }, 0);
    return true;
  }

  /* 窗口尺寸变化：地图/表格由外壳广播，这里只补图表实例。
   * 不用 bus.on('resize') 单独注册，避免与视图自己的 onResize 重复 resize。 */
  window.addEventListener('resize', function () { resizeAll(); });

  WQ.charts = {
    palette: palette,
    cssVar: cssVar,
    get: get,
    /* 视图自己 init 的实例（例如趋势图，它有 dataZoom 联动）登记进来，
     * 这样「导出 PNG」不必关心实例是谁创建的。 */
    adopt: function (id, chart) { inst[id] = chart; return chart; },
    resizeAll: resizeAll,
    base: base,
    png: png,
    dispose: function (id) {
      var c = inst[id];
      if (c && !c.isDisposed()) c.dispose();
      delete inst[id];
    }
  };
})(window.WQ);
