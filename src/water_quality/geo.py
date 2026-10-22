# -*- coding: utf-8 -*-
"""流域矢量边界：读取 GeoJSON 并做 WGS-84 → GCJ-02 纠偏。

**为什么必须纠偏**：底图用的是高德（GCJ-02），而 GeoJSON 规范规定坐标为
WGS-84。不做纠偏直接叠加，整条流域边界会整体偏移约 600 米 —— 和站点坐标
是同一个问题。这里在服务端一次性把边界换算好，前端拿到的就是可以直接贴上
高德底图的坐标。

支持的输入：
  · FeatureCollection / Feature / 裸 Geometry 都可以；
  · GeometryCollection 会递归处理；
  · 任意层级（Point / LineString / Polygon / Multi*）由 _walk 递归覆盖。

坐标系假设：WGS-84（GeoJSON 规范默认）。若拿到的是 CGCS2000，两者在
米级精度上可视为一致；若对方给的是已纠偏的 GCJ-02 数据，则**不应**再纠偏 ——
这种情况需要显式告知，见 load_basin_boundary(already_gcj=True)。
"""

import json
import math
import os

from .config import GEO_DIR

# 克拉索夫斯基椭球参数（与前端 coord.js 一致）
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lon, lat):
    """国境外不做纠偏（GCJ-02 只在中国境内生效）。"""
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs2gcj(lon, lat):
    """WGS-84 → GCJ-02。返回 (lon, lat)。"""
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlng = _transform_lng(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlng, lat + dlat


def _walk(coords, fn):
    """递归处理任意层级的坐标数组（[x, y] 是最内层）。

    注意不能用 `isinstance(coords[0], (int, float))` 之外的判断去猜层级，
    这里按「第一个元素是数字即为坐标对」来判定，覆盖 Point/Line/Polygon/Multi*。
    """
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_walk(c, fn) for c in coords]


def _shift_coords(coords):
    return _walk(coords, lambda c: list(wgs2gcj(float(c[0]), float(c[1]))))


def _shift_geometry(geom):
    if not isinstance(geom, dict):
        return geom
    gtype = geom.get('type')
    if gtype == 'GeometryCollection':
        for g in geom.get('geometries') or []:
            _shift_geometry(g)
        return geom
    if 'coordinates' in geom and geom['coordinates']:
        geom['coordinates'] = _shift_coords(geom['coordinates'])
    return geom


def _shift_feature(feat):
    if isinstance(feat, dict):
        _shift_geometry(feat.get('geometry'))
    return feat


def shift_geojson(obj, already_gcj=False):
    """就地把 GeoJSON 的坐标从 WGS-84 换成 GCJ-02（already_gcj=True 时原样返回）。"""
    if already_gcj or not isinstance(obj, dict):
        return obj
    gtype = obj.get('type')
    if gtype == 'FeatureCollection':
        for f in obj.get('features') or []:
            _shift_feature(f)
    elif gtype == 'Feature':
        _shift_feature(obj)
    elif gtype:
        _shift_geometry(obj)
    return obj


_CACHE = {'path': None, 'mtime': None, 'data': None}


def find_basin_file():
    """在 GEO_DIR 下找边界文件。

    优先匹配 basin.* ；否则取最近修改的那个 —— 负责人可能直接丢一个
    「湘江流域.geojson」进来，不必强制改名。
    """
    if not os.path.isdir(GEO_DIR):
        return None
    cands = [os.path.join(GEO_DIR, n) for n in os.listdir(GEO_DIR)
             if n.lower().endswith(('.geojson', '.json'))]
    if not cands:
        return None
    for c in cands:
        if os.path.basename(c).lower().startswith('basin'):
            return c
    return max(cands, key=os.path.getmtime)


def load_basin_boundary(already_gcj=False):
    """读取并纠偏边界。找不到文件返回 None（前端据此静默不显示）。

    带 mtime 缓存：替换文件后无需重启服务即可生效。
    """
    path = find_basin_file()
    if not path:
        return None
    mtime = os.path.getmtime(path)
    if _CACHE['path'] == path and _CACHE['mtime'] == mtime and not already_gcj:
        return _CACHE['data']

    # utf-8-sig：对方用 Windows 记事本另存常带 BOM，直接 json.load 会报错
    with open(path, 'r', encoding='utf-8-sig') as f:
        obj = json.load(f)

    obj = shift_geojson(obj, already_gcj=already_gcj)
    obj['_source'] = os.path.basename(path)
    obj['_coord_system'] = 'GCJ-02' if already_gcj else 'WGS-84 → GCJ-02'

    _CACHE.update({'path': path, 'mtime': mtime, 'data': obj})
    return obj


# ══════════════════════════════════════════════════════════════
# 通用地理图层
#
# data/geo 下的约定：
#   basin.geojson                 矢量面 —— 流域边界（默认开启）
#   landuse.json                  土地利用的类别表/名称状态
#   tiles/landuse/{z}/{x}/{y}.png 土地利用瓦片金字塔（z3–z12，原生约 z12）
#
# 两类图层都以"载荷 + 前言"的形式下发，前端用一个统一的图层注册表管理，
# 所以以后再加图层只要往 data/geo 放文件、并在 list_layers 里登记即可。
# ══════════════════════════════════════════════════════════════

def _p(name):
    return os.path.join(GEO_DIR, name)


def _read_json(name):
    # utf-8-sig：Windows 记事本另存常带 BOM，直接 json.load 会报错
    with open(_p(name), 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def list_layers():
    """列出 data/geo 下可用的图层。

    返回的每一项只含"这是什么"，不含几何/影像本体 —— 前端按需再取载荷，
    避免首屏把几十 MB 的栅格一起拉下来。
    """
    out = []
    if os.path.exists(_p('basin.geojson')):
        out.append({'key': 'basin', 'name': '流域边界', 'type': 'vector',
                    'default': True, 'kind': 'polygon'})
    if _tiles_meta() is not None:
        try:
            meta = _read_json('landuse.json')
        except Exception:
            meta = {}
        out.append({'key': 'landuse', 'name': meta.get('name') or '土地利用',
                    'type': 'composite', 'default': False,
                    'name_pending': bool(meta.get('name_pending')),
                    'note': meta.get('note', ''),
                    'classes': meta.get('classes', [])})
    return out


def layer_payload(key):
    """返回单个图层的载荷。

    矢量：GeoJSON，坐标已纠偏到 GCJ-02（与高德底图一致）。
    影像：给出图片地址与**纠偏后的地理范围**，图片本身另走 /api/geo_image。
    """
    if key == 'basin':
        obj = load_basin_boundary()
        if obj is None:
            return None
        return {'type': 'vector', 'geojson': obj}

    if key == 'landuse':
        tiles_meta = _tiles_meta()
        if tiles_meta is None:
            return None
        try:
            meta = _read_json('landuse.json')
        except Exception:
            meta = {}
        # 只给瓦片影像。河流边界已按要求撤除（不再叠加）。
        return {
            'type': 'composite',
            'tiles': {
                'url': '/api/geo_tile/landuse/{z}/{x}/{y}.png',
                'minZoom': int(tiles_meta.get('minZoom', 3)),
                'maxZoom': int(tiles_meta.get('maxNativeZoom', 12)),
                'maxNativeZoom': int(tiles_meta.get('maxNativeZoom', 12)),
                'tileSize': int(tiles_meta.get('tileSize', 256)),
            },
            'name_pending': bool(meta.get('name_pending')),
            'note': meta.get('note', ''),
            'classes': meta.get('classes', []),
        }
    return None


def _tiles_meta():
    p = _p(os.path.join('tiles', 'landuse.json'))
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception:
        return None




def layer_tile_path(key, z, x, y):
    """瓦片文件路径；不存在返回 None。做了严格的取值校验，避免路径穿越。"""
    if key != 'landuse':
        return None
    try:
        z = int(z); x = int(x); y = int(y)
    except (TypeError, ValueError):
        return None
    if not (0 <= z <= 20):
        return None
    n = 2 ** z
    if not (0 <= x < n and 0 <= y < n):
        return None
    p = _p(os.path.join('tiles', 'landuse', str(z), str(x), '%d.png' % y))
    return p if os.path.exists(p) else None

# ══════════════════════════════════════════════════════════════
# 空瓦片
#
# 土地利用只保存了"非全透明"的瓦片（省掉约 1600 个空瓦片），所以请求落在
# 空区域时会找不到文件。**不能返回 404**：Leaflet 不会把 404 的瓦片标记为
# 已加载，每次拖动/缩放都会重新请求 —— 实测有一半的瓦片请求是 404，
# 走内网穿透时这些无效往返就是拖动卡顿的主要来源。
#
# 返回一张全透明 PNG + 200 + 长缓存，浏览器与 Leaflet 都会认为"这张瓦片有了"，
# 于是不再重试。
# ══════════════════════════════════════════════════════════════

_EMPTY_TILE = {'data': None}


def _chunk(tag, data):
    import struct
    import zlib
    return (struct.pack('>I', len(data)) + tag + data
            + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))


def transparent_tile(size=256):
    """生成全透明 PNG（纯标准库，不依赖 Pillow —— 运行时镜像里没有它）。"""
    if _EMPTY_TILE['data'] is None:
        import struct
        import zlib
        raw = b''.join(b'\x00' + b'\x00' * (size * 4) for _ in range(size))
        ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
        _EMPTY_TILE['data'] = (b'\x89PNG\r\n\x1a\n'
                               + _chunk(b'IHDR', ihdr)
                               + _chunk(b'IDAT', zlib.compress(raw, 9))
                               + _chunk(b'IEND', b''))
    return _EMPTY_TILE['data']