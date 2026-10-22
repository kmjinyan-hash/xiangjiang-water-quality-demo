# -*- coding: utf-8 -*-
"""容器兼容性与安全检查单测。

覆盖的都是「改造前会真出故障」的点，不是泛泛的冒烟：

  1. WQ_ROOT / 目录环境变量覆盖      ← 装进 site-packages 后路径会跑飞
  2. 调度器可整体关闭                 ← gunicorn -w N 会让每个 worker 各跑一遍采集
  3. 业务时区                        ← 容器默认 UTC，cron 2:00 会变成北京时间 10:00
  4. 权重可 weights_only=True 加载     ← 旧格式允许 .pth 内执行任意代码
  5. 推理路径不再引入 sklearn
  6. 缺测字段写空串而不是 "0.0"        ← 本项目历史上最严重的数据缺陷
  7. ScalerLite 与 sklearn 算术一致
  8. CORS 白名单解析

配置是 import 期读取的，所以凡涉及环境变量的用例都必须换子进程测。

跑法：python tests/test_container_compat.py
"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_failures = []


def check(name, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                           ('   <- ' + str(detail)) if detail else ''))
    if not cond:
        _failures.append(name)
    return cond


def run_py(code, env_extra=None):
    """在子进程里跑一段代码，返回 (returncode, stdout, stderr)。"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('WQ_')}
    env['PYTHONPATH'] = SRC
    env['PYTHONIOENCODING'] = 'utf-8'
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, encoding='utf-8', errors='replace',
                       env=env, cwd=BASE)
    return p.returncode, (p.stdout or ''), (p.stderr or '')


# ────────────────────────── 1. 路径 ──────────────────────────

def test_paths_override():
    print('\n[1] WQ_ROOT / 目录环境变量覆盖')
    with tempfile.TemporaryDirectory() as td:
        td = os.path.abspath(td)
        code = ("from water_quality import config as c;"
                "print('ROOT', c.PROJECT_ROOT);"
                "print('DB', c.DB_PATH);"
                "print('PTH', c.MODEL_PATH)")
        rc, out, err = run_py(code, {'WQ_ROOT': td})
        check('退出码 0', rc == 0, err.strip()[-200:])
        check('PROJECT_ROOT 跟随 WQ_ROOT', ('ROOT ' + td) in out, out.strip())
        check('DB_PATH 落在 WQ_ROOT 下', os.path.join(td, 'data', 'app.db') in out, out.strip())
        check('MODEL_PATH 落在 WQ_ROOT 下', os.path.join(td, 'models') in out, out.strip())
        check('未产生「已安装进 site-packages」告警',
              'site-packages' not in err, err.strip()[-200:])

    with tempfile.TemporaryDirectory() as td:
        td = os.path.abspath(td)
        code = "from water_quality import config as c; print('DB', c.DB_PATH); print('LOG', c.LOGS_DIR)"
        rc, out, err = run_py(code, {'WQ_DATA_DIR': td, 'WQ_LOGS_DIR': td})
        check('WQ_DATA_DIR 单独生效', os.path.join(td, 'app.db') in out, out.strip())
        check('WQ_LOGS_DIR 单独生效', ('LOG ' + td) in out, out.strip())


# ────────────────────────── 2. 调度器 ──────────────────────────

def test_scheduler_toggle():
    print('\n[2] 调度器可整体关闭（多 worker 部署的前提）')
    code = "import water_quality.app as a; print('SCHED', a.scheduler is None)"
    rc, out, err = run_py(code, {'WQ_ENABLE_SCHEDULER': '0'})
    check('WQ_ENABLE_SCHEDULER=0 时不启动', rc == 0 and 'SCHED True' in out,
          out.strip()[-150:] or err.strip()[-200:])
    rc, out, err = run_py(code, {'WQ_ENABLE_SCHEDULER': '1'})
    check('默认/开启时启动', rc == 0 and 'SCHED False' in out,
          out.strip()[-150:] or err.strip()[-200:])


# ────────────────────────── 3. 时区 ──────────────────────────

def test_timezone():
    print('\n[3] 业务时区（容器默认 UTC，不设会让定时任务差 8 小时）')
    from water_quality.config import _load_timezone, TIMEZONE_NAME
    check('默认时区名', TIMEZONE_NAME == 'Asia/Shanghai', TIMEZONE_NAME)
    tz = _load_timezone('Asia/Shanghai')
    off = tz.utcoffset(dt.datetime(2026, 1, 15, 12, 0))
    check('Asia/Shanghai 偏移 +8h', off == dt.timedelta(hours=8), off)
    check('未知时区名回退而非抛异常',
          _load_timezone('Not/AZone').utcoffset(None) == dt.timedelta(0))

    code = ("from water_quality.config import now;"
            "import datetime;"
            "u = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None);"
            "print('DIFF', round((now() - u).total_seconds() / 3600))")
    rc, out, err = run_py(code)
    check('now() 比 UTC 快 8 小时', rc == 0 and 'DIFF 8' in out,
          out.strip()[-120:] or err.strip()[-200:])

    # 日志时间戳用的是进程时区（TZ），业务时间用的是 WQ_TZ。
    # 两者若不一致，看日志会把 21:00 的事读成 13:00 —— 所以未设 WQ_TZ 时必须跟随 TZ。
    code2 = "from water_quality.config import TIMEZONE_NAME; print('TZNAME', TIMEZONE_NAME)"
    rc, out, err = run_py(code2, {'WQ_TZ': 'Asia/Urumqi', 'TZ': 'Asia/Shanghai'})
    check('WQ_TZ 优先于 TZ', 'TZNAME Asia/Urumqi' in out, out.strip()[-120:])
    rc, out, err = run_py(code2, {'TZ': 'Asia/Urumqi'})
    check('未设 WQ_TZ 时跟随 TZ（避免日志与业务时区矛盾）',
          'TZNAME Asia/Urumqi' in out, out.strip()[-120:])
    rc, out, err = run_py(code2, {'WQ_TZ': '', 'TZ': ''})
    check('两者都空时用默认值', 'TZNAME Asia/Shanghai' in out, out.strip()[-120:])


# ────────────────────────── 4. 模型存档 ──────────────────────────

def test_checkpoint_safe_load():
    print('\n[4] 权重可 weights_only=True 加载（关掉 pickle 执行面）')
    import torch
    from water_quality.config import META_PATH, MODEL_PATH

    check('权重文件存在', os.path.exists(MODEL_PATH), MODEL_PATH)
    check('meta.json 存在', os.path.exists(META_PATH), META_PATH)
    if not (os.path.exists(MODEL_PATH) and os.path.exists(META_PATH)):
        return

    try:
        ck = torch.load(MODEL_PATH, map_location='cpu', weights_only=True)
        ok, why = True, ''
    except Exception as e:
        ck, ok, why = None, False, '%s: %s' % (type(e).__name__, e)
    check('weights_only=True 加载成功', ok, why)
    if ok:
        check('只含 model_state_dict', set(ck.keys()) == {'model_state_dict'}, sorted(ck.keys()))
        check('330 个张量', len(ck['model_state_dict']) == 330, len(ck['model_state_dict']))

    with open(META_PATH, encoding='utf-8') as f:
        meta = json.load(f)
    for key in ('model_config', 'input_scalers', 'target_scalers'):
        check('meta 含 %s' % key, key in meta)
    check('input_scalers 14 个', len(meta.get('input_scalers') or {}) == 14)
    check('target_scalers 5 个', len(meta.get('target_scalers') or {}) == 5)
    check('训练指标被无损保留（extra.performance_metrics）',
          'performance_metrics' in (meta.get('extra') or {}),
          sorted(meta.get('extra') or {}))


def test_no_sklearn_at_runtime():
    print('\n[5] 推理路径不再引入 sklearn')
    code = ("import sys;"
            "from water_quality.ml.inference import init_model, run_prediction;"
            "init_model(); r = run_prediction();"
            "print('N', len(r));"
            "print('SKLEARN', any(m == 'sklearn' or m.startswith('sklearn.') for m in sys.modules))")
    rc, out, err = run_py(code)
    check('退出码 0', rc == 0, err.strip()[-300:])
    check('推理出 5 个断面', 'N 5' in out, out.strip()[-150:])
    check('sklearn 未被导入', 'SKLEARN False' in out, out.strip()[-150:])


# ────────────────────────── 6. 数据完整性 ──────────────────────────

def test_missing_fields_stay_empty():
    print('\n[6] 缺测字段写空串，不伪造成 "0.0"（历史最严重的数据缺陷）')
    with tempfile.TemporaryDirectory() as td:
        code = (
            "from water_quality.db import init_db, insert_raw_data, get_db_connection\n"
            "init_db()\n"
            "n = insert_raw_data([{'断面名称': '三角洲', '监测时间': '2026-04-05 20:00',"
            " '总氮': '2.93'}])\n"
            "c = get_db_connection()\n"
            "r = c.execute('SELECT tem, ph, tn FROM raw_data').fetchone()\n"
            "print('INSERTED', n)\n"
            "print('TEM', repr(r['tem']))\n"
            "print('PH', repr(r['ph']))\n"
            "print('TN', repr(r['tn']))\n"
            "c.close()\n"
        )
        rc, out, err = run_py(code, {'WQ_DATA_DIR': td})
        check('退出码 0', rc == 0, err.strip()[-300:])
        check('插入 1 行', 'INSERTED 1' in out, out.strip()[-200:])
        check("缺列的 tem 存 '' 而非 '0.0'", "TEM ''" in out, out.strip()[-200:])
        check("缺列的 ph 存 '' 而非 '0.0'", "PH ''" in out, out.strip()[-200:])
        check('给了值的 tn 正常保存', "TN '2.93'" in out, out.strip()[-200:])

        # WAL 模式是并发写的前提
        code2 = ("from water_quality.db import get_db_connection;"
                 "c = get_db_connection();"
                 "print('JOURNAL', c.execute('PRAGMA journal_mode').fetchone()[0]); c.close()")
        rc, out, err = run_py(code2, {'WQ_DATA_DIR': td})
        check('SQLite 已启用 WAL', 'journal wal' in out.lower(), out.strip()[-120:])


# ────────────────────────── 7. ScalerLite ──────────────────────────

def test_scaler_lite_math():
    print('\n[7] ScalerLite 与 sklearn 算术一致')
    import numpy as np
    from water_quality.ml.checkpoint import ScalerLite

    s = ScalerLite([10.0, 20.0], [2.0, 5.0])
    x = np.array([[12.0, 30.0]])
    z = s.transform(x)
    check('transform: (x-mean)/scale', np.allclose(z, [[1.0, 2.0]]), z.tolist())
    back = s.inverse_transform(z)
    check('inverse_transform 可还原', np.allclose(back, x), back.tolist())

    z0 = ScalerLite([0.0], [0.0])          # sklearn 约定：方差为 0 时 scale_=1
    check('scale_=0 兜成 1（避免除零）', z0.scale_.tolist() == [1.0], z0.scale_.tolist())
    check('除零不会产生 inf', not np.isinf(z0.transform([[3.0]])).any())

    sj = s.to_json()
    check('可序列化为 JSON', set(sj) == {'mean_', 'scale_'} and sj['mean_'] == [10.0, 20.0], sj)
    json.dumps(sj)                          # 不抛异常即合格


# ────────────────────────── 8. CORS ──────────────────────────

def test_cors_config():
    print('\n[8] CORS 白名单解析')
    code = "from water_quality.config import CORS_ORIGINS; print('CORS', CORS_ORIGINS)"
    rc, out, err = run_py(code, {'WQ_CORS_ORIGINS': 'http://a.com, http://b.com'})
    check('逗号分隔并去空白', "'http://a.com', 'http://b.com'" in out, out.strip()[-150:])
    rc, out, err = run_py(code, {'WQ_CORS_ORIGINS': '*'})
    check('默认 *', "CORS ['*']" in out, out.strip()[-150:])


# ────────────────────────── 9. 目录与摘要 ──────────────────────────

def test_dirs_and_describe():
    print('\n[9] 运行时目录与配置摘要')
    from water_quality.config import (PROCESSED_DIR, LOGS_DIR, DB_PATH,
                                      ensure_runtime_dirs, describe)
    check('ensure_runtime_dirs() 返回 True', ensure_runtime_dirs() is True)
    check('processed 目录存在', os.path.isdir(PROCESSED_DIR), PROCESSED_DIR)
    check('logs 目录存在', os.path.isdir(LOGS_DIR), LOGS_DIR)
    d = describe()
    for key in ('PROJECT_ROOT', 'DATA_DIR', 'MODELS_DIR', 'TZ', 'SCHEDULER',
                'HOST:PORT', 'CORS', 'PYTHON'):
        check('配置摘要含 %s' % key, key in d, d.replace('\n', ' | ')[:120])
    check('DB_PATH 指向 data/app.db', DB_PATH.replace('\\', '/').endswith('data/app.db'), DB_PATH)


# ────────────────────────── 10. launcher 跨平台 ──────────────────────────

# ────────────────────────── 11. 前后端分离 ──────────────────────────

def test_frontend_separation():
    print('\n[11] 前后端分离：前端可独立部署，后端只给 JSON')
    root = os.path.dirname(SRC)
    front = os.path.join(root, 'frontend')

    cfg = open(os.path.join(front, 'app.config.js'), encoding='utf-8').read()
    check('app.config.js 暴露 apiBase 配置项（部署时只改这一处）', 'apiBase' in cfg)
    core = open(os.path.join(front, 'assets', 'js', 'core.js'), encoding='utf-8').read()
    check('core.js 支持 ?api= 临时指向其它后端', '[?&]api=' in core)
    check('core.js 支持 ?port= 临时指向本机某端口', '[?&]port=' in core)

    idx = open(os.path.join(front, 'index.html'), encoding='utf-8').read()
    check('index.html 用相对路径引用资源（换静态服务器不必改代码）',
          'src="assets/' in idx and 'href="assets/' in idx)
    check('index.html 是纯静态（无模板变量）', '{{' not in idx and '{%' not in idx)

    appsrc = open(os.path.join(SRC, 'water_quality', 'app.py'), encoding='utf-8').read()
    # 离线单文件版已弃用：导出脚本与内置快照机制都应已移除
    check('离线导出脚本已移除（离线版弃用）',
          not os.path.exists(os.path.join(root, 'scripts', 'build_offline_html.py')))
    check('前端不再有内置快照回退（无 WQ_EMBEDDED）', 'WQ_EMBEDDED' not in core)
    check('前端无 file:// 离线优先分支', 'OFFLINE_FIRST' not in core)
    check('前端不再生成 dist 单文件', not os.path.exists(os.path.join(root, 'dist')))

# ────────────────────────── 12. 阈值表单一真源 ──────────────────────────

def test_rules_single_source():
    print('\n[12] 阈值表单一真源（原先三份副本且已漂移）')
    from water_quality.rules import AI_WARNING_RULES, to_frontend_rules
    root = os.path.dirname(SRC)
    check('后端 8 个指标', len(AI_WARNING_RULES) == 8, sorted(AI_WARNING_RULES))

    fe = to_frontend_rules(include_aliases=False)
    check('对外同数 8 个', len(fe) == 8, sorted(fe))
    check('PH 区间型转成 isRange', fe['PH'].get('isRange') is True, sorted(fe['PH']))
    check('PH 不再有内部 type 键', 'type' not in fe['PH'])
    check('DO 转成 lowerIsWorse', fe['DO'].get('lowerIsWorse') is True, sorted(fe['DO']))
    check('对外不含 aliases', all('aliases' not in r for r in fe.values()))
    check('注入版必须带 aliases（离线页靠它做文本匹配）',
          all('aliases' in r for r in to_frontend_rules(include_aliases=True).values()))

    appsrc = open(os.path.join(SRC, 'water_quality', 'app.py'), encoding='utf-8').read()
    check('app.py 不再内嵌阈值表', "'warn': 450.0" not in appsrc)
    check('app.py 从 rules 导入', 'from .rules import' in appsrc)

    front = os.path.join(root, 'frontend')
    core = open(os.path.join(front, 'assets', 'js', 'core.js'), encoding='utf-8').read()
    check('前端经统一数据层从接口取阈值', "api('/api/ai_warning_rules')" in core)
    views = open(os.path.join(front, 'assets', 'js', 'views.js'), encoding='utf-8').read()
    check('前端只从 state.rules 读阈值，不再各存一份', 'WQ.state.rules' in views)


# ────────────────────────── 13. CSV 原子写 ──────────────────────────

def test_atomic_csv_write():
    print('\n[13] 模型输入 CSV 原子写（并发读到半个文件会污染模型输入）')
    p = os.path.join(SRC, 'water_quality', 'pipeline', 'clean.py')
    src = open(p, encoding='utf-8').read()
    check('先写 .tmp 临时文件', "tmp_path = file_path + '.tmp'" in src)
    check('再用 os.replace 原子替换', 'os.replace(tmp_path, file_path)' in src)
    check('不再直接 to_csv 覆盖目标文件', 'combined_df.to_csv(file_path' not in src)


# ────────────────────────── 14. AI 报告落库 ──────────────────────────

def test_ai_reports_persist():
    print('\n[14] AI 报告落库（原先只存内存，进程重启即丢）')
    with tempfile.TemporaryDirectory() as td:
        code = (
            "from water_quality.db import init_db, insert_ai_report, list_ai_reports, get_ai_report\n"
            "init_db()\n"
            "r = {'id': 't1', 'station': 'st10', 'report_date': '2026-04-06', 'source': '测试',\n"
            "     'title': 'T', 'overall': 'warning', 'summary': 'S',\n"
            "     'content': '总氮 1.5 mg/L',\n"
            "     'indicators': [{'key': 'TN', 'value': 1.5}],\n"
            "     'created_at': '2026-04-06T10:00:00'}\n"
            "insert_ai_report(r)\n"
            "print('LIST', len(list_ai_reports()))\n"
            "print('DETAIL', get_ai_report('t1')['indicators'][0]['value'])\n"
            "print('LIGHT_NOCONTENT', 'content' not in list_ai_reports()[0])\n"
        )
        rc, out, err = run_py(code, {'WQ_DATA_DIR': td})
        check('退出码 0', rc == 0, err.strip()[-300:])
        check('写入后可列出', 'LIST 1' in out, out.strip()[-200:])
        check('明细含逐项判定结果', 'DETAIL 1.5' in out, out.strip()[-200:])
        check('列表不返回正文（控制响应体积）', 'LIGHT_NOCONTENT True' in out, out.strip()[-200:])

        # 关键：**换一个进程**再读 —— 只有这样才能证明是落库而不是内存
        code2 = ("from water_quality.db import list_ai_reports;"
                 "print('AFTER_RESTART', len(list_ai_reports()))")
        rc, out, err = run_py(code2, {'WQ_DATA_DIR': td})
        check('**换进程仍能读到**（证明已落库）', 'AFTER_RESTART 1' in out, out.strip()[-200:])

        code3 = ("from water_quality.db import delete_ai_report, list_ai_reports;"
                 "print('DEL', delete_ai_report('t1'));"
                 "print('LEFT', len(list_ai_reports()))")
        rc, out, err = run_py(code3, {'WQ_DATA_DIR': td})
        check('可删除且删后为空', 'DEL True' in out and 'LEFT 0' in out, out.strip()[-200:])


def main():
    print('=' * 68)
    print(' 容器兼容性与安全检查 · 单测')
    print('=' * 68)
    test_paths_override()
    test_scheduler_toggle()
    test_timezone()
    test_checkpoint_safe_load()
    test_no_sklearn_at_runtime()
    test_missing_fields_stay_empty()
    test_scaler_lite_math()
    test_cors_config()
    test_dirs_and_describe()
    test_frontend_separation()
    test_rules_single_source()
    test_atomic_csv_write()
    test_ai_reports_persist()
    print('\n' + '=' * 68)
    if _failures:
        print(' 失败 %d 项：' % len(_failures))
        for f in _failures:
            print('   - %s' % f)
        return 1
    print(' 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
