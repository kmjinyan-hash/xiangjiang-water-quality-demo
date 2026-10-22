# -*- coding: utf-8 -*-
"""内置断面台账 —— **唯一数据源**。

为什么放在后端
--------------
以前这 14 个断面的名称、坐标是写在前端 `stations.js` 里的（`window.STATIONS`），
后端只有一份 ID 白名单。结果是同一套数据存在三处：
    frontend/stations.js（坐标+名称+河名+城市）
    config.VALID_STATION_IDS（只有 ID）
    pipeline/clean.py STATION_MAP（中文名→ID）
前端改了后端不知道，后端加了站点前端看不见，打包 APK 还会出现第四份。

现在统一到这里：前端只通过 `GET /api/stations` 取，不在本地存任何站点数据。
这也让"按站点授权"成为可能 —— 权限判断要在后端，前提是站点名录在 backend。

坐标约定
--------
`lon` / `lat` 一律是 **WGS-84**（数据原始坐标）。
高德底图需要 GCJ-02，转换是**显示层的事**，由前端 coord.js 完成 ——
后端不关心前端用哪家底图。
"""

# 14 个内置断面。src 字段保留原意：
#   map = 按地图逐点核对过；csv = 取自站点经纬度台账原始值
BUILTIN_STATIONS = [
    {'id': 'st0',  'name': '绿埠头',   'role': 'upstream', 'river': '湘江',   'city': '永州',
     'lon': 111.285776, 'lat': 26.297005, 'src': 'map'},
    {'id': 'st1',  'name': '诸葛庙',   'role': 'upstream', 'river': '潇水',   'city': '永州',
     'lon': 111.625778, 'lat': 26.200781, 'src': 'map'},
    {'id': 'st2',  'name': '归阳镇',   'role': 'upstream', 'river': '湘江',   'city': '衡阳',
     'lon': 112.193790, 'lat': 26.543627, 'src': 'map'},
    {'id': 'st3',  'name': '舂陵水',   'role': 'upstream', 'river': '舂陵水', 'city': '衡阳',
     'lon': 112.643396, 'lat': 26.615584, 'src': 'map'},
    {'id': 'st4',  'name': '蒸水',     'role': 'upstream', 'river': '蒸水',   'city': '衡阳',
     'lon': 112.604460, 'lat': 26.917090, 'src': 'map'},
    {'id': 'st5',  'name': '耒水',     'role': 'upstream', 'river': '耒水',   'city': '衡阳',
     'lon': 113.052337, 'lat': 27.020879, 'src': 'map'},
    {'id': 'st6',  'name': '洣水',     'role': 'upstream', 'river': '洣水',   'city': '株洲',
     'lon': 112.996290, 'lat': 27.083047, 'src': 'map'},
    {'id': 'st7',  'name': '熬洲',     'role': 'upstream', 'river': '湘江',   'city': '衡阳',
     'lon': 112.939209, 'lat': 27.305302, 'src': 'map'},
    {'id': 'st8',  'name': '渌水',     'role': 'upstream', 'river': '渌水',   'city': '株洲',
     'lon': 113.105270, 'lat': 27.787910, 'src': 'map'},
    {'id': 'st10', 'name': '涟水',     'role': 'target',   'river': '涟水',   'city': '湘潭',
     'lon': 112.882836, 'lat': 27.847727, 'src': 'map'},
    {'id': 'st11', 'name': '昭山',     'role': 'target',   'river': '湘江',   'city': '长沙',
     'lon': 112.998870, 'lat': 27.968450, 'src': 'map'},
    {'id': 'st12', 'name': '桔子洲',   'role': 'target',   'river': '湘江',   'city': '长沙',
     'lon': 112.960000, 'lat': 28.170000, 'src': 'csv'},
    {'id': 'st13', 'name': '三角洲',   'role': 'target',   'river': '浏阳河', 'city': '长沙',
     'lon': 113.043613, 'lat': 28.193177, 'src': 'map'},
    {'id': 'st14', 'name': '捞刀河口', 'role': 'target',   'river': '捞刀河', 'city': '长沙',
     'lon': 113.007524, 'lat': 28.261131, 'src': 'map'},
]

BUILTIN_BY_ID = {s['id']: s for s in BUILTIN_STATIONS}
BUILTIN_IDS = [s['id'] for s in BUILTIN_STATIONS]


def public_station(s):
    """对外的站点对象：只给数据，不含任何权限或界面用字段。

    ``code`` 是纯展示用的 ID 大写形式，放在这里是为了让前端不必自己拼 ——
    前端拿到什么就显示什么，不参与任何判断。
    """
    out = dict(s)
    out['code'] = str(s['id']).upper()
    out['builtin'] = True
    return out
