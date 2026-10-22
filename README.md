# 湘江流域水质智能预警平台

个人作品与研究应用展示，提供多断面地图、历史水质趋势、总氮模型推理、规则预警、报表，以及使用者自带大模型 API 的文字解读。界面无需登录。由于科研需要，目前上传的演示为最原始的UI界面，使用和改进可自行修改。

**仅供个人非商业学习与参考。** 包含前后端、模型定义、已训练权重、缩放参数、现有数据快照及地图资源；不包含训练源代码、密钥、账号数据库、日志及原内部仓库历史。研究仍在进行，未公开成果及其他权利由权利人保留。详见 [LICENSE](LICENSE)。

## 直接观看

[打开在线交互演示](https://kmjinyan-hash.github.io/xiangjiang-water-quality-demo/)

该链接在仓库的 GitHub Pages 部署完成后生效。Pages 设置选择 main 分支的 /docs 目录。

在线页使用合成数据与示例公式，供体验交互，不运行研究模型。完整应用和权重推理请在本机运行。GitHub Pages 仅托管静态网页，不能运行 Flask 后端。

## Docker 启动完整应用

安装并启动 Docker Desktop（Windows 使用 WSL 2 后端），执行：

```bash
git clone https://github.com/kmjinyan-hash/xiangjiang-water-quality-demo.git
cd xiangjiang-water-quality-demo
docker compose up -d --build
```

首次构建会下载 Python、PyTorch 等依赖，需要较长时间。启动后打开 **http://localhost:8080/**。基础图表和模型推理无需账号或大模型密钥。Windows 也可双击 `启动.bat`。

```bash
docker compose ps
docker compose logs --tail=100 water-quality
docker compose down
```

`down` 保留数据卷。若 8080 被占用，可修改 Compose 中 `127.0.0.1:8080:80` 的宿主端口。端口只绑定本机。网页由 Caddy 托管并反代至 Flask/Gunicorn；首次启动创建新的本机数据库。

## 自带大模型 API

点击右上角“大模型 API”，填写自己的 OpenAI 兼容服务地址、模型名称与密钥，保存后按需测试或生成解读。密钥仅在当前页面内存中保存，刷新后清除。

完整应用将该次请求的配置传给你本机的后端，由其连接指定服务；静态演示由浏览器直接连接，需要服务商支持 CORS。调用及费用由使用者与服务商结算。仓库无共享密钥。不要在提交、Issues、截图或地址栏中放入真实密钥。

## 模型与数据

- `src/water_quality/ml/model_def.py`：模型结构及前向传播定义。
- `src/water_quality/ml/inference.py`：推理流程。
- `models/sequential_river_tn_model.pth`：已有推理权重，使用 `weights_only=True` 加载。
- `models/sequential_river_tn_model.meta.json`：结构配置和输入/输出缩放参数。
- `data/processed/`：断面历史快照；`data/raw/`：站点坐标台账。
- `data/geo/`：边界及土地利用显示资源。

总氮推理使用多站点最近序列，输出 ST10—ST14 五个目标断面的结果。本仓库不提供训练程序及训练复现流程。快照不等于实时数据；应结合时间戳理解结果。历史评价指标不代表当前在线准确率。

所有结果仅供作品展示和个人参考，不作为水质等级认定、监管、安全或环境管理决策依据。合成演示结果与完整应用模型推理应分别理解。

## 目录与开发

| 目录 | 内容 |
| --- | --- |
| `frontend/` | 完整前端，无登录界面 |
| `src/water_quality/` | API、数据处理、预警、大模型适配、推理 |
| `models/`、`data/` | 权重、参数与配套数据 |
| `deploy/`、`scripts/` | 部署、构建及检查工具 |
| `tests/` | 功能与 API 配置验证 |
| `docs/demo/` | 无后端的合成数据网页演示 |
| `docs/` | 使用说明及历史技术资料，旧指令以本 README 为准 |
| `licenses/` | 第三方许可及 NOTICE |

Python 本地开发需 Python 3.12：

```bash
python -m venv .venv
# Windows：.venv\Scripts\Activate.ps1
# macOS / Linux：source .venv/bin/activate
pip install -e ".[excel,tz]"
python -m water_quality.app
```

后端默认为 `http://127.0.0.1:5000`。另开终端构建和托管前端：

```bash
python scripts/build_frontend.py --out dist --api-base http://127.0.0.1:5000
python -m http.server 8080 --bind 127.0.0.1 --directory dist
```

打开 `http://127.0.0.1:8080/`。采集源可能不可用；发布版关闭定时采集，使用已有快照。远程部署需自行配置 HTTPS、访问控制和来源限制。

## 许可与署名

Copyright © 2026 kmjinyan-hash and the project contributors.

原创应用、模型定义及权重按 [个人非商业参考许可](LICENSE) 提供。这是受限许可，不是 MIT/Apache 等开放源代码许可。允许个人非商业学习、查看和本机运行；商业使用、额外再分发和修改版公开传播需另获书面许可。法定及平台权利不受影响。

第三方代码适用其原许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。数据及地图资料保留相关权利人的权利，仓库公开不授予额外的第三方资料商业或再分发权利。合作或额外授权请通过 Issues 联系，不要提交密钥或未公开科研资料。
