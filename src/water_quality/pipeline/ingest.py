# -*- coding: utf-8 -*-
"""
ingest.py — 通用数据导入（让取数不再依赖任何单一网站）

背景
    原数据源「全国地表水水质自动监测实时数据」(szzdjc.cnemc.cn:8070)
    已被官方下线：多地多设备实测 ERR_CONNECTION_REFUSED。
    因此把取数能力从「只认一个网站」改为「只认格式」：无论数据来自内网、
    对接人导出、省厅新接口还是人工整理，只要列能对上就能入库。

    本模块**不重复实现清洗逻辑**，而是复用与在线采集完全相同的
    pipeline.clean.ingest_records()，因此数据口径、去重规则、日期归一化
    与爬虫路径逐字一致。

用法
    # 1. 看期望格式（生成模板 CSV）
    python -m water_quality.pipeline.ingest --template 模板.csv

    # 2. 预览解析结果，不写任何数据
    python -m water_quality.pipeline.ingest --csv 数据.csv --dry-run

    # 3. 正式入库并刷新 data/processed/*.csv
    python -m water_quality.pipeline.ingest --csv 数据.csv

    # 4. 列名对不上时手动指定
    python -m water_quality.pipeline.ingest --csv 数据.csv --map 监测日期=监测时间 --map 站点=断面名称

支持的输入
    * .csv          —— 自动探测编码（utf-8-sig / utf-8 / gb18030 / gbk）
    * .xlsx / .xls  —— 需要 openpyxl（缺失时给出明确提示）

容错特性
    * 列名自动识别中英文及常见别名（"--map" 可覆盖）
    * 断面名支持 中文名 / 带「断面」后缀 / stX 直写
    * 数值可带单位（'2.93 mg/L' -> 2.93）
    * 缺测标记（'--'、'-'、'N/A'、空）-> NaN，不伪造成 0
    * 文件内若混入表头行（原故障特征）会被自动丢弃
"""
import argparse
import os
import re

import pandas as pd

from .clean import STATION_MAP, ingest_records
from .crawler import INVALID_TOKENS, normalize_measure_time

# 规范字段名 -> 常见别名（中英文混排都会遇到）
COLUMN_ALIASES = {
    '断面名称': ['断面名称', '断面', '站点', '站点名称', '监测断面', '监测点名称',
                 'station', 'stationname', 'site', 'sitename'],
    '监测时间': ['监测时间', '日期', '时间', '采样时间', '监测日期', '采样日期',
                 'datetime', 'date', 'time', 'monitordate'],
    '水温': ['水温', '温度', 'tem', 'watertemperature'],
    'pH': ['pH', 'ph', 'ph值', '酸碱度'],
    '溶解氧': ['溶解氧', 'do', 'dissolvedoxygen'],
    '电导率': ['电导率', 'tds', 'conductivity', '电导'],
    '浊度': ['浊度', 'ntu', 'turbidity'],
    '高锰酸盐': ['高锰酸盐', '高锰酸盐指数', 'codmn', 'cod', '耗氧量', 'permanganate'],
    '总磷': ['总磷', 'tp', 'totalphosphorus'],
    '总氮': ['总氮', 'tn', 'totalnitrogen'],
    '氨氮': ['氨氮', 'nh3n', 'nh3-n', '氨态氮', 'ammonianitrogen'],
    '水质类别': ['水质类别', '类别', '水质', 'waterquality', 'category'],
    '流域': ['流域', '水系', 'basin'],
    '省份': ['省份', '省', 'province'],
}

# 需要按数值清洗的字段（其余按文本保留）
NUMERIC_FIELDS = {'水温', 'pH', '溶解氧', '电导率', '浊度', '高锰酸盐',
                  '总磷', '总氮', '氨氮'}

ALL_STATION_IDS = set(STATION_MAP.values())
_NAME_TO_ID = dict(STATION_MAP)
_ID_TO_NAME = {v: k for k, v in STATION_MAP.items()}

_NUM_RE = re.compile(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
_PAREN_RE = re.compile(r'[（(\[【][^）)\]】]*[）)\]】]')
_WS_RE = re.compile(r'\s+')


# ────────────────────────── 解析辅助 ──────────────────────────

def _norm_header(s):
    """列名归一化：去括号单位、去空白、ASCII 转小写。"""
    t = _PAREN_RE.sub('', str(s))
    t = _WS_RE.sub('', t).strip().lower()
    return t


def _clean_numeric(v):
    """从带单位的文本里抽出数字；缺测标记返回空串（下游变 NaN）。"""
    if v is None:
        return ''
    s = str(v).strip()
    if s in INVALID_TOKENS or s.lower() in ('nan', 'none', 'null'):
        return ''
    m = _NUM_RE.search(s)
    return m.group(0) if m else ''


def resolve_station(raw):
    """断面名 -> stX。支持中文名、带后缀、stX 直写；无法识别返回 None。"""
    s = str(raw or '').strip()
    if not s:
        return None
    m = re.fullmatch(r'(?i)st[\s_-]*(\d{1,2})', s)
    if m:
        sid = 'st%d' % int(m.group(1))
        return sid if sid in ALL_STATION_IDS else None
    for suffix in ('监测断面', '断面', '站点', '监测点', '站'):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[:-len(suffix)]
            break
    if s in _NAME_TO_ID:
        return _NAME_TO_ID[s]
    for name, sid in _NAME_TO_ID.items():
        if name in s:
            return sid
    return None


def normalize_datetime(raw):
    """任意日期写法 -> 'YYYY-MM-DD HH:MM'；无法解析返回空串。"""
    s = str(raw or '').strip()
    if not s:
        return ''
    fixed = normalize_measure_time(s)          # 处理 'MM-DD HH:MM' 无年份
    if fixed != s:
        return fixed
    ts = pd.to_datetime(s, errors='coerce', format='mixed')
    if pd.isna(ts):
        return ''
    if ts.hour or ts.minute:
        return ts.strftime('%Y-%m-%d %H:%M')
    return ts.strftime('%Y-%m-%d')


def read_table(path):
    """读 CSV/Excel。CSV 自动探测编码。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.xlsx', '.xls'):
        try:
            return pd.read_excel(path)
        except ImportError:
            raise SystemExit(
                "读取 Excel 需要 openpyxl，请先执行：pip install openpyxl\n"
                "（若不便安装，可先用 Excel 另存为 UTF-8 的 CSV 再导入）")
        except Exception as e:
            raise SystemExit("读取 Excel 失败: %s" % e)

    last = None
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'gbk'):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError as e:
            last = e
            continue
        except Exception as e:
            raise SystemExit("读取 %s 失败: %s" % (path, e))
    raise SystemExit("无法识别 %s 的编码（已尝试 utf-8-sig / utf-8 / gb18030 / gbk）：%s"
                     % (path, last))


def build_column_map(df, user_map=None):
    """把实际列名映射到规范字段名。返回 {规范名: 实际列名}。"""
    user_map = user_map or {}
    resolved = {}

    # 1) 用户显式指定优先
    for actual, canon in user_map.items():
        if actual in df.columns and canon not in resolved:
            resolved[canon] = actual

    norm_to_actual = {}
    for actual in df.columns:
        norm_to_actual.setdefault(_norm_header(actual), actual)

    # 2) 别名精确匹配
    for canon, aliases in COLUMN_ALIASES.items():
        if canon in resolved:
            continue
        for a in aliases:
            na = _norm_header(a)
            if na in norm_to_actual:
                resolved[canon] = norm_to_actual[na]
                break

    # 3) 别名子串兜底（跳过单字别名，避免误命中）
    for canon, aliases in COLUMN_ALIASES.items():
        if canon in resolved:
            continue
        hit = None
        for a in aliases:
            na = _norm_header(a)
            if len(na) < 2:
                continue
            for nh, actual in norm_to_actual.items():
                if na in nh:
                    hit = actual
                    break
            if hit:
                break
        if hit:
            resolved[canon] = hit

    return resolved


def frame_to_records(df, colmap, province_default=''):
    """DataFrame -> 规范记录列表（键为中文规范名，供 ingest_records 使用）。

    两处关键处理（都有踩坑记录）：
      1. 断面名归一化为 STATION_MAP 里的规范中文名。因为下游 ingest_records()
         是用 `df['断面名称'] == cn_name` 精确匹配的，若这里保留 '三角洲断面'
         这种带后缀的原始写法，匹配就会失败、st13 的 CSV 根本不会生成。
      2. 文件内混入的表头行必须丢弃。判据不是写死「断面名称」，而是「该单元格
         文本等于本表任一列名或任一已知别名」—— 否则导出文件把表头写成
         '站点名称 / 监测日期' 时就漏判（这一行会掉进「脏日期」桶里）。
    """
    res_name = colmap.get('断面名称')
    res_time = colmap.get('监测时间')
    if res_name is None or res_time is None:
        raise SystemExit(
            "缺少必需列：断面名称=%s，监测时间=%s\n"
            "请用 --map 实际列名=断面名称 显式指定，或先跑 --template 查看期望格式。"
            % (res_name, res_time))

    header_literals = {_norm_header(c) for c in df.columns}
    for canon, aliases in COLUMN_ALIASES.items():
        header_literals.add(_norm_header(canon))
        for a in aliases:
            header_literals.add(_norm_header(a))
    header_literals.discard('')

    records = []
    skipped_header = 0
    skipped_bad = 0
    for _, row in df.iterrows():
        raw_name = row[res_name]
        raw_time = row[res_time]

        if pd.isna(raw_name) or pd.isna(raw_time):
            skipped_header += 1                      # 空行
            continue
        name = str(raw_name).strip()
        if (not name
                or _norm_header(name) in header_literals
                or _norm_header(raw_time) in header_literals):
            skipped_header += 1                      # 文件内混入的表头行
            continue

        iso = normalize_datetime(raw_time)
        if not iso:
            skipped_bad += 1
            continue

        # 断面名归一到规范中文名（'三角洲断面' -> '三角洲'），无法识别则原样保留
        sid = resolve_station(name)
        canonical = _ID_TO_NAME.get(sid, name) if sid else name

        rec = {'断面名称': canonical, '监测时间': iso}
        if '省份' in colmap and not pd.isna(row[colmap['省份']]):
            rec['省份'] = str(row[colmap['省份']]).strip()
        elif province_default:
            rec['省份'] = province_default
        else:
            rec['省份'] = ''

        for field in ('流域', '水质类别'):
            if field in colmap and not pd.isna(row[colmap[field]]):
                rec[field] = str(row[colmap[field]]).strip()

        for field in NUMERIC_FIELDS:
            if field in colmap:
                rec[field] = _clean_numeric(row[colmap[field]])
            else:
                rec[field] = ''
        records.append(rec)

    return records, skipped_header, skipped_bad


TEMPLATE_HEADER = ['省份', '流域', '断面名称', '监测时间', '水质类别',
                   '水温', 'pH', '溶解氧', '电导率', '浊度',
                   '高锰酸盐', '氨氮', '总磷', '总氮']
TEMPLATE_ROWS = [
    ['湖南省', '长江流域', '三角洲', '2026-04-05 20:00', 'Ⅲ',
     '16.1', '8.56', '7.20', '1040.7', '18.0', '5.60', '0.043', '0.066', '2.93'],
    ['湖南省', '长江流域', '捞刀河口', '2026-04-05 19:00', 'Ⅳ',
     '17.2', '8.78', '6.10', '2155.5', '6.0', '6.09', '0.001', '0.058', '--'],
]


def write_template(path):
    pd.DataFrame(TEMPLATE_ROWS, columns=TEMPLATE_HEADER).to_csv(
        path, index=False, encoding='utf_8_sig')
    print("模板已生成：%s" % path)
    print("  列（%d）：%s" % (len(TEMPLATE_HEADER), '、'.join(TEMPLATE_HEADER)))
    print("  必需：断面名称、监测时间（其余列可缺，缺失按 NaN 处理）")
    print("  断面名可用中文名（%s）或 stX 直写"
          % '、'.join(list(STATION_MAP)[:6]) + ' 等 14 个')


# ────────────────────────── 主流程 ──────────────────────────

def run(csv_path, user_map=None, dry_run=False, write_db=True,
        processed_dir=None, strict=False, province_default=''):
    df = read_table(csv_path)
    print("=" * 72)
    print(" 通用数据导入")
    print("=" * 72)
    print("文件      : %s" % csv_path)
    print("读入行数  : %d" % len(df))
    print("原始列    : %s" % '、'.join(str(c) for c in df.columns))

    colmap = build_column_map(df, user_map)
    print("\n列映射    :")
    for canon in ('断面名称', '监测时间', '省份', '流域', '水质类别'):
        print("   %-8s <- %s" % (canon, colmap.get(canon, '（缺失）')))
    num_mapped = [f for f in NUMERIC_FIELDS if f in colmap]
    print("   指标列   <- %s（共 %d/%d）"
          % ('、'.join(num_mapped) if num_mapped else '（全部缺失！）',
             len(num_mapped), len(NUMERIC_FIELDS)))
    missing = [f for f in NUMERIC_FIELDS if f not in colmap]
    if missing:
        print("   ⚠ 未识别到的指标列：%s（将按 NaN 处理）" % '、'.join(missing))

    try:
        records, skipped_header, skipped_bad = frame_to_records(df, colmap, province_default)
    except SystemExit as e:
        print("\n%s" % e)
        return 2

    print("\n解析结果  :")
    print("   有效记录 : %d 条" % len(records))
    print("   丢弃表头行: %d 条（文件内混入的表头，原故障特征）" % skipped_header)
    print("   丢弃脏日期: %d 条" % skipped_bad)

    # 断面识别情况
    hit, miss = {}, {}
    for r in records:
        sid = resolve_station(r['断面名称'])
        if sid:
            hit[sid] = hit.get(sid, 0) + 1
        else:
            miss[r['断面名称']] = miss.get(r['断面名称'], 0) + 1
    print("   命中模型断面: %d 个 %s" % (len(hit), sorted(hit) if hit else ''))
    if miss:
        top = sorted(miss.items(), key=lambda kv: -kv[1])[:10]
        print("   ⚠ 不在 14 个目标断面内: %d 个，例如 %s"
              % (len(miss), '、'.join('%s(%d)' % kv for kv in top)))

    # 时间范围
    if records:
        dates = sorted(r['监测时间'] for r in records)
        print("   时间范围 : %s ~ %s" % (dates[0], dates[-1]))

    if not records:
        print("\n没有可入库的记录，已终止。")
        return 1

    if dry_run:
        print("\n[dry-run] 未写入任何数据。示例记录：")
        for r in records[:5]:
            print("   %s | %s | TN=%s PH=%s"
                  % (r['断面名称'], r['监测时间'], r.get('总氮'), r.get('pH')))
        print("=" * 72)
        return 0

    stats = ingest_records(records, processed_dir=processed_dir, write_db=write_db)
    print("\n入库结果  :")
    print("   raw_data 新增 : %d 行" % stats.get('inserted', 0))
    print("   命中目标断面  : %d 条" % stats.get('target_matches', 0))
    print("   刷新断面 CSV  : %d 个" % stats.get('updated_csv', 0))
    print("=" * 72)

    if stats.get('updated_csv', 0) == 0:
        print("⚠ 没有刷新任何断面 CSV —— 说明文件里的断面名都不在 14 个目标断面内。")
        print("  可用 --list-stations 查看可接受的断面名。")
        return 1
    if strict and (miss or skipped_bad):
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="通用数据导入：把任意来源的水质数据表入库并刷新模型输入",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', help='输入文件（.csv / .xlsx / .xls）')
    ap.add_argument('--template', metavar='PATH', help='生成模板 CSV 后退出')
    ap.add_argument('--list-stations', action='store_true', help='列出 14 个目标断面后退出')
    ap.add_argument('--dry-run', action='store_true', help='只解析预览，不写任何数据')
    ap.add_argument('--map', action='append', default=[], metavar='实际列=规范名',
                    help='手动指定列映射，可重复')
    ap.add_argument('--no-db', action='store_true', help='只刷新 CSV，不写 raw_data 表')
    ap.add_argument('--processed-dir', help='覆盖 CSV 输出目录（默认 data/processed）')
    ap.add_argument('--province', default='', help='文件里没有省份列时使用的默认省份')
    ap.add_argument('--strict', action='store_true',
                    help='存在未识别断面或脏日期时以非 0 退出（便于脚本校验）')
    args = ap.parse_args(argv)

    if args.list_stations:
        print("可接受的断面名（共 %d 个）：" % len(STATION_MAP))
        for name, sid in STATION_MAP.items():
            print("   %-8s -> %s" % (name, sid))
        return 0

    if args.template:
        write_template(args.template)
        return 0

    if not args.csv:
        ap.print_help()
        return 2

    user_map = {}
    for item in args.map:
        if '=' not in item:
            print("--map 参数格式应为 实际列名=规范名，收到: %s" % item)
            return 2
        actual, canon = item.split('=', 1)
        user_map[actual.strip()] = canon.strip()

    return run(args.csv, user_map=user_map, dry_run=args.dry_run,
               write_db=not args.no_db, processed_dir=args.processed_dir,
               strict=args.strict, province_default=args.province)


if __name__ == '__main__':
    raise SystemExit(main())
