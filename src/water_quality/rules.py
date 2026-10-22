# -*- coding: utf-8 -*-
"""rules.py — AI 预警阈值表（唯一真源）

阈值参考 GB 3838-2002《地表水环境质量标准》（河流）。

为什么单独成一个模块
    这张表原先放在 app.py 里，于是有两个问题：

    1. **副作用**：app.py 在 import 期会建库、载模型、启动调度器。
       而任何想复用这张表的脚本，都得为此 import 整个应用 —— 代价与副作用都不该有。
    2. **三份副本且已经漂移**：
         · app.py            8 个指标，snake_case（lower_is_worse / low_warn / type:'range'）
         · stations.js       只有 4 个指标，camelCase（lowerIsWorse）
         · single-app.js     8 个指标，camelCase（isRange / lowWarn）
       两份 JS 的键名规范还与 Python 不同 —— 直接互相注入会打断离线判定。

   现在：Python 内部统一 snake_case，对外（JSON 接口 / 注入单文件版）用
   to_frontend_rules() 转成 camelCase。前端的判定逻辑一行都不用改，
   而改一次阈值、后端与两个前端同时生效。
"""

# 每个指标：
#   label/unit        展示用
#   warn/alarm        普通型指标的预警/超标阈值
#   lower_is_worse    数值越低越差（溶解氧）
#   type='range'      pH 这类区间型，用 low_alarm/low_warn/high_warn/high_alarm
#   aliases           从 AI 报告文本里抽取指标用的正则片段
AI_WARNING_RULES = {
    'TN':    {'label': '总氮(TN)',      'unit': 'mg/L', 'warn': 1.0,   'alarm': 2.0,
              'aliases': [r'总氮', r'TN', r'总\s*N']},
    'TP':    {'label': '总磷(TP)',      'unit': 'mg/L', 'warn': 0.2,   'alarm': 0.4,
              'aliases': [r'总磷', r'TP']},
    'CODMN': {'label': '高锰酸盐指数',  'unit': 'mg/L', 'warn': 6.0,   'alarm': 10.0,
              'aliases': [r'高锰酸盐指数', r'高锰酸盐', r'CODMn', r'耗氧量']},
    'NH3N':  {'label': '氨氮(NH3-N)',   'unit': 'mg/L', 'warn': 0.5,   'alarm': 1.0,
              'aliases': [r'氨氮', r'NH3\s*-?\s*N', r'氨态氮']},
    'DO':    {'label': '溶解氧(DO)',    'unit': 'mg/L', 'warn': 5.0,   'alarm': 2.0,
              'lower_is_worse': True, 'aliases': [r'溶解氧', r'DO']},
    'PH':    {'label': 'pH',            'unit': '',     'type': 'range',
              'low_alarm': 6.0, 'low_warn': 6.5, 'high_warn': 8.5, 'high_alarm': 9.0,
              'aliases': [r'pH\s*值', r'pH']},
    'NTU':   {'label': '浊度',          'unit': 'NTU',  'warn': 3.0,   'alarm': 5.0,
              'aliases': [r'浊度', r'NTU']},
    'TDS':   {'label': '溶解性总固体',  'unit': 'mg/L', 'warn': 450.0, 'alarm': 1000.0,
              'aliases': [r'溶解性总固体', r'TDS', r'矿化度']},
}

# snake_case -> 前端 camelCase。两个前端页面历史上用的就是这套 camelCase，
# 所以转换放在这里、前端判定代码不必改动。
_FRONTEND_KEY_MAP = {
    'lower_is_worse': 'lowerIsWorse',
    'low_alarm': 'lowAlarm',
    'low_warn': 'lowWarn',
    'high_warn': 'highWarn',
    'high_alarm': 'highAlarm',
}


def to_frontend_rules(include_aliases=True):
    """转成前端（JS）使用的键名结构。

    type='range' 在 JS 侧写作 isRange=True（保持前端既有写法）。

    :param include_aliases: JSON 接口默认不带 aliases（正则片段对展示无意义），
                            但注入单文件版时必须带 —— 离线页要靠它做文本匹配。
    """
    out = {}
    for key, rule in AI_WARNING_RULES.items():
        r = {k: v for k, v in rule.items() if k != 'type'}
        for old, new in _FRONTEND_KEY_MAP.items():
            if old in r:
                r[new] = r.pop(old)
        if rule.get('type') == 'range':
            r['isRange'] = True
        if not include_aliases:
            r.pop('aliases', None)
        out[key] = r
    return out
