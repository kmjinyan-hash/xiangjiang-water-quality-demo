# -*- coding: utf-8 -*-
"""
clean.py — 采集结果入库 + 清洗为模型输入

职责：
    1. 把采集到的原始记录写入 SQLite 的 raw_data 表（数据湖，全量囤）；
    2. 按 STATION_MAP 把中文断面名过滤出 14 个目标断面，中文表头映射为英文特征，
       合并进 data/processed/stX_processed.csv（去重、按日期排序）。

「入库 + 清洗」被抽成 ingest_records()，由两条路径共用：
    * run_daily_pipeline()            —— 每日定时：爬虫在线采集后调用
    * crawler --from-file --ingest    —— 离线：用人工保存的页面 HTML 入库

这样即使目标站点暂时不可达，也能通过「浏览器另存页面 -> 离线入库」把数据链路走通。
"""
import os
import logging
import pandas as pd

from ..config import PROCESSED_DIR, ensure_runtime_dirs
from ..db import insert_raw_data

logger = logging.getLogger(__name__)

# 容错导入：selenium / webdriver-manager 缺失时平台仍能启动，仅后台采集不可用
_get_yangtze_fn = None
try:
    from .crawler import get_yangtze_final_mission as _get_yangtze_fn
except ImportError as _e:                                    # pragma: no cover
    logger.warning("Crawler unavailable (%s). 平台仍可启动，仅后台采集不可用。", _e)

# 断面中文名 -> 内部 STx 编号
STATION_MAP = {
    "绿埠头": "st0", "诸葛庙": "st1", "归阳镇": "st2",
    "舂陵水": "st3", "蒸水": "st4", "耒水": "st5",
    "洣水": "st6", "熬洲": "st7", "渌水": "st8",
    "涟水": "st10", "昭山": "st11", "桔子洲": "st12",
    "三角洲": "st13", "捞刀河口": "st14",
}

# 中文表头 -> 英文模型特征
COLUMN_MAP = {
    '监测时间': 'DATE',
    '水温': 'TEM',
    'pH': 'PH',
    '溶解氧': 'DO',
    '电导率': 'TDS',
    '浊度': 'NTU',
    '高锰酸盐': 'CODMN',
    '总磷': 'TP',
    '总氮': 'TN',
}

MODEL_FEATURES = ['DATE', 'TEM', 'PH', 'DO', 'TDS', 'NTU', 'CODMN', 'TP', 'TN']


def ingest_records(records, processed_dir=None, write_db=True):
    """把记录写进数据湖，并刷新目标断面的模型输入 CSV。

    参数:
        records       : 记录列表（键为中文表头，与 db.insert_raw_data 对齐）
        processed_dir : CSV 输出目录，默认 config.PROCESSED_DIR（测试时可重定向）
        write_db      : 是否写入 raw_data 表（测试时可关闭）

    返回统计字典 {'records','target_matches','inserted','updated_csv'}。
    """
    stats = {'records': 0, 'target_matches': 0, 'inserted': 0, 'updated_csv': 0}
    if not records:
        logger.warning("ingest_records: 记录为空，未做任何写入")
        return stats

    stats['records'] = len(records)
    matched = sorted({r.get('断面名称', '') for r in records
                      if r.get('断面名称', '') in STATION_MAP})
    stats['target_matches'] = len(
        [r for r in records if r.get('断面名称', '') in STATION_MAP])
    logger.info("待入库 %d 条；命中 14 个目标断面 %d 条（%s）",
                len(records), stats['target_matches'],
                "、".join(matched) if matched else "无")

    # ── 入库（数据湖：全量囤，重复行由 UNIQUE 约束忽略）──
    if write_db:
        stats['inserted'] = insert_raw_data(records)
        logger.info("raw_data 新增 %d 行", stats['inserted'])

    df = pd.DataFrame(records)
    if '断面名称' not in df.columns:
        logger.error("记录里缺少『断面名称』列，无法清洗")
        return stats

    data_dir = processed_dir or PROCESSED_DIR
    ensure_runtime_dirs()
    os.makedirs(data_dir, exist_ok=True)

    for cn_name, st_id in STATION_MAP.items():
        station_df = df[df['断面名称'] == cn_name].copy()
        if station_df.empty:
            continue

        station_df = station_df.rename(columns=COLUMN_MAP)

        # 缺失的指标列补 NaN —— 旧实现补 '0.0'，等于把「缺测」伪造成「实测 0」
        for col in MODEL_FEATURES:
            if col not in station_df.columns:
                station_df[col] = float('nan')

        station_df = station_df[MODEL_FEATURES]

        for col in MODEL_FEATURES:
            if col != 'DATE':
                station_df[col] = pd.to_numeric(station_df[col], errors='coerce')

        # DATE 归一化为 YYYY-MM-DD；解析不出来的行**直接丢弃**。
        # 旧实现用「今天」兜底，等于凭空造出当天的水质数据，会污染模型输入。
        # format='mixed' 同时消除了 pandas 3.0 的 Could not infer format 告警。
        station_df['DATE'] = pd.to_datetime(
            station_df['DATE'], errors='coerce', format='mixed').dt.strftime('%Y-%m-%d')
        bad = int(station_df['DATE'].isna().sum())
        if bad:
            logger.warning("%s(%s): 丢弃 %d 行无法解析日期的记录", st_id, cn_name, bad)
            station_df = station_df.dropna(subset=['DATE'])
        if station_df.empty:
            logger.warning("%s(%s): 本批无有效记录，跳过", st_id, cn_name)
            continue

        file_path = os.path.join(data_dir, "%s_processed.csv" % st_id)
        try:
            if os.path.exists(file_path):
                existing_df = pd.read_csv(file_path)
                # 旧 CSV 里的脏值也一并规整，避免历史脏数据反复被沿用
                for col in MODEL_FEATURES:
                    if col != 'DATE' and col in existing_df.columns:
                        existing_df[col] = pd.to_numeric(existing_df[col], errors='coerce')
                combined_df = pd.concat([existing_df, station_df], ignore_index=True)
            else:
                combined_df = station_df

            combined_df = combined_df.drop_duplicates(subset=['DATE'], keep='last')
            combined_df = combined_df.sort_values(by='DATE')

            # 原子写：先落临时文件再 os.replace。
            # 直接 to_csv 覆盖同一个路径有两个问题：
            #   1) 并发读脏 —— 调度器/ingest 正在写时，inference.get_latest_data()
            #      与 app.py 的 /api/history、/api/data_status 都可能 pd.read_csv 到
            #      只写了一半的文件，得到截断的序列（模型输入错、或直接抛解析异常）；
            #   2) 崩溃留残 —— 写到一半进程挂掉，原地留下一个损坏的 CSV，
            #      而这个文件正是模型输入，属于静默污染。
            # os.replace 在同一文件系统上是原子的：读者要么看到完整旧文件、
            # 要么看到完整新文件，不存在中间态。
            tmp_path = file_path + '.tmp'
            combined_df.to_csv(tmp_path, index=False, encoding='utf_8_sig')
            os.replace(tmp_path, file_path)
            stats['updated_csv'] += 1
            logger.info("%s(%s): 写入 %d 行，累计 %d 行，最新 %s",
                        st_id, cn_name, len(station_df), len(combined_df),
                        combined_df['DATE'].iloc[-1])
        except Exception as e:
            logger.error("更新 %s(%s) 的 CSV 失败: %s", st_id, cn_name, e)

    logger.info("清洗完成：刷新 %d 个断面 CSV（目录 %s）", stats['updated_csv'], data_dir)
    return stats


def run_daily_pipeline():
    """每日流水线：爬虫采集 -> 入库 -> 刷新模型输入 CSV。"""
    logger.info("Starting daily pipeline...")

    if _get_yangtze_fn is None:
        logger.error("爬虫模块不可用，流水线终止")
        return False

    try:
        final_data = _get_yangtze_fn()
    except Exception as e:
        logger.error("爬虫执行失败: %s", e, exc_info=True)
        return False

    if not final_data:
        logger.error("采集结果为 0 条，流水线终止（不做任何写入）。"
                     "请先执行 `python -m water_quality.pipeline.crawler --check` 确认采集链路。")
        return False

    logger.info("采集到 %d 条记录，样本: station=%s, time=%s, province=%s",
                len(final_data), final_data[0].get('断面名称'),
                final_data[0].get('监测时间'), final_data[0].get('省份'))

    stats = ingest_records(final_data)
    logger.info("Pipeline complete. %s", stats)
    return stats['updated_csv'] > 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_daily_pipeline()
