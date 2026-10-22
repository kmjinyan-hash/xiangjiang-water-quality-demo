# -*- coding: utf-8 -*-
"""权限判定 —— **唯一入口**。

为什么集中在一处
----------------
按站点授权最容易出错的地方不是"没做"，而是"漏了某个接口"。
漏一个就是越权漏洞：前端看不到，不代表 `curl` 拿不到。
所以这里只暴露几个函数，所有按站点查的接口都必须经过它们；
代码审查时只需确认"这个接口有没有调用权限判定"。

规则
----
    admin   -> 全部断面，不受 user_stations 约束
    viewer  -> 看 user_stations 里分配的断面

关于默认值
----------
**新用户注册时默认授予全部站点**（在创建用户的接口里显式写入 user_stations）。
所以"没有记录"仍然表示"什么都看不见"——这是刻意保留的状态，
管理员可以清空某个人的全部授权来表达"不给任何数据"，
而不是被迫用"没有记录"来兼表"默认全开"和"明确不给"两件事。
"""
from __future__ import annotations

from .db import get_user_stations, list_custom_stations
from .stations_data import BUILTIN_IDS


def all_station_ids():
    """系统里所有合法断面 id（内置 + 用户新增）。"""
    ids = list(BUILTIN_IDS)
    try:
        ids += [str(r['id']) for r in list_custom_stations()]
    except Exception:
        pass
    return ids


def allowed_station_ids(user):
    """返回允许访问的断面 id 集合。

    ``None`` 表示「不受限」（只有 admin）。调用方必须区分 ``None`` 与空集合：
    空集合 = 没有任何权限；None = 全部权限。
    """
    if not user:
        return set()
    if str(user.get('role') or '').lower() == 'admin':
        return None
    try:
        return set(get_user_stations(user['id']))
    except Exception:
        return set()


def can_access_station(user, station_id):
    """该用户能否访问这个断面。未知断面一律拒绝。"""
    sid = str(station_id or '').strip().lower()
    if not sid:
        return False
    if sid not in all_station_ids():
        return False
    allowed = allowed_station_ids(user)
    if allowed is None:
        return True
    return sid in allowed


def filter_station_ids(user, station_ids):
    """把一组断面 id 过滤成该用户可见的。"""
    allowed = allowed_station_ids(user)
    out = []
    for sid in station_ids or []:
        s = str(sid).strip().lower()
        if allowed is None or s in allowed:
            out.append(sid)
    return out


def filter_station_dicts(user, stations):
    """把站点对象列表（含 'id' 字段）过滤成该用户可见的。"""
    allowed = allowed_station_ids(user)
    if allowed is None:
        return list(stations)
    return [s for s in stations if str(s.get('id')).lower() in allowed]


def denied_response_message():
    return '无权访问该断面'
