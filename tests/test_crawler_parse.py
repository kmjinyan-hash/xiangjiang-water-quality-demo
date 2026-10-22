# -*- coding: utf-8 -*-
"""
爬虫解析层离线单测 —— 用**真实故障现场**构造 fixture，复现并锁定修复。

fixture 直接来自数据库里的证据（data/app.db 的 raw_data 表）：
  * 第 1 行就是表头：station_name='断面名称'，measure_time='监测时间'
  * 「上海市」名下的记录实际是海河流域断面（三小营、衡水湖大湖心、安州…）
    → 证明省份切换从未成功，循环变量被直接当成了数据来源
  * 监测时间是无年份的 'MM-DD HH:MM'（如 '04-05 20:00'）
  * 缺测值 '--' 被写成 '0.0'（等于把缺测伪造成实测 0）

不需要网络、不需要 selenium，可在任何环境回归。
    python tests/test_crawler_parse.py
"""
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from water_quality.pipeline.crawler import (          # noqa: E402
    detect_column_map,
    normalize_measure_time,
    records_from_rows,
    row_to_record,
    rows_from_html,
)

# 表头用 <td> 渲染 —— 这正是历史上「表头被当成数据行」的 DOM 形态
HEADER_TD_HTML = """<table>
<tr>
<td>省份</td><td>流域</td><td>断面名称</td><td>监测时间</td><td>水质类别</td>
<td>水温<br>(℃)</td><td>pH<br>(无量纲)</td><td>溶解氧<br>(mg/L)</td><td>电导率<br>(μS/cm)</td>
<td>浊度<br>(NTU)</td><td>高锰酸盐指数<br>(mg/L)</td><td>氨氮<br>(mg/L)</td><td>总磷<br>(mg/L)</td><td>总氮<br>(mg/L)</td>
</tr>
<tr><td>湖南省</td><td>长江流域</td><td>三角洲</td><td>04-05 20:00</td><td>Ⅲ</td><td>16.1</td><td>8.56</td><td>7.20</td><td>1040.7</td><td>18.0</td><td>5.60</td><td>0.043</td><td>0.066</td><td>2.93</td></tr>
<tr><td>湖南省</td><td>长江流域</td><td>捞刀河口</td><td>04-05 19:00</td><td>Ⅳ</td><td>17.2</td><td>8.78</td><td>6.10</td><td>2155.5</td><td>6.0</td><td>6.09</td><td>0.001</td><td>0.058</td><td>--</td></tr>
<tr><td>湖南省</td><td>长江流域</td><td>桔子洲</td><td>04-05 19:00</td><td>Ⅱ</td><td>16.4</td><td>8.31</td><td>9.59</td><td>1511.8</td><td>20.7</td><td>5.20</td><td>0.014</td><td>0.024</td><td>0.61</td></tr>
</table>"""

# 表头用 <th> 渲染（更规范的形态，应同样正确）
HEADER_TH_HTML = """<table><thead><tr>
<th>断面名称</th><th>监测时间</th><th>总氮</th><th>pH</th>
</tr></thead><tbody>
<tr><td>昭山</td><td>04-05 20:00</td><td>2.26</td><td>7.9</td></tr>
</tbody></table>"""

# 现场脏数据：省份被错标成上海市，实际是海河流域断面
MISLABELED_HTML = """<table>
<tr><td>流域</td><td>断面名称</td><td>监测时间</td><td>水质类别</td><td>总氮</td></tr>
<tr><td>海河流域</td><td>三小营</td><td>04-05 20:00</td><td>Ⅲ</td><td>2.93</td></tr>
</table>"""

_failures = []


def check(name, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                           ('   <- ' + str(detail)) if detail else ''))
    if not cond:
        _failures.append(name)
    return cond


def test_header_row_is_not_data():
    print('\n[1] 表头行绝不能被当成数据行（历史故障 B）')
    rows = rows_from_html(HEADER_TD_HTML)
    check('解析出 4 个 <tr>（1 表头 + 3 数据）', len(rows) == 4, '实际 %d' % len(rows))

    cmap = detect_column_map(rows)
    check('识别到表头字段', len(cmap) >= 10, '命中 %d 个' % len(cmap))
    check('断面名称列号 = 2', cmap.get('断面名称') == 2, cmap.get('断面名称'))
    check('监测时间列号 = 3', cmap.get('监测时间') == 3, cmap.get('监测时间'))
    check('总氮列号 = 13', cmap.get('总氮') == 13, cmap.get('总氮'))

    recs = records_from_rows(rows, '湖南省', cmap)
    check('抽出 3 条记录（表头被丢弃）', len(recs) == 3, '实际 %d' % len(recs))
    names = [r['断面名称'] for r in recs]
    check('记录里没有「断面名称」这个假断面', '断面名称' not in names, names)
    check('记录里没有「监测时间」这个假断面', '监测时间' not in names, names)
    check('断面名为 三角洲/捞刀河口/桔子洲',
          names == ['三角洲', '捞刀河口', '桔子洲'], names)
    check('省份标签正确写入', all(r['省份'] == '湖南省' for r in recs))


def test_th_header_variant():
    print('\n[2] <th> 表头形态同样正确')
    rows = rows_from_html(HEADER_TH_HTML)
    cmap = detect_column_map(rows)
    recs = records_from_rows(rows, '湖南省', cmap)
    check('抽出 1 条记录', len(recs) == 1, '实际 %d' % len(recs))
    check('断面名为 昭山', recs and recs[0]['断面名称'] == '昭山',
          recs[0]['断面名称'] if recs else None)


def test_missing_value_is_not_zero():
    print('\n[3] 缺测不能伪造成 0（原实现把 \'--\' 写成 \'0.0\'）')
    rows = rows_from_html(HEADER_TD_HTML)
    recs = records_from_rows(rows, '湖南省', detect_column_map(rows))
    laodao = [r for r in recs if r['断面名称'] == '捞刀河口']
    check('找到 捞刀河口', len(laodao) == 1)
    if laodao:
        check("总氮 '--' 转成空串（下游变 NaN）", laodao[0]['总氮'] == '',
              repr(laodao[0]['总氮']))
        check('同行的 pH 8.78 保留', laodao[0]['pH'] == '8.78', laodao[0]['pH'])


def test_province_label_comes_from_verified_switch():
    print('\n[4] 省份标签必须由调用方传入的「已验证省份」，不得凭循环变量臆断')
    rows = rows_from_html(MISLABELED_HTML)
    recs = records_from_rows(rows, '湖南省', detect_column_map(rows))
    check('流域列仍如实反映海河流域（数据本身不被篡改）',
          recs and recs[0]['流域'] == '海河流域',
          recs[0]['流域'] if recs else None)
    check('省份由调用方决定（切换失败时调用方必须跳过，见 crawler._switch_province）',
          recs and recs[0]['省份'] == '湖南省')


def test_time_normalization():
    print('\n[5] 无年份监测时间补全（跨年不能错一年）')
    now = datetime(2026, 10, 16, 12, 0)
    check("'04-05 20:00' -> 2026-04-05 20:00",
          normalize_measure_time('04-05 20:00', now) == '2026-04-05 20:00',
          normalize_measure_time('04-05 20:00', now))
    check("跨年：'12-31 23:00' -> 2025-12-31 23:00",
          normalize_measure_time('12-31 23:00', now) == '2025-12-31 23:00',
          normalize_measure_time('12-31 23:00', now))
    check("已带年份则原样返回",
          normalize_measure_time('2026-04-05 20:00', now) == '2026-04-05 20:00',
          normalize_measure_time('2026-04-05 20:00', now))
    check("非法时间不抛异常",
          normalize_measure_time('监测时间', now) == '监测时间',
          normalize_measure_time('监测时间', now))


def test_direct_row_guards():
    print('\n[6] 行级防线（独立于表头探测）')
    cmap = {'断面名称': 0, '监测时间': 1}
    check('断面名称单元格是表头 -> 丢弃',
          row_to_record(['断面名称', '监测时间'], 'X', cmap) is None)
    check('监测时间不像时间 -> 丢弃',
          row_to_record(['三角洲', '正常'], 'X', cmap) is None)
    check('缺少监测时间列 -> 丢弃',
          row_to_record(['三角洲'], 'X', {'断面名称': 0}) is None)
    check('空断面名 -> 丢弃',
          row_to_record(['', '04-05 20:00'], 'X', cmap) is None)
    check('正常行 -> 保留',
          (row_to_record(['三角洲', '04-05 20:00'], 'X', cmap) or {}).get('断面名称') == '三角洲')
    check('列号越界 -> 丢弃（不抛 IndexError）',
          row_to_record(['三角洲'], 'X', cmap) is None)


def test_ingest_to_temp_dir():
    print('\n[7] 入库 + 清洗（重定向到临时目录，不触碰真实数据）')
    import tempfile

    import pandas as pd

    from water_quality.pipeline.clean import MODEL_FEATURES, ingest_records

    rows = rows_from_html(HEADER_TD_HTML)
    recs = records_from_rows(rows, '湖南省', detect_column_map(rows))
    for r in recs:
        r['监测时间'] = normalize_measure_time(r['监测时间'], datetime(2026, 10, 16, 12, 0))

    with tempfile.TemporaryDirectory() as td:
        stats = ingest_records(recs, processed_dir=td, write_db=False)
        check('records = 3', stats['records'] == 3, stats)
        check('命中目标断面 3 条', stats['target_matches'] == 3, stats)
        check('刷新 3 个断面 CSV', stats['updated_csv'] == 3, stats)
        check('write_db=False 时不写库', stats['inserted'] == 0, stats)

        # 三角洲=st13、捞刀河口=st14、桔子洲=st12
        for sid in ('st12', 'st13', 'st14'):
            check('生成 %s_processed.csv' % sid,
                  os.path.exists(os.path.join(td, sid + '_processed.csv')))

        df = pd.read_csv(os.path.join(td, 'st13_processed.csv'))
        check('st13 列结构 = 模型特征', list(df.columns) == MODEL_FEATURES, list(df.columns))
        check('st13 日期归一化为 2026-04-05', list(df['DATE']) == ['2026-04-05'],
              list(df['DATE']))
        check('st13 TN = 2.93', abs(float(df['TN'].iloc[0]) - 2.93) < 1e-9, df['TN'].iloc[0])

        df14 = pd.read_csv(os.path.join(td, 'st14_processed.csv'))
        check("st14 缺测的 TN 变 NaN 而不是 0（原实现会写成 0.0）",
              pd.isna(df14['TN'].iloc[0]), df14['TN'].iloc[0])

        # 再入库一次：同日期应被去重覆盖，而不是追加
        ingest_records(recs, processed_dir=td, write_db=False)
        df2 = pd.read_csv(os.path.join(td, 'st13_processed.csv'))
        check('重复入库按 DATE 去重（行数不增长）', len(df2) == len(df),
              '%d -> %d' % (len(df), len(df2)))


def main():
    print('=' * 66)
    print(' 爬虫解析层离线单测（fixture 来自数据库真实故障现场）')
    print('=' * 66)
    test_header_row_is_not_data()
    test_th_header_variant()
    test_missing_value_is_not_zero()
    test_province_label_comes_from_verified_switch()
    test_time_normalization()
    test_direct_row_guards()
    test_ingest_to_temp_dir()
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
