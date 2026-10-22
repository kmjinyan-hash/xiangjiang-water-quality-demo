# -*- coding: utf-8 -*-
"""模型推理：加载权重、按站点取最近 7 步数据、预测下游断面总氮（TN）。

安全设计（与旧实现的关键差别）
    旧实现把 sklearn 的 StandardScaler 对象 pickle 进了 .pth，于是只能
        torch.load(..., weights_only=False)
    而 weights_only=False 会执行 pickle 里的任意对象构造 —— 等价于
    「只要 .pth 被替换，就能在本机执行任意代码」。

    现在把缩放器拆成同目录的纯 JSON（config.META_PATH），权重文件里只剩张量，
    因此可以：
      * 用 weights_only=True 安全加载权重；
      * 运行时不再依赖 sklearn（也就没有了跨版本 InconsistentVersionWarning）。

    旧的 .pth 仍可加载（带告警），迁移用 scripts/migrate_checkpoint.py。
"""
import logging
import os

import numpy as np
import pandas as pd
import torch

from ..config import META_PATH, MODEL_PATH, station_csv_path
from .checkpoint import ScalerLite, load_meta, load_state_dict
# 轻量模型结构定义（唯一真源，不再需要把 backend/ 塞进 sys.path）
from .model_def import SequentialRiverTNModel

logger = logging.getLogger(__name__)

# Global caches
MODEL_CACHE = None
INPUT_SCALERS = None
TARGET_SCALERS = None
MODEL_CONFIG = None

# Station order must match the training code exactly
STATION_NAMES = ['ST0', 'ST1', 'ST2', 'ST3', 'ST4', 'ST5',
                 'ST6', 'ST7', 'ST8', 'ST10', 'ST11', 'ST12', 'ST13', 'ST14']
TARGET_STATIONS = ['ST10', 'ST11', 'ST12', 'ST13', 'ST14']
INPUT_FEATURES = ['TEM', 'PH', 'DO', 'TDS', 'NTU', 'CODMN', 'TP', 'TN']


def init_model():
    """加载模型（幂等）。

    优先走安全路径：权重 weights_only=True + 缩放器从 meta.json 读。
    只有 meta.json 缺失时才回退到旧格式，并明确告警。
    """
    global MODEL_CACHE, INPUT_SCALERS, TARGET_SCALERS, MODEL_CONFIG

    if MODEL_CACHE is not None:
        return

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError("Model file not found: %s" % MODEL_PATH)

    meta = load_meta()
    if meta is not None:
        MODEL_CONFIG, INPUT_SCALERS, TARGET_SCALERS, _extra = meta
        logger.info("Loading checkpoint (weights_only=True, meta=%s)...",
                    os.path.basename(META_PATH))
        checkpoint = load_state_dict()
    else:
        logger.warning(
            "未找到 %s，回退到旧格式加载（weights_only=False）。"
            "旧格式允许在 .pth 中执行任意代码，且依赖 sklearn；"
            "请执行 python scripts/migrate_checkpoint.py 迁移。",
            os.path.basename(META_PATH))
        checkpoint = torch.load(MODEL_PATH, map_location=torch.device('cpu'),
                                weights_only=False)
        MODEL_CONFIG = checkpoint['model_config']
        INPUT_SCALERS = {k: ScalerLite(sc.mean_, sc.scale_)
                         for k, sc in (checkpoint.get('input_scalers') or {}).items()}
        TARGET_SCALERS = {k: ScalerLite(sc.mean_, sc.scale_)
                          for k, sc in (checkpoint.get('target_scalers') or {}).items()}

    # Reconstruct the model with the exact same config used during training
    MODEL_CACHE = SequentialRiverTNModel(
        input_configs=MODEL_CONFIG['input_configs'],
        upstream_mapping=MODEL_CONFIG['upstream_mapping'],
        station_positions=MODEL_CONFIG['station_positions'],
        hidden_size=MODEL_CONFIG.get('hidden_size', 80),
        dropout=MODEL_CONFIG.get('dropout', 0.2)
    )

    MODEL_CACHE.load_state_dict(checkpoint['model_state_dict'])
    MODEL_CACHE.eval()
    logger.info("Model loaded successfully. hidden_size=%d",
                MODEL_CONFIG.get('hidden_size', 80))
    logger.info("  Input scalers loaded for %d stations.", len(INPUT_SCALERS))
    logger.info("  Target scalers loaded for %d stations.", len(TARGET_SCALERS))


def get_latest_data(time_steps=7):
    """
    Read the latest `time_steps` rows from each station's CSV,
    apply the corresponding input scaler, and return tensors.
    """
    inputs_tensor = {}
    actuals = {}
    last_dates = {}  # per-station last date dict

    for station in STATION_NAMES:
        csv_path = station_csv_path(station)
        if not os.path.exists(csv_path):
            logger.warning("CSV not found for station %s, skipping.", station)
            continue

        df = pd.read_csv(csv_path)

        # Replace non-numeric sentinels (e.g. '-') with NaN so float conversion succeeds
        for col in INPUT_FEATURES + ['TN']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Basic outlier filtering to match training code
        if 'TN' in df.columns:
            tn_mean = df['TN'].mean()
            tn_std = df['TN'].std()
            if tn_std > 0:
                z_scores = np.abs((df['TN'] - tn_mean) / tn_std)
                df.loc[z_scores > 3, 'TN'] = np.nan
        df = df.dropna(subset=['TN'])

        if len(df) == 0:
            continue

        # Take the last `time_steps` rows
        latest_df = df.tail(time_steps).copy()

        # Pad at the BEGINNING if fewer than time_steps rows (older data is missing)
        if len(latest_df) < time_steps:
            padding = pd.DataFrame([latest_df.iloc[0]] * (time_steps - len(latest_df)))
            latest_df = pd.concat([padding, latest_df], ignore_index=True)

        seq_data = latest_df[INPUT_FEATURES].values.astype(float)
        seq_data = np.nan_to_num(seq_data, nan=0.0)

        # Apply the scaler that was used during training
        if station in INPUT_SCALERS:
            seq_data = INPUT_SCALERS[station].transform(seq_data)

        inputs_tensor[station] = torch.FloatTensor(seq_data).unsqueeze(0)  # (1, 7, 8)

        if station in TARGET_STATIONS:
            actuals[station] = float(latest_df['TN'].iloc[-1])
            last_dates[station] = str(latest_df['DATE'].iloc[-1])

    return inputs_tensor, actuals, last_dates


def run_prediction():
    """
    Main entry point: load model if needed, read latest data, run inference,
    inverse-transform predictions, and return a dict keyed by station.
    """
    if MODEL_CACHE is None:
        init_model()

    inputs, actuals, last_dates = get_latest_data(time_steps=7)

    if not inputs:
        raise ValueError("No input data available for any station.")

    with torch.no_grad():
        predictions = MODEL_CACHE(inputs)  # (1, 5)

    predictions_np = predictions.numpy().flatten()

    results = {}
    for i, station in enumerate(TARGET_STATIONS):
        pred_val = float(predictions_np[i])

        # Inverse transform using the target scaler
        if station in TARGET_SCALERS:
            pred_unscaled = TARGET_SCALERS[station].inverse_transform([[pred_val]])
            pred_val = float(pred_unscaled[0][0])

        results[station] = {
            'predicted_TN': round(pred_val, 4),
            'actual_TN': round(actuals.get(station, 0.0), 4),
            'last_recorded_date': last_dates.get(station, "")
        }

    return results
