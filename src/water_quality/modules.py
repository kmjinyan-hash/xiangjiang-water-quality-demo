# -*- coding: utf-8 -*-
"""功能模块注册表 —— 让"可插拔"变成可验证的事实。

为什么需要它
------------
系统里有几块能力，重要性、依赖、部署条件各不相同：

    预测      需要 .pth 权重
    AI 报告   需要在线大模型 API key
    数据采集   需要外网与 selenium
    地图图层   需要 data/geo 下的瓦片与边界
    用户权限   永远需要

对使用者来说，这些应该是**可开关的模块**，而不是"装了这一整套就得全有"。
比如没有 API key 的环境不该暴露 AI 报告入口；不需要采集的环境不该跑爬虫。

怎么插拔
--------
两道口子：

1. **部署期开关**（本文件）
   WQ_MODULES=ai,crawler            只启用列出的（白名单）
   WQ_DISABLE_MODULES=crawler       额外禁用（黑名单，优先于白名单）
   未列出的走每个模块的 default。

2. **路由守卫**
   `before_request` 按路径找到所属模块，模块被禁用就直接 503，
   并在响应里说明"该模块未启用"。这样前端不必知道细节，
   第三方调用方也能得到明确原因。

如何新增一个模块
----------------
在 MODULES 里加一条 + 在 MODULE_PATHS 里登记它的路径前缀 + 写实现。
不需要改其它模块，也不需要改管理界面（界面从 /api/admin/modules 渲染）。
"""
from __future__ import annotations

import os

# 模块清单。order 决定管理界面的展示顺序。
MODULES = [
    {
        'key': 'predict', 'name': '水质预测', 'order': 10, 'default': True,
        'desc': '加载 .pth 权重做断面 TN 预测，提供预测、历史、数据下载。',
        'requires': ['models/*.pth'],
    },
    {
        'key': 'reports', 'name': '预警报告', 'order': 20, 'default': True,
        'desc': '按阈值规则判定等级、管理报告。不依赖大模型也能用（规则摘要）。',
        'requires': [],
    },
    {
        'key': 'ai', 'name': 'AI 报告生成', 'order': 30, 'default': True,
        'desc': '调用在线大模型撰写结论与处置建议。缺 API key 时自动降级为规则摘要。',
        'requires': ['WQ_LLM_API_KEY'],
    },
    {
        'key': 'crawler', 'name': '数据采集', 'order': 40, 'default': True,
        'desc': '定时抓取监测数据并入库。需要外网；容器内需 selenium + chromium。',
        'requires': ['外网', 'selenium'],
    },
    {
        'key': 'geo', 'name': '地图图层', 'order': 50, 'default': True,
        'desc': '流域边界、土地利用瓦片等地理图层与瓦片服务。',
        'requires': ['data/geo'],
    },
    {
        'key': 'users', 'name': '用户与权限', 'order': 60, 'default': True,
        'desc': '登录、用户管理、按站点授权。关掉会导致无法登录，慎用。',
        'requires': [],
        'essential': True,
    },
]

# 路径前缀 -> 模块 key。长的放前面，避免 /api/ai_report/generate 被 /api/ai_report 抢走。
MODULE_PATHS = [
    ('/api/ai_report/generate', 'ai'),
    ('/api/llm_status', 'ai'),
    ('/api/ai_report', 'reports'),
    ('/api/ai_reports', 'reports'),
    ('/api/ai_warning_rules', 'reports'),
    ('/api/predict', 'predict'),
    ('/api/history/', 'predict'),
    ('/api/prediction_history/', 'predict'),
    ('/api/data_status', 'predict'),
    ('/api/download/', 'predict'),
    ('/api/stations', 'predict'),
    ('/api/geo_', 'geo'),
    ('/api/users', 'users'),
]


def _split_env(name):
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.replace(';', ',').split(',') if x.strip()}


_WHITELIST = _split_env('WQ_MODULES')
_BLACKLIST = _split_env('WQ_DISABLE_MODULES')


def is_enabled(key):
    """模块是否启用。essential 模块不允许被关掉（否则系统没法登录）。"""
    key = (key or '').strip().lower()
    mod = next((m for m in MODULES if m['key'] == key), None)
    if not mod:
        return False
    if mod.get('essential'):
        return True
    if key in _BLACKLIST:
        return False
    if _WHITELIST:
        return key in _WHITELIST
    return bool(mod.get('default', True))


def module_for_path(path):
    """路径属于哪个模块；不属于任何模块返回 None（例如 /api/auth/*）。"""
    p = path or ''
    for prefix, key in MODULE_PATHS:
        if p.startswith(prefix):
            return key
    return None


def list_modules():
    """给管理界面的清单：含启用状态与实际依赖是否满足。"""
    out = []
    for m in sorted(MODULES, key=lambda x: x.get('order', 999)):
        item = dict(m)
        item['enabled'] = is_enabled(m['key'])
        item['deps'] = [_dep_state(d) for d in (m.get('requires') or [])]
        out.append(item)
    return out


def _dep_state(dep):
    """粗判依赖是否就绪，只做提示用途，不做硬性阻断。"""
    key = (dep or '').strip()
    if key.startswith('WQ_'):
        return {'name': key, 'ok': bool(os.environ.get(key))}
    if key == 'data/geo':
        from .config import PROJECT_ROOT
        return {'name': key, 'ok': os.path.isdir(os.path.join(PROJECT_ROOT, 'data', 'geo'))}
    if key.startswith('models/'):
        from .config import MODELS_DIR
        return {'name': key, 'ok': os.path.isdir(MODELS_DIR) and bool(os.listdir(MODELS_DIR))}
    return {'name': key, 'ok': True}      # 外网/selenium 这类无法静态判断，标记为未知
