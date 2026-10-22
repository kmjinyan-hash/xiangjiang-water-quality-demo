# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────
# 湘江流域水质智能预警平台 · 生产镜像
#
# 构建：
#     docker build -t water-quality:0.9.0 .
#
# 运行（推荐：data 与日志挂卷，容器可随时重建）：
#     docker run -d --name wq -p 5000:5000 \
#       -v wq-data:/app/data -v wq-logs:/app/var/logs \
#       -e WQ_TZ=Asia/Shanghai \
#       water-quality:0.9.0
#
# 纯演示（用镜像内自带的 CSV 快照，不挂卷）：
#     docker run --rm -p 5000:5000 water-quality:0.9.0
#     然后浏览器打开 http://127.0.0.1:5000/
#
# 详见 docs/容器化说明.md
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/app/src \
    WQ_ROOT=/app \
    WQ_HOST=0.0.0.0 \
    WQ_PORT=5000 \
    WQ_TZ=Asia/Shanghai \
    WQ_ENABLE_SCHEDULER=0
# 让进程与日志时间戳也用业务时区。只设 WQ_TZ 的话，日志会打 UTC 而业务算 +8，
# 看日志时会把 21:00 的事读成 13:00。（config.py 里 WQ_TZ 优先，其次 TZ。）
ENV TZ=Asia/Shanghai

WORKDIR /app

# 依赖层单独缓存：改代码不必重装依赖
COPY requirements.txt ./

# torch 优先装 CPU-only 版本。默认 PyPI 的 torch wheel 会带上整套 CUDA 运行时，
# 镜像会从数百 MB 涨到数 GB，而本项目只做 CPU 前向推理，完全用不到。
# 若该 CPU 索引暂时没有钉住的版本，回退到 PyPI（镜像大一些但能构建成功）。
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 \
 || pip install --no-cache-dir torch==2.14.1

RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir 'gunicorn>=23.0.0' tzdata
# openpyxl 打进镜像：容器是本项目**推荐的取数入口**
# （docker cp 传文件 → python -m water_quality.pipeline.ingest），
# 对接人给的常常是 .xlsx 而不是 CSV。它很小，不值得为了省这点体积
# 让用户在容器里再装一次。本地开发装 [excel] extra 即可。
RUN pip install --no-cache-dir openpyxl

# ── 爬虫用的浏览器驱动 ──────────────────────────────────────────
# 数据源页面是 JS 渲染的（省份下拉 + iframe + 滚动加载），纯 urllib 抓不到表格，
# 所以自动采集必须有一个真浏览器。之前镜像里没有 selenium/chromedriver，
# 导致「数据源恢复后自动采集」这条链路在容器里必然 ModuleNotFoundError ——
# 而且因为站点当前不可达，这个问题一直被"预检失败"挡在后面看不出来。
#
# 只装 chromium-driver：该包本身把 chromium 作为硬依赖带入（没有浏览器光有驱动没用），
# 不再额外声明 chromium 与字体包，尽量少占体积。
#
# crawler._make_driver() 已内置 --no-sandbox / --disable-dev-shm-usage，适配容器。
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium-driver \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir selenium
ENV WQ_CHROMEDRIVER=/usr/bin/chromedriver

# 只拷贝运行所需内容。.dockerignore 已排除 .git / 日志 / 数据库 / 构建产物。
# models/ 烘焙进镜像，保证镜像自包含、开箱即出预测。
# **不含 frontend/**：后端是纯 API 服务，前端是独立静态产物，由 Caddy/nginx/CDN 托管。
# 这样后端换实现前端不用改，前端换实现后端也不用改。
COPY src/ ./src/
COPY models/ ./models/
COPY data/processed/ ./data/processed/
COPY data/raw/ ./data/raw/
# 参考数据（流域边界 GeoJSON、土地利用瓦片）放在**不属于挂载点**的 /app/geodata，
# 由 docker-entrypoint.sh 在启动时补进 /app/data/geo。
#
# 为什么不直接 COPY 到 /app/data/geo：/app/data 通常被命名卷挂载，
# 卷会整体覆盖该目录，把烘焙进去的内容遮住 —— 那样容器就不自包含了，
# 迁移时必须额外带宿主机目录并挂载，容易漏。
COPY data/geo/ ./geodata/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# 非 root 运行。data 与 var/logs 需要可写（挂载卷时以卷的权限为准）。
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/var/logs \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# /api/data_status 会真的去读各站 CSV，因此能发现「进程活着但数据读不到」的情况，
# 比只探测端口有意义。
HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/data_status', timeout=5).status == 200 else 1)"

# 单 worker：调度器跑在应用进程内，多 worker 会让每个 worker 各跑一遍采集。
# 需要横向扩容时把 WQ_ENABLE_SCHEDULER 设为 0，并另起一个单进程容器专跑定时任务。
# 注意用 `python -m water_quality.app` 也可以，但 Flask 自带服务器不适合生产。
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", \
     "--access-logfile", "-", "--error-logfile", "-", "water_quality.app:app"]
