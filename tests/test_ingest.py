# -*- coding: utf-8 -*-
"""
通用数据导入的单测：列名识别、断面名解析、数值/日期清洗、编码探测、入库与 dry-run。

场景来自真实会遇到的情况：
  * Excel 另存的 GBK 编码 CSV
  * 列名带单位后缀（"水温(℃)"、"溶解氧(mg/L)"）
  * 列名写成「站点名称 / 监测日期」
  * 断面名带后缀（"三角洲断面"）或直接写 stX
  * 缺测标记 '--'
  * 文件里混入表头行（原爬虫故障的同源问题）

    python tests/test_ingest.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pandas as pd  # noqa: E402

from water_quality.pipeline.ingest import (            # noqa: E402
    _clean_numeric,
    build_column_map,
    frame_to_records,
    normalize_datetime,
    read_table,
    resolve_station,
    run,
)

_failures = []

# 一张「Excel 风格」的表：列名带单位、断面名带后缀、含缺测
CSV_COLUMNS = ['省份', '流域', '站点名称', '监测日期', '水质类别',
               '水温(℃)', 'ph', '溶解氧(mg/L)', '电导率(μS/cm)', '浊度(NTU)',
               '高锰酸盐指数(mg/L)', '氨氮(mg/L)', '总磷(mg/L)', '总氮(mg/L)']
CSV_ROWS = [
    ['湖南省', '长江流域', '三角洲断面', '2026-04-05 20:00', 'Ⅲ',
     '16.1', '8.56', '7.20', '1040.7', '18.0', '5.60', '0.043', '0.066', '2.93'],
    ['湖南省', '长江流域', '捞刀河口', '2026/4/5 19:00', 'Ⅳ',
     '17.2', '8.78', '6.10', '2155.5', '6.0', '6.09', '0.001', '0.058', '--'],
    ['湖南省', '长江流域', '桔子洲', '04-05 19:00', 'Ⅱ',
     '16.4', '8.31', '9.59', '1511.8', '20.7', '5.20', '0.014', '0.024', '0.61 mg/L'],
    ['湖南省', '长江流域', '黄河花园口', '2026-04-05 20:00', 'Ⅴ',
     '15.0', '8.00', '6.00', '900.0', '10.0', '5.00', '0.1', '0.1', '1.0'],
]


def check(name, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                           ('   <- ' + str(detail)) if detail else ''))
    if not cond:
        _failures.append(name)
    return cond


def _make_df():
    return pd.DataFrame(CSV_ROWS, columns=CSV_COLUMNS)


def test_column_alias_mapping():
    print('\n[1] 列名别名识别（中英文 + 带单位后缀）')
    cm = build_column_map(_make_df())
    for canon, actual in [('断面名称', '站点名称'), ('监测时间', '监测日期'),
                          ('水温', '水温(℃)'), ('pH', 'ph'),
                          ('溶解氧', '溶解氧(mg/L)'), ('电导率', '电导率(μS/cm)'),
                          ('浊度', '浊度(NTU)'), ('高锰酸盐', '高锰酸盐指数(mg/L)'),
                          ('氨氮', '氨氮(mg/L)'), ('总磷', '总磷(mg/L)'),
                          ('总氮', '总氮(mg/L)'), ('省份', '省份')]:
        check('%s <- %s' % (canon, actual), cm.get(canon) == actual, cm.get(canon))


def test_user_map_override():
    print('\n[2] --map 显式覆盖优先于自动识别')
    df = pd.DataFrame(columns=['A', 'B', 'TN'])
    cm = build_column_map(df, user_map={'A': '断面名称', 'B': '监测时间'})
    check('断面名称 <- A', cm.get('断面名称') == 'A', cm.get('断面名称'))
    check('监测时间 <- B', cm.get('监测时间') == 'B', cm.get('监测时间'))
    check('总氮 仍能自动识别', cm.get('总氮') == 'TN', cm.get('总氮'))


def test_station_resolution():
    print('\n[3] 断面名 -> stX')
    check("'三角洲'", resolve_station('三角洲') == 'st13', resolve_station('三角洲'))
    check("'三角洲断面' 去后缀", resolve_station('三角洲断面') == 'st13',
          resolve_station('三角洲断面'))
    check("'ST10' 直写", resolve_station('ST10') == 'st10', resolve_station('ST10'))
    check("'st10' 小写", resolve_station('st10') == 'st10', resolve_station('st10'))
    check("' 昭山 ' 去空白", resolve_station(' 昭山 ') == 'st11', resolve_station(' 昭山 '))
    check("'黄河花园口' 不在目标内 -> None",
          resolve_station('黄河花园口') is None, resolve_station('黄河花园口'))
    check("'st9' 数据集不存在 -> None", resolve_station('st9') is None, resolve_station('st9'))
    check("空串 -> None", resolve_station('') is None)


def test_numeric_cleaning():
    print('\n[4] 数值清洗（带单位 / 缺测）')
    check("'2.93 mg/L' -> '2.93'", _clean_numeric('2.93 mg/L') == '2.93',
          _clean_numeric('2.93 mg/L'))
    check("'16.1' -> '16.1'", _clean_numeric('16.1') == '16.1')
    check("'--' -> ''（缺测不伪造成 0）", _clean_numeric('--') == '',
          repr(_clean_numeric('--')))
    check("'-' -> ''", _clean_numeric('-') == '', repr(_clean_numeric('-')))
    check("'N/A' -> ''", _clean_numeric('N/A') == '', repr(_clean_numeric('N/A')))
    check("'nan' -> ''", _clean_numeric('nan') == '', repr(_clean_numeric('nan')))
    check("'' -> ''", _clean_numeric('') == '')
    check("'8.56' 保留", _clean_numeric('8.56') == '8.56')


def test_datetime_normalization():
    print('\n[5] 日期归一化')
    check("'2026-04-05 20:00'",
          normalize_datetime('2026-04-05 20:00') == '2026-04-05 20:00',
          normalize_datetime('2026-04-05 20:00'))
    check("'2026/4/5' -> '2026-04-05'",
          normalize_datetime('2026/4/5') == '2026-04-05', normalize_datetime('2026/4/5'))
    check("'2026/4/5 19:00' -> 带时间",
          normalize_datetime('2026/4/5 19:00') == '2026-04-05 19:00',
          normalize_datetime('2026/4/5 19:00'))
    got = normalize_datetime('04-05 19:00')
    check("'04-05 19:00' 自动补年", got.endswith('-04-05 19:00') and got[:2] == '20', got)
    check("'不是日期' -> ''", normalize_datetime('不是日期') == '',
          repr(normalize_datetime('不是日期')))
    check("空串 -> ''", normalize_datetime('') == '')


def test_gbk_csv_read():
    print('\n[6] GBK 编码 CSV 读取（Excel 另存的常见编码）')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, 'gbk.csv')
        _make_df().to_csv(p, index=False, encoding='gbk')
        df = read_table(p)
        check('行数 = 4', len(df) == 4, len(df))
        check('列名正确解码', '断面名称' not in df.columns and '站点名称' in df.columns,
              list(df.columns)[:4])

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, 'utf8bom.csv')
        _make_df().to_csv(p, index=False, encoding='utf_8_sig')
        df = read_table(p)
        check('UTF-8-BOM 也读得对', len(df) == 4 and df.columns[0] == '省份',
              (len(df), df.columns[0]))


def test_records_and_internal_header():
    print('\n[7] 生成记录 + 丢弃文件内混入的表头行')
    df = _make_df()
    # 在文件中段插入一行表头（原爬虫故障的同源问题）
    df = pd.concat([df.iloc[:2], pd.DataFrame([CSV_COLUMNS], columns=CSV_COLUMNS),
                    df.iloc[2:]], ignore_index=True)
    records, skipped_header, skipped_bad = frame_to_records(df, build_column_map(df))
    check('丢弃 1 行内嵌表头', skipped_header == 1, skipped_header)
    check('丢弃 0 行脏日期', skipped_bad == 0, skipped_bad)
    check('得到 4 条有效记录', len(records) == 4, len(records))
    names = [r['断面名称'] for r in records]
    check('记录里没有假断面', '断面名称' not in names, names)

    tn = [r for r in records if r['断面名称'] == '捞刀河口'][0]['总氮']
    check("捞刀河口 总氮 '--' -> ''", tn == '', repr(tn))
    tn2 = [r for r in records if r['断面名称'] == '桔子洲'][0]['总氮']
    check("桔子洲 '0.61 mg/L' -> '0.61'", tn2 == '0.61', repr(tn2))


def test_dry_run_writes_nothing():
    print('\n[8] --dry-run 不写任何数据')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, 'in.csv')
        _make_df().to_csv(p, index=False, encoding='utf_8_sig')
        out = os.path.join(td, 'out')
        rc = run(p, dry_run=True, write_db=False, processed_dir=out)
        check('返回码 0', rc == 0, rc)
        check('未创建输出目录内容', not os.path.exists(out) or not os.listdir(out),
              os.listdir(out) if os.path.exists(out) else 'not created')


def test_full_ingest_to_tempdir():
    print('\n[9] 完整导入（重定向到临时目录，不写库）')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, 'in.csv')
        _make_df().to_csv(p, index=False, encoding='utf_8_sig')
        out = os.path.join(td, 'out')
        rc = run(p, dry_run=False, write_db=False, processed_dir=out)
        check('返回码 0', rc == 0, rc)
        # 三角洲=st13 捞刀河口=st14 桔子洲=st12；黄河花园口应被忽略
        for sid in ('st12', 'st13', 'st14'):
            check('生成 %s_processed.csv' % sid,
                  os.path.exists(os.path.join(out, sid + '_processed.csv')))
        chk = sorted(os.listdir(out))
        check('不为非目标断面生成文件（无 st? 之外的产物）', len(chk) == 3, chk)
        df13 = pd.read_csv(os.path.join(out, 'st13_processed.csv'))
        check('st13 日期 = 2026-04-05', list(df13['DATE']) == ['2026-04-05'],
              list(df13['DATE']))
        check('st13 TN = 2.93', abs(float(df13['TN'].iloc[0]) - 2.93) < 1e-9,
              df13['TN'].iloc[0])
        df14 = pd.read_csv(os.path.join(out, 'st14_processed.csv'))
        check('st14 缺测 TN 为 NaN 而非 0', pd.isna(df14['TN'].iloc[0]),
              df14['TN'].iloc[0])


def main():
    print('=' * 66)
    print(' 通用数据导入 · 单测')
    print('=' * 66)
    test_column_alias_mapping()
    test_user_map_override()
    test_station_resolution()
    test_numeric_cleaning()
    test_datetime_normalization()
    test_gbk_csv_read()
    test_records_and_internal_header()
    test_dry_run_writes_nothing()
    test_full_ingest_to_tempdir()
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
