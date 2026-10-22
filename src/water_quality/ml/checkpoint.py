# -*- coding: utf-8 -*-
"""checkpoint.py — 推理模型存档的安全读取

存档格式（两个文件，同在 models/ 下）
    <name>.pth          {'model_state_dict': {...}}    ← 纯张量，weights_only=True 可加载
    <name>.meta.json    {'model_config', 'input_scalers', 'target_scalers', 'extra'}

为什么必须拆成两个文件
    旧实现把 sklearn 的 StandardScaler 对象 pickle 进了 .pth，于是加载端只能写
        torch.load(path, weights_only=False)
    而 weights_only=False 会执行 pickle 里的任意对象构造 —— 等价于
    「只要 .pth 被替换，就能在本机执行任意代码」。模型文件在容器与流水线里
    往往是从外部挂载/下载的，这个攻击面必须关掉。

    StandardScaler 真正需要的只有 mean_ 与 scale_ 两个数组。用 ScalerLite
    以 numpy 复刻后即可写成 JSON，顺带把运行时对 sklearn 的依赖也去掉了
    （原先还每次启动都报 InconsistentVersionWarning）。
"""
import json
import os

import numpy as np
import torch

from ..config import META_PATH, MODEL_PATH


class ScalerLite:
    """用 numpy 复刻 sklearn StandardScaler 的 transform / inverse_transform。

    算术与 sklearn 完全一致：z = (x - mean_) / scale_，反变换 x = z * scale_ + mean_。
    只保留两个数组，因此可以直接序列化成 JSON。
    """

    __slots__ = ('mean_', 'scale_')

    def __init__(self, mean, scale):
        self.mean_ = np.asarray(mean, dtype=np.float64)
        self.scale_ = np.asarray(scale, dtype=np.float64)
        # sklearn 在方差为 0 时把 scale_ 置 1；这里再兜一层，避免除零
        self.scale_ = np.where(self.scale_ == 0, 1.0, self.scale_)

    def transform(self, X):
        return (np.asarray(X, dtype=np.float64) - self.mean_) / self.scale_

    def inverse_transform(self, X):
        return np.asarray(X, dtype=np.float64) * self.scale_ + self.mean_

    def to_json(self):
        return {'mean_': self.mean_.tolist(), 'scale_': self.scale_.tolist()}

    def __repr__(self):
        return 'ScalerLite(n_features=%d)' % self.mean_.size


def load_meta(meta_path=None):
    """读取元数据。返回 (model_config, input_scalers, target_scalers, extra)；缺失返回 None。"""
    meta_path = meta_path or META_PATH
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    scalers_in = {k: ScalerLite(v['mean_'], v['scale_'])
                  for k, v in (meta.get('input_scalers') or {}).items()}
    scalers_tg = {k: ScalerLite(v['mean_'], v['scale_'])
                  for k, v in (meta.get('target_scalers') or {}).items()}
    return meta.get('model_config'), scalers_in, scalers_tg, (meta.get('extra') or {})


def load_state_dict(pth_path=None):
    """用 weights_only=True 安全加载权重（不执行 pickle 里的任意对象构造）。"""
    pth_path = pth_path or MODEL_PATH
    return torch.load(pth_path, map_location=torch.device('cpu'), weights_only=True)
