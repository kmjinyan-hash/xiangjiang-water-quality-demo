# -*- coding: utf-8 -*-
"""
冒烟测试：验证包可导入、路径真源正确、模型可加载、5 个目标断面预测值与基线一致。

背景：重构前 backend/ 以「扁平 import + sys.path 注入」运行，`import backend.app`
      会直接 ModuleNotFoundError。本测试的第 2 组用例专门守住这条回归。

用法：
    Windows:  set PYTHONPATH=src  &&  python tests\\test_smoke.py
    Linux/Mac: PYTHONPATH=src python tests/test_smoke.py

若已安装 pytest，也可直接 `pytest tests/test_smoke.py`（函数名以 test_ 开头）。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# ── 回归基线：重构前的 5 站点预测值，必须逐位一致 ──
BASELINE = {
    'ST10': 2.4268,
    'ST11': 2.0986,
    'ST12': 2.1003,
    'ST13': 2.6596,
    'ST14': 2.5435,
}
BASELINE_BASE_DATE = '2026-04-01'

_failures = []


def check(name, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                           ('   <- ' + str(detail)) if detail else ''))
    if not cond:
        _failures.append(name)
    return cond


def test_paths():
    print('\n[1] 路径真源 config.py')
    from water_quality import config
    for key in ('PROJECT_ROOT', 'PROCESSED_DIR', 'MODELS_DIR', 'LOGS_DIR',
                'DB_PATH', 'MODEL_PATH', 'FRONTEND_DIR', 'FRONTEND_CONFIG'):
        check('config.%s 已定义' % key, bool(getattr(config, key)))
    check('权重文件存在', os.path.exists(config.MODEL_PATH), config.MODEL_PATH)

    # 前端已与后端分离：独立在项目根的 frontend/ 下，是可单独部署的静态应用
    check('前端外壳 index.html 存在',
          os.path.exists(os.path.join(config.FRONTEND_DIR, 'index.html')))
    check('前端部署配置 app.config.js 存在',
          os.path.exists(config.FRONTEND_CONFIG))
    for rel in ('assets/css/theme.css', 'assets/css/app.css', 'assets/icons/icons.svg',
                'assets/js/core.js', 'assets/js/shell.js', 'assets/js/views.js',
                'assets/js/main.js', 'assets/vendor/leaflet/leaflet.js',
                'assets/vendor/echarts.min.js'):
        check('前端资源 %s 存在' % rel,
              os.path.exists(os.path.join(config.FRONTEND_DIR, rel.replace('/', os.sep))))
    idx = open(os.path.join(config.FRONTEND_DIR, 'index.html'), encoding='utf-8').read()
    check('前端不再依赖 CDN（index.html 中无 unpkg / jsdelivr）',
          'unpkg' not in idx and 'jsdelivr' not in idx)
    check('旧的前端目录 src/water_quality/web 已移除',
          not os.path.exists(os.path.join(config.PACKAGE_DIR, 'web')))

    n = len([f for f in os.listdir(config.PROCESSED_DIR) if f.endswith('_processed.csv')])
    check('清洗后 CSV 共 14 个', n == 14, '实际 %d' % n)


def test_imports():
    print('\n[2] 包可导入（重构前 backend 作包导入必失败）')
    import importlib
    for mod in ('water_quality.config', 'water_quality.db', 'water_quality.ml.model_def',
                'water_quality.ml.inference', 'water_quality.pipeline.clean',
                'water_quality.pipeline.crawler', 'water_quality.launcher'):
        try:
            importlib.import_module(mod)
            check(mod, True)
        except Exception as e:
            check(mod, False, '%s: %s' % (type(e).__name__, e))


def test_model():
    print('\n[3] 模型加载与推理（回归基线）')
    from water_quality.ml import inference as mi
    try:
        mi.init_model()
        check('模型加载成功', mi.MODEL_CACHE is not None)
    except Exception as e:
        check('模型加载成功', False, str(e))
        return
    try:
        res = mi.run_prediction()
    except Exception as e:
        check('run_prediction 可执行', False, str(e))
        return
    check('run_prediction 可执行', True)
    check('返回恰好 5 个目标断面', set(res) == set(BASELINE), '实际 %s' % sorted(res))
    for st, expect in BASELINE.items():
        got = res.get(st, {}).get('predicted_TN')
        check('%s predicted_TN = %s' % (st, expect),
              got is not None and abs(got - expect) < 1e-6, '实际 %s' % got)
        check('%s last_recorded_date = %s' % (st, BASELINE_BASE_DATE),
              res.get(st, {}).get('last_recorded_date') == BASELINE_BASE_DATE,
              '实际 %s' % res.get(st, {}).get('last_recorded_date'))


def test_routes():
    print('\n[4] 路由注册')
    import water_quality
    from water_quality.app import app
    from water_quality.config import SERVE_FRONTEND
    rules = {str(r.rule) for r in app.url_map.iter_rules()}
    for want in ('/', '/report', '/api/predict', '/api/history/<station>',
                 '/api/prediction_history/<station>', '/api/data_status',
                 '/api/download/<station>', '/api/ai_report', '/api/ai_reports',
                 '/api/ai_report/<rid>', '/api/ai_warning_rules'):
        check('路由 %s' % want, want in rules)
    check('旧 /frontend/<path:filename> 别名已移除',
          '/frontend/<path:filename>' not in rules)

    # 前后端分离后：后端只托管 frontend/ 下的静态文件（static_url_path=''），
    # 不再有包内的 /static/ ，也不再渲染任何模板。
    check('不再有包内 /static/ 路由', '/static/<path:filename>' not in rules)
    if SERVE_FRONTEND:
        check('前端静态路由已注册（后端顺带托管 frontend/）',
              '/<path:filename>' in rules)
    appsrc = open(os.path.join(os.path.dirname(water_quality.__file__), 'app.py'),
                  encoding='utf-8').read()
    check('后端不再使用模板渲染（render_template 已无引用）',
          'render_template' not in appsrc)


def main():
    print('=' * 66)
    print(' 湘江流域水质智能预警平台 · 冒烟测试')
    print('=' * 66)
    test_paths()
    test_imports()
    test_model()
    test_routes()
    print('\n' + '=' * 66)
    if _failures:
        print(' 失败 %d 项：' % len(_failures))
        for f in _failures:
            print('   - %s' % f)
        return 1
    print(' 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
