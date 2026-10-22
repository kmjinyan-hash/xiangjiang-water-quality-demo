"""
模型类定义模块 — 独立于训练代码，专供推理导入使用。

包含:
  - PositionalEncoding: 站点位置编码
  - CumulativeAttention: 累积注意力机制
  - SequentialRiverTNModel: 顺序河流 TN 预测模型
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np


class PositionalEncoding(nn.Module):
    """位置编码 - 编码站点在河流中的位置"""

    def __init__(self, hidden_size, max_positions=20):
        super(PositionalEncoding, self).__init__()
        self.hidden_size = hidden_size

        pe = torch.zeros(max_positions, hidden_size)
        position = torch.arange(0, max_positions, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, hidden_size, 2).float() *
                             (-np.log(10000.0) / hidden_size))

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)

        self.register_buffer('pe', pe)

    def forward(self, position_ids):
        return self.pe[position_ids]


class CumulativeAttention(nn.Module):
    """累积注意力机制 - 考虑距离和上下游关系"""

    def __init__(self, hidden_size):
        super(CumulativeAttention, self).__init__()

        self.hidden_size = hidden_size

        self.query_proj = nn.Linear(hidden_size, hidden_size)
        self.key_proj = nn.Linear(hidden_size, hidden_size)
        self.value_proj = nn.Linear(hidden_size, hidden_size)

        self.distance_embedding = nn.Embedding(20, hidden_size // 4)

        self.out_proj = nn.Linear(hidden_size, hidden_size)
        self.layer_norm = nn.LayerNorm(hidden_size)

    def forward(self, target_features, upstream_features, distances):
        if len(upstream_features) == 0:
            return target_features

        batch_size = target_features.size(0)
        device = target_features.device

        upstream_stack = torch.stack(upstream_features, dim=1)

        query = self.query_proj(target_features).unsqueeze(1)
        keys = self.key_proj(upstream_stack)
        values = self.value_proj(upstream_stack)

        scores = torch.matmul(query, keys.transpose(-2, -1)) / np.sqrt(self.hidden_size)

        if distances:
            distance_tensor = torch.tensor(distances, dtype=torch.long).to(device)
            distance_embeds = self.distance_embedding(distance_tensor)

            distance_weights = 1.0 / (1.0 + torch.log1p(distance_tensor.float()))
            distance_weights = distance_weights.unsqueeze(0).unsqueeze(0)
            scores = scores * distance_weights

        attn_weights = F.softmax(scores, dim=-1)
        attended = torch.matmul(attn_weights, values).squeeze(1)

        output = self.out_proj(attended)
        output = self.layer_norm(output + target_features)

        return output


class SequentialRiverTNModel(nn.Module):
    """顺序河流TN预测模型 - 考虑累积上游影响"""

    def __init__(self, input_configs, upstream_mapping, station_positions,
                 hidden_size=64, dropout=0.2):
        super(SequentialRiverTNModel, self).__init__()

        self.hidden_size = hidden_size
        self.input_configs = input_configs
        self.upstream_mapping = upstream_mapping
        self.station_positions = station_positions

        self.all_stations = sorted(station_positions.keys(), key=lambda x: station_positions[x])
        self.target_stations = ['ST10', 'ST11', 'ST12', 'ST13', 'ST14']

        self.station_encoders = nn.ModuleDict()
        for station, input_size in input_configs.items():
            self.station_encoders[station] = nn.LSTM(
                input_size,
                hidden_size,
                num_layers=2,
                batch_first=True,
                dropout=dropout,
                bidirectional=True
            )

        self.position_encoding = PositionalEncoding(hidden_size * 2)
        self.cumulative_attention = CumulativeAttention(hidden_size * 2)

        self.st14_selective_attention = nn.MultiheadAttention(
            embed_dim=hidden_size * 2,
            num_heads=8,
            dropout=dropout
        )

        self.st14_compressor = nn.Sequential(
            nn.Linear(hidden_size * 2, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size)
        )

        self.flow_gru = nn.GRU(
            hidden_size * 2,
            hidden_size,
            num_layers=2,
            batch_first=True,
            dropout=dropout,
            bidirectional=True
        )

        self.station_processors = nn.ModuleDict()
        for station in self.target_stations:
            if station == 'ST14':
                self.station_processors[station] = nn.Sequential(
                    nn.Linear(hidden_size * 5, hidden_size * 3),
                    nn.LayerNorm(hidden_size * 3),
                    nn.ReLU(),
                    nn.Dropout(dropout),
                    nn.Linear(hidden_size * 3, hidden_size * 2),
                    nn.LayerNorm(hidden_size * 2),
                    nn.ReLU(),
                    nn.Dropout(dropout * 0.7),
                    nn.Linear(hidden_size * 2, hidden_size),
                    nn.LayerNorm(hidden_size),
                    nn.ReLU()
                )
            else:
                self.station_processors[station] = nn.Sequential(
                    nn.Linear(hidden_size * 4, hidden_size * 2),
                    nn.LayerNorm(hidden_size * 2),
                    nn.ReLU(),
                    nn.Dropout(dropout),
                    nn.Linear(hidden_size * 2, hidden_size),
                    nn.LayerNorm(hidden_size),
                    nn.ReLU(),
                    nn.Dropout(dropout * 0.5)
                )

        self.predictors = nn.ModuleDict()
        for station in self.target_stations:
            if station == 'ST14':
                self.predictors[station] = nn.Sequential(
                    nn.Linear(hidden_size, hidden_size),
                    nn.LayerNorm(hidden_size),
                    nn.ReLU(),
                    nn.Dropout(dropout * 0.5),
                    nn.Linear(hidden_size, hidden_size // 2),
                    nn.ReLU(),
                    nn.Linear(hidden_size // 2, 1)
                )
            else:
                self.predictors[station] = nn.Sequential(
                    nn.Linear(hidden_size, hidden_size // 2),
                    nn.ReLU(),
                    nn.Linear(hidden_size // 2, 1)
                )

        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, (nn.LSTM, nn.GRU)):
            for name, param in module.named_parameters():
                if 'weight' in name:
                    nn.init.xavier_uniform_(param)
                elif 'bias' in name:
                    nn.init.zeros_(param)

    def encode_station(self, station, inputs):
        if station in inputs and station in self.station_encoders:
            lstm_out, _ = self.station_encoders[station](inputs[station])
            features = lstm_out[:, -1, :]

            position = self.station_positions[station]
            batch_size = features.size(0)
            position_ids = torch.full((batch_size,), position, dtype=torch.long).to(features.device)
            pos_encoding = self.position_encoding(position_ids)

            features = features + 0.1 * pos_encoding

            return features
        return None

    def forward(self, inputs):
        batch_size = next(iter(inputs.values())).size(0)
        device = next(iter(inputs.values())).device

        station_features = {}
        for station in self.all_stations:
            features = self.encode_station(station, inputs)
            if features is not None:
                station_features[station] = features

        predictions = []

        for target_station in self.target_stations:
            if target_station in station_features:
                target_features = station_features[target_station]
            else:
                target_features = torch.zeros(batch_size, self.hidden_size * 2).to(device)

            upstream_stations = self.upstream_mapping[target_station]
            upstream_features = []
            distances = []

            for upstream_station in upstream_stations:
                if upstream_station in station_features:
                    upstream_features.append(station_features[upstream_station])
                    distance = self.station_positions[target_station] - self.station_positions[upstream_station]
                    distances.append(distance)

            if target_station == 'ST14' and upstream_features:
                if len(upstream_features) > 8:
                    sorted_indices = sorted(range(len(distances)), key=lambda i: distances[i])[:8]
                    selected_features = [upstream_features[i] for i in sorted_indices]
                    selected_distances = [distances[i] for i in sorted_indices]
                else:
                    selected_features = upstream_features
                    selected_distances = distances

                upstream_stack = torch.stack(selected_features, dim=1)

                upstream_stack_t = upstream_stack.transpose(0, 1)
                target_query = target_features.unsqueeze(0)

                attended_upstream, attn_weights = self.st14_selective_attention(
                    target_query, upstream_stack_t, upstream_stack_t
                )
                attended_upstream = attended_upstream.squeeze(0)

                compressed_upstream = self.st14_compressor(attended_upstream)

                recent_stations = upstream_stations[-5:]
                flow_sequence = []
                for station in recent_stations:
                    if station in station_features:
                        flow_sequence.append(station_features[station])
                flow_sequence.append(target_features)

                if flow_sequence:
                    flow_tensor = torch.stack(flow_sequence, dim=1)
                    flow_output, _ = self.flow_gru(flow_tensor)
                    flow_features = flow_output[:, -1, :]
                else:
                    flow_features = torch.zeros(batch_size, self.hidden_size * 2).to(device)

                combined_features = torch.cat([target_features, flow_features, compressed_upstream], dim=-1)

            else:
                if upstream_features:
                    cumulative_features = self.cumulative_attention(
                        target_features, upstream_features, distances
                    )

                    flow_sequence = []
                    for station in self.all_stations:
                        if self.station_positions[station] <= self.station_positions[target_station]:
                            if station in station_features:
                                flow_sequence.append(station_features[station])

                    if flow_sequence:
                        flow_tensor = torch.stack(flow_sequence, dim=1)
                        flow_output, _ = self.flow_gru(flow_tensor)
                        flow_features = flow_output[:, -1, :]
                    else:
                        flow_features = torch.zeros(batch_size, self.hidden_size * 2).to(device)

                    combined_features = torch.cat([target_features, flow_features], dim=-1)
                else:
                    combined_features = torch.cat([target_features, target_features], dim=-1)

            processed_features = self.station_processors[target_station](combined_features)
            prediction = self.predictors[target_station](processed_features)
            predictions.append(prediction)

        predictions = torch.cat(predictions, dim=1)

        return predictions
