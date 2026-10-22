# -*- coding: utf-8 -*-
"""
clean_raw_data.py — 数据清理（raw_data 脏数据 + 清理无关数据 + 废弃的 users 表）

背景（2026-10 现场取证，不是推测）
    raw_data 里那批 1397 行是旧版爬虫留下的，确认有三类缺陷：
      1. 表头行入库 —— 1 行 station_name='断面名称' / measure_time='监测时间'。
      2. 时间没有年份 —— 1396 行是 'MM-DD HH:MM'（如 '04-05 19:00'）。
      3. 省份列不可信 —— 50 个断面同时挂在两个省下（上海市与青海省共享
         「丁家柳河桥」「万泰」；安徽省与湖南省共享「东关桥」），
         省份列记录的是**遍历游标**，不是断面所属省份。

处理原则（只清「确定错误」的，不删真实测量值）
    * 表头行**确定不是数据** → 删除。
    * 年份**可以确定** → 用与在线采集同一套规则补年。
    * 省份**无法确定** → 置空；仅 14 个湘江断面有确定事实（均在湖南）→ 回填湖南省。

上面这三项是**默认行为**（幂等，重复运行报告 0 项）。另外两个开关默认**关闭**，
需要显式指定：

    --purge-non-target
        删除非 14 个目标断面的 raw_data 行。本项目只关心湘江流域那 14 个断面，
        其余是全国其它流域的断面数据，属**无关数据**。删除前先归档。
    --drop-users
        删除 users 表。登录鉴权需求已被负责人否决（见 PRD 顶部变更记录），
        该表不再被任何代码引用。删除前先归档。
        注意：新库本来就不会再创建这张表，本开关是给**老库**用的。

归档一律写到 var/backups/（已在 .gitignore 里排除 —— 归档可能含账号哈希，
不该进版本库）。

用法
    python scripts/clean_raw_data.py                        # 只报告（默认 dry-run）
    python scripts/clean_raw_data.py --apply                # 只做三项脏数据修复
    python scripts/clean_raw_data.py --apply --purge-non-target --drop-users   # 全清
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from water_quality.config import BACKUP_DIR, DB_PATH                   # noqa: E402
from water_quality.pipeline.clean import STATION_MAP                   # noqa: E402
from water_quality.pipeline.crawler import (                            # noqa: E402
    TARGET_HEADERS, normalize_measure_time,
)

# 14 个湘江流域断面（中文名），事实依据：STATION_MAP 里全都属湖南境内
TARGET_NAMES = list(STATION_MAP.keys())
HUNAN = '湖南省'

# 表头字面量：只要 station_name 或 measure_time 撞上这些，就不是数据行
HEADER_LITERALS = sorted(set(TARGET_HEADERS) | {'断面名称', '站点名称', '监测时间', '省份', '流域'})

_FULL_TS = None      # 延迟编译


def _is_full_timestamp(s):
    """判断是否已是 'YYYY-MM-DD HH:MM' 完整时间戳。"""
    global _FULL_TS
    if _FULL_TS is None:
        import re
        _FULL_TS = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
    return bool(_FULL_TS.match((s or '').strip()))


def _ph(n):
    return ','.join('?' * n)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()[0] > 0


def survey(conn):
    """统计待处理项（只读，不改数据）。"""
    c = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM raw_data").fetchone()[0]

    hl, tgt = _ph(len(HEADER_LITERALS)), _ph(len(TARGET_NAMES))
    header_rows = c.execute(
        "SELECT rowid, station_name, measure_time FROM raw_data "
        "WHERE station_name IN (%s) OR measure_time IN (%s)" % (hl, hl),
        tuple(HEADER_LITERALS) * 2).fetchall()

    times = c.execute("SELECT rowid, measure_time FROM raw_data").fetchall()
    no_year = [(r[0], r[1]) for r in times if not _is_full_timestamp(r[1])]

    tgt_wrong = c.execute(
        "SELECT station_name, province FROM raw_data WHERE station_name IN (%s) "
        "AND province IS NOT NULL AND province <> ?" % tgt, tuple(TARGET_NAMES) + (HUNAN,)).fetchall()

    other_prov = c.execute(
        "SELECT COUNT(*) FROM raw_data WHERE station_name NOT IN (%s) "
        "AND province IS NOT NULL AND province <> ''" % tgt, tuple(TARGET_NAMES)).fetchone()[0]

    non_target = c.execute(
        "SELECT COUNT(*) FROM raw_data WHERE station_name NOT IN (%s)" % tgt,
        tuple(TARGET_NAMES)).fetchone()[0]

    users_rows = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] if _table_exists(conn, 'users') else None

    return {
        'total': total,
        'header_rows': header_rows,
        'no_year': no_year,
        'tgt_wrong': tgt_wrong,
        'other_prov': other_prov,
        'non_target': non_target,
        'users_rows': users_rows,
    }


def report(s):
    print('=' * 70)
    print(' 数据清理计划（dry-run，尚未改动任何数据）')
    print('=' * 70)
    print('  数据库      : %s' % DB_PATH)
    print('  归档目录    : %s' % BACKUP_DIR)
    print('  当前总行数  : %d' % s['total'])
    print()
    print('  ── 默认执行：三类确定错误的脏数据 ──')
    print('  [1] 表头行（确定不是数据）→ 删除：%d 行' % len(s['header_rows']))
    for r in s['header_rows'][:5]:
        print('        rowid=%s  station_name=%r  measure_time=%r' % (r[0], r[1], r[2]))
    print('  [2] 时间无年份 → 补全：%d 行   例 %r -> %r'
          % (len(s['no_year']),
             s['no_year'][0][1] if s['no_year'] else '', 
             normalize_measure_time(s['no_year'][0][1]) if s['no_year'] else ''))
    print('  [3] 不可信省份 → 置空：%d 行；湘江断面回填 %s：%d 行'
          % (s['other_prov'], HUNAN, len(s['tgt_wrong'])))
    for name, prov in s['tgt_wrong'][:8]:
        print('        %-8s %r  ->  %r' % (name, prov, HUNAN))
    print()
    print('  ── 需要显式开关 ──')
    print('  [4] --purge-non-target  删除非 14 个目标断面的行：%d 行' % s['non_target'])
    if s['non_target']:
        print('        保留的 14 个断面：%s' % '、'.join(TARGET_NAMES))
    if s['users_rows'] is None:
        print('  [5] --drop-users        users 表不存在，无需处理')
    else:
        print('  [5] --drop-users        DROP TABLE users（当前 %d 行）' % s['users_rows'])
    print()
    print('  说明：删除前都会先归档到 %s，不会真的丢数据。' % BACKUP_DIR)


def _archive(conn, table, where='', params=(), label=''):
    """把表的（子集）导出为 CSV 归档，返回归档路径。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    path = os.path.join(BACKUP_DIR, '%s_%s%s.csv' % (table, label + '_' if label else '', stamp))
    sql = 'SELECT * FROM %s%s' % (table, (' WHERE ' + where) if where else '')
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    import csv
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(cur.fetchall())
    return path


def apply_changes(conn, purge_non_target=False, drop_users=False, backup=True):
    s = survey(conn)

    work = bool(s['header_rows'] or s['no_year'] or s['tgt_wrong'] or s['other_prov'])
    if purge_non_target and s['non_target']:
        work = True
    if drop_users and s['users_rows'] is not None:
        work = True
    if not work:
        print('\n没有需要清理的内容（脚本是幂等的，可重复运行）。')
        return 0

    if backup:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        db_bak = os.path.join(BACKUP_DIR, 'app.db.bak-%s' % stamp)
        shutil.copy2(DB_PATH, db_bak)
        print('\n已备份数据库：%s' % db_bak)

    c = conn.cursor()
    hl, tgt = _ph(len(HEADER_LITERALS)), _ph(len(TARGET_NAMES))

    # 1) 删除表头行
    c.execute("DELETE FROM raw_data WHERE station_name IN (%s) OR measure_time IN (%s)" % (hl, hl),
              tuple(HEADER_LITERALS) * 2)
    n_header = c.rowcount

    # 2) 补全年份（与在线采集同一套规则）
    n_year = 0
    for rowid, t in s['no_year']:
        fixed = normalize_measure_time(t)
        if fixed != t:
            c.execute("UPDATE raw_data SET measure_time=? WHERE rowid=?", (fixed, rowid))
            n_year += c.rowcount

    # 3) 省份：湘江断面回填湖南省，其余置空
    c.execute("UPDATE raw_data SET province=? WHERE station_name IN (%s) AND province IS NOT ?"
              % tgt, (HUNAN,) + tuple(TARGET_NAMES) + (HUNAN,))
    n_tgt = c.rowcount
    c.execute("UPDATE raw_data SET province='' WHERE station_name NOT IN (%s) AND province IS NOT ''"
              % tgt, tuple(TARGET_NAMES))
    n_other = c.rowcount

    conn.commit()
    print('\n清理完成：')
    print('  删除表头行        : %d' % n_header)
    print('  补全时间戳        : %d' % n_year)
    print('  回填湘江断面省份   : %d' % n_tgt)
    print('  置空不可信省份     : %d' % n_other)

    # 4) 清理无关数据（非目标断面）
    if purge_non_target and s['non_target']:
        arch = _archive(conn, 'raw_data', 'station_name NOT IN (%s)' % tgt,
                        tuple(TARGET_NAMES), label='non_target')
        c.execute("DELETE FROM raw_data WHERE station_name NOT IN (%s)" % tgt, tuple(TARGET_NAMES))
        n_purged = c.rowcount
        conn.commit()
        print('  删除无关断面行     : %d   （已归档 %s）' % (n_purged, arch))
    elif s['non_target']:
        print('  保留无关断面行     : %d   （需要时加 --purge-non-target）' % s['non_target'])

    # 5) 丢弃废弃的 users 表
    if drop_users and s['users_rows'] is not None:
        arch = _archive(conn, 'users', label='')
        c.execute('DROP TABLE users')
        conn.commit()
        print('  删除 users 表      : %d 行账号  （已归档 %s）' % (s['users_rows'], arch))
    elif s['users_rows'] is not None:
        print('  users 表           : %d 行，保留（需要时加 --drop-users）' % s['users_rows'])

    left = conn.execute('SELECT COUNT(*) FROM raw_data').fetchone()[0]
    print('\n  raw_data 剩余行数  : %d' % left)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='清理 raw_data 脏数据、无关数据与废弃的 users 表')
    ap.add_argument('--apply', action='store_true', help='真正执行（默认只 dry-run）')
    ap.add_argument('--purge-non-target', action='store_true',
                    help='删除非 14 个目标断面的 raw_data 行（先归档）')
    ap.add_argument('--drop-users', action='store_true',
                    help='DROP 掉已废弃的 users 表（先归档）')
    ap.add_argument('--no-backup', action='store_true', help='执行时不备份数据库（不推荐）')
    args = ap.parse_args(argv)

    if not os.path.exists(DB_PATH):
        print('找不到数据库：%s' % DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        s = survey(conn)
        report(s, args)
        if not args.apply:
            print()
            print('=' * 70)
            print(' 以上为 dry-run。执行：')
            print('   python scripts/clean_raw_data.py --apply')
            if s['non_target'] or s['users_rows'] is not None:
                print('   python scripts/clean_raw_data.py --apply --purge-non-target --drop-users')
            print('=' * 70)
            return 0
        return apply_changes(conn, purge_non_target=args.purge_non_target,
                             drop_users=args.drop_users, backup=not args.no_backup)
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
