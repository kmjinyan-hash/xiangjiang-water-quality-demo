/* ══════════════════════════════════════════════════════════════════════════
 * timeline.js — ⑪ 时间轴播放（地图圆点按日期逐帧切换，6 秒/帧，可暂停）
 *
 * 帧的来源，按这个顺序决定，界面上一律写明用的是哪种：
 *   1. 预测记录：prediction_history 里的 predict_date，≥ 2 个日期才用；
 *      圆点颜色由**预测总氮**判定，非预测断面没有预测值 → 灰色。
 *   2. 实测回放：预测日期不足 2 个时自动回退到 history 的日期，
 *      圆点颜色由**当天实测总氮**判定。
 *
 * 为什么不硬只用预测记录：当前库里每个断面只有 1 条预测（一个日期），
 * 按它做播放只有一帧、点了什么也不变 —— 那不是"能做但数据少"，
 * 而是看起来坏了。回退时把原因写在播放条上，比悄悄不动更可信。
 *
 * 只改显示：不写库、不触发推理、不改任何原始记录。
 * ══════════════════════════════════════════════════════════════════════════ */
(function (WQ) {
  'use strict';

  var $ = WQ.$;
  var FRAME_MS = 6000;

  var st = {
    frames: [], idx: 0, timer: null, mode: null, playing: false, note: ''
  };

  function dateOf(v) { return WQ.dateOnly(v); }

  function tnRule() { return (WQ.state.rules || {}).TN || null; }

  function levelOfTn(v) {
    var r = tnRule();
    if (r == null || v == null || v === '' || isNaN(Number(v))) { return 'unknown'; }
    return WQ.grade.levelOf('TN', Number(v), r);
  }

  /* ── 帧构建 ─────────────────────────────────────────────────────── */

  function predictFrames(targets, byStation, dates) {
    return dates.map(function (d) {
      var values = {};
      targets.forEach(function (s) {
        var rows = byStation[s.code] || [];
        var hit = null;
        for (var i = 0; i < rows.length; i++) {
          if (dateOf(rows[i].predict_date) === d) { hit = rows[i]; break; }
        }
        if (!hit) { values[s.id] = { value: null, lv: 'unknown', src: '无预测' }; return; }
        var v = Number(hit.predicted_TN);
        values[s.id] = { value: v, lv: levelOfTn(v), src: '预测', actual: hit.actual_TN };
      });
      return { date: d, values: values, kind: 'predict' };
    });
  }

  function historyFrames() {
    /* 复用分析看板的历史缓存，避免同一批数据拉两遍 */
    return WQ.views.insight.series().then(function (SERIES) {
      var byDate = {};
      Object.keys(SERIES || {}).forEach(function (sid) {
        (SERIES[sid] || []).forEach(function (row) {
          var d = dateOf(row.DATE);
          if (!d || d === '--') { return; }
          var rec = byDate[d] || (byDate[d] = {});
          if (!(sid in rec)) { rec[sid] = row; }
        });
      });
      var dates = Object.keys(byDate).sort();
      return dates.map(function (d) {
        var values = {};
        Object.keys(byDate[d]).forEach(function (sid) {
          var v = byDate[d][sid].TN;
          var has = !(v == null || v === '' || isNaN(Number(v)));
          values[sid] = has
            ? { value: Number(v), lv: levelOfTn(v), src: '实测' }
            : { value: null, lv: 'unknown', src: '无值' };
        });
        return { date: d, values: values, kind: 'history' };
      });
    });
  }

  function build() {
    var targets = (WQ.state.stations || []).filter(function (s) { return s.role === 'target'; });
    if (!targets.length) { return Promise.reject(new Error('断面尚未加载完成')); }

    return Promise.all(targets.map(function (s) {
      return WQ.data.predictions(s.code, 365).catch(function () { return []; });
    })).then(function (lists) {
      var byStation = {};
      var dateSet = {};
      lists.forEach(function (rows, i) {
        byStation[targets[i].code] = rows || [];
        (rows || []).forEach(function (r) {
          var d = dateOf(r.predict_date);
          if (d && d !== '--') { dateSet[d] = 1; }
        });
      });
      var predDates = Object.keys(dateSet).sort();

      if (predDates.length >= 2) {
        st.mode = 'predict';
        st.note = '预测记录 ' + predDates.length + ' 个日期';
        return { frames: predictFrames(targets, byStation, predDates), mode: 'predict' };
      }
      st.mode = 'history';
      st.note = '预测记录仅 ' + predDates.length + ' 个日期，已回退为实测历史回放';
      return historyFrames().then(function (frames) {
        return { frames: frames, mode: 'history' };
      });
    });
  }

  /* ── 播放 ───────────────────────────────────────────────────────── */

  function frameAlarmCount(f) {
    var n = 0;
    Object.keys(f.values).forEach(function (k) { if (f.values[k].lv === 'alarm') { n++; } });
    return n;
  }

  function frameKnownCount(f) {
    var n = 0;
    Object.keys(f.values).forEach(function (k) { if (f.values[k].value != null) { n++; } });
    return n;
  }

  function apply(i) {
    if (!st.frames.length) { return; }
    st.idx = Math.max(0, Math.min(st.frames.length - 1, i));
    var f = st.frames[st.idx];
    WQ.state.frameValues = f.values;
    if (WQ.views.map && WQ.views.map.restyle) { WQ.views.map.restyle(); }

    var dateEl = $('#tlDate'), range = $('#tlRange'), info = $('#tlInfo');
    if (dateEl) { dateEl.textContent = f.date; }
    if (range) { range.value = String(st.idx); }
    if (info) {
      info.textContent = '第 ' + (st.idx + 1) + '/' + st.frames.length + ' 帧 · 超标 ' +
        frameAlarmCount(f) + ' 个断面 · 有值 ' + frameKnownCount(f) + ' 个';
    }
    WQ.shell.setStatus('时间轴回放 ' + f.date + ' · ' + (st.mode === 'predict' ? '预测值' : '实测值'));
  }

  function setPlayIcon(playing) {
    var btn = $('#tlPlay');
    if (!btn) { return; }
    btn.innerHTML = WQ.icon(playing ? 'i-pause' : 'i-play');
    btn.setAttribute('aria-pressed', String(!!playing));
    btn.title = playing ? '暂停（6 秒 / 帧）' : '播放（6 秒 / 帧）';
  }

  function play() {
    if (!st.frames.length) { return; }
    st.playing = true;
    setPlayIcon(true);
    if (st.timer) { clearInterval(st.timer); }
    st.timer = setInterval(function () {
      /* 切到别的视图就停：看不见的播放没有意义，还会一直重绘图标 */
      if (WQ.state.view !== 'map') { pause(); return; }
      var next = (st.idx + 1) % st.frames.length;
      apply(next);
    }, FRAME_MS);
  }

  function pause() {
    st.playing = false;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
    setPlayIcon(false);
  }

  function toggle() { if (st.playing) { pause(); } else { play(); } }

  function stop() {
    pause();
    st.frames = [];
    st.idx = 0;
    st.mode = null;
    st.note = '';
    WQ.state.frameValues = null;
    if (WQ.views.map && WQ.views.map.restyle) { WQ.views.map.restyle(); }
    var bar = $('#wqTimeline');
    if (bar) { bar.hidden = true; }
    WQ.shell.setStatus('已退出时间轴回放');
  }

  function open() {
    WQ.shell.setView('map');
    if (st.frames.length) {
      var bar0 = $('#wqTimeline');
      if (bar0) { bar0.hidden = false; }
      apply(st.idx);
      play();
      return;
    }

    open._busy = true;
    WQ.toast('正在读取预测与历史记录…');
    build().then(function (res) {
      open._busy = false;
      st.frames = res.frames || [];
      st.idx = 0;
      if (!st.frames.length) {
        WQ.toast('没有可回放的日期', 'err');
        return;
      }
      var bar = $('#wqTimeline');
      var range = $('#tlRange');
      var badge = $('#tlMode');
      if (range) { range.max = String(st.frames.length - 1); range.value = '0'; }
      if (badge) {
        badge.hidden = false;
        badge.textContent = st.mode === 'predict' ? '预测帧' : '实测帧';
        badge.title = st.note || '';
      }
      if (bar) { bar.hidden = false; }
      apply(0);
      play();
      if (st.mode === 'history') {
        WQ.toast(st.note, 'warn');
      }
    }).catch(function (e) {
      open._busy = false;
      WQ.toast('时间轴读取失败：' + e.message, 'err');
    });
  }

  /* ── 控件 ───────────────────────────────────────────────────────── */

  var bound = false;
  function bind() {
    if (bound) { return; }
    bound = true;
    var play_ = $('#tlPlay');
    var prev = $('#tlPrev');
    var next = $('#tlNext');
    var range = $('#tlRange');
    var close = $('#tlClose');

    if (play_) { play_.addEventListener('click', toggle); }
    if (prev) { prev.addEventListener('click', function () { pause(); apply(st.idx - 1); }); }
    if (next) { next.addEventListener('click', function () { pause(); apply(st.idx + 1); }); }
    if (range) {
      range.addEventListener('input', function (e) {
        pause();
        apply(Number(e.target.value));
      });
    }
    if (close) { close.addEventListener('click', stop); }

    /* Esc 退出回放：与量算、弹窗的取消键保持一致 */
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && st.frames.length && !document.querySelector('.wq-overlay')) {
        stop();
      }
    });
  }

  WQ.actions.timeline = function () {
    bind();
    open();
  };

  /* 退出登录 / 重新引导时不该留着旧帧 */
  WQ.bus.on('boot', function () { stop(); });

  WQ.timeline = {
    open: open, stop: stop, play: play, pause: pause,
    state: function () { return { mode: st.mode, frames: st.frames.length, idx: st.idx, playing: st.playing }; }
  };
})(window.WQ);
