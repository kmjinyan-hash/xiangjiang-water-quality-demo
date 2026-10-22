# -*- coding: utf-8 -*-
"""
config.py — 全项目唯一的配置与路径真源。

设计原则：
  * 默认从本文件的 __file__ 推导项目根，**不依赖当前工作目录**；
  * 允许用 WQ_ROOT 显式指定项目根（容器 / 打包安装后的必需项）；
  * 任何模块都不再自行 sys.path.insert / os.path.dirname(...) 拼路径；
  * 可调参数一律支持环境变量覆盖，便于部署与容器化。

目录约定：
    <项目根>/
      data/processed/   ← 模型输入 CSV（st*_processed.csv）
      data/raw/         ← 原始台账（站点经纬度坐标.csv）
      data/app.db       ← SQLite（运行时产物，不入版本库）
      models/           ← .pth 权重 + .scalers.json
      dist/             ← 单文件 HTML 构建产物
      var/logs/         ← 轮转日志
      src/water_quality/web/  ← 模板与静态资源

容器化要点（详见 docs/容器化说明.md）：
    WQ_ROOT=/app            ← 指定项目根，避免装进 site-packages 后路径跑飞
    WQ_DATA_DIR/WQ_MODELS_DIR/WQ_LOGS_DIR/WQ_BACKUP_DIR  ← 单独挂载卷
    WQ_HOST=0.0.0.0         ← 容器里必须监听 0.0.0.0
    WQ_ENABLE_SCHEDULER=0   ← 多 worker 部署时**必须**关掉，只让一个进程跑调度
    WQ_TZ=Asia/Shanghai     ← 容器默认 UTC，不设会让 cron 2:00 变成北京时间 10:00
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# ─────────────── 时区 ───────────────
# 容器基础镜像默认 UTC。cron 配的是「每天 2:00」，若不显式指定时区，# 在 UTC 容器里会变成北京时间 10:00 —— 数据管道的日期归属会整体错位。
#
# WQ_TZ 管**业务时区**（定时任务、报告日期）；TZ 是**进程/日志时区**。
# 未设 WQ_TZ 时跟随 TZ，避免出现「日志时间戳是 UTC、业务时间是 +8」这种
# 自相矛盾的排障现场 —— 那样看日志会把 21:00 的事读成 13:00。
TIMEZONE_NAME = os.environ.get('WQ_TZ') or os.environ.get('TZ') or 'Asia/Shanghai'


def _load_timezone(name):
    """把时区名解析为 tzinfo，带 Windows 回退。

    Windows 没有系统级 tz 数据库，zoneinfo 需要额外的 tzdata 包；
    未安装时回退为固定偏移，保证不会因为缺包直接崩。
    Asia/Shanghai 自 1991 年起不再使用夏令时，固定 +8 与真实时区完全等价。
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        pass
    fallback = {
        'Asia/Shanghai': 8, 'PRC': 8, 'CTT': 8, 'Asia/Chongqing': 8,
        'Asia/Urumqi': 6, 'UTC': 0,
    }
    if name in fallback:
        return timezone(timedelta(hours=fallback[name]))
    return timezone.utc


TZ = _load_timezone(TIMEZONE_NAME)


def now() -> datetime:
    """配置时区的当前时间。

    故意返回 **naive** datetime：所有调用点原先都是 datetime.now()，
    返回 naive 可以让已有的 strftime 输出格式完全不变，
    只把「哪个时区」这件事修正过来。
    """
    return datetime.now(TZ).replace(tzinfo=None)


# ─────────────── 路径推导 ───────────────
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../src/water_quality
SRC_DIR = os.path.dirname(PACKAGE_DIR)                            # .../src

# 启动期的提醒信息（由 app 打印，避免在 import 期直接抛异常）
WARNINGS = []


def _looks_installed(package_dir: str) -> bool:
    """判断包是否被安装进 site-packages（而非从源码树运行）。"""
    lowered = package_dir.replace('\\', '/').lower()
    return ('/site-packages/' in lowered
            or '/dist-packages/' in lowered
            or lowered.rstrip('/').endswith('/site-packages')
            or lowered.rstrip('/').endswith('/dist-packages'))


def _resolve_project_root() -> str:
    """项目根：WQ_ROOT 优先 → 源码树推导 → 兜底当前工作目录。"""
    env_root = os.environ.get('WQ_ROOT', '').strip()
    if env_root:
        return os.path.abspath(env_root)

    if _looks_installed(PACKAGE_DIR):
        # 装进 site-packages 后，dirname(SRC_DIR) 是 Python 的 lib 目录，
        # data/ models/ logs/ 会全部落到解释器安装目录里 —— 必须显式指定。
        WARNINGS.append(
            '检测到包已安装到 site-packages，但未设置 WQ_ROOT。'
            '将回退到当前工作目录 %s 作为项目根；'
            '容器/部署环境请显式设置 WQ_ROOT=/app。' % os.getcwd())
        return os.path.abspath(os.getcwd())

    return os.path.dirname(SRC_DIR)


PROJECT_ROOT = _resolve_project_root()

DATA_DIR = os.environ.get('WQ_DATA_DIR') or os.path.join(PROJECT_ROOT, 'data')
PROCESSED_DIR = os.path.join(DATA_DIR, 'processed')
RAW_DIR = os.path.join(DATA_DIR, 'raw')
# 流域矢量边界等地理数据（GeoJSON）。与 processed/raw 分开放，
# 因为它不是监测记录，而是底图叠加用的地理要素。
GEO_DIR = os.environ.get('WQ_GEO_DIR') or os.path.join(DATA_DIR, 'geo')
MODELS_DIR = os.environ.get('WQ_MODELS_DIR') or os.path.join(PROJECT_ROOT, 'models')
LOGS_DIR = os.environ.get('WQ_LOGS_DIR') or os.path.join(PROJECT_ROOT, 'var', 'logs')
# 数据清理 / 迁移前的归档目录。
# 刻意放在 var/ 下并在 .gitignore 里排除：归档里可能含账号哈希、历史原始数据，
# 不该进版本库。
BACKUP_DIR = os.environ.get('WQ_BACKUP_DIR') or os.path.join(PROJECT_ROOT, 'var', 'backups')

# ─────────────── 前端（与后端彻底分离）───────────────
# 后端只提供 /api/* JSON 接口，**不再托管任何前端文件**。
#   · 前端是纯静态应用（frontend/ 源码 → scripts/build_frontend.py → dist/），
#     可以放到任何静态服务器：Caddy / nginx / CDN / 对象存储 / 别人的机器 / APK 内；
#   · 前端指向哪个后端由产物里的 app.config.js（apiBase）或 URL 的 ?api= 决定；
#   · 跨源访问靠 WQ_CORS_ORIGINS 放行。
# 这里不再保留 FRONTEND_DIR / SERVE_FRONTEND —— 一旦保留，后端就又"知道"前端文件树，
# 前端换目录结构时后端也得动，那正是要拆掉的耦合。
#
# 构建产物在 deploy 时挂给静态服务器，与本服务无关。

DB_PATH = os.path.join(DATA_DIR, 'app.db')
MODEL_PATH = os.path.join(MODELS_DIR, 'sequential_river_tn_model.pth')
# 与 .pth 同目录的纯 JSON 元数据（model_config + 各站缩放器）。
# 拆出来之后，权重可以走 torch.load(weights_only=True)，
# 不必再为了反序列化 sklearn 对象而开放 pickle（见 ml/inference.py）。
META_PATH = os.path.join(MODELS_DIR, 'sequential_river_tn_model.meta.json')

# ─────────────── 服务 ───────────────
# 本机默认只监听回环；容器通过环境变量监听容器内部所有地址，
# 宿主机的访问范围由 Compose 的 127.0.0.1 端口映射限制。
HOST = os.environ.get('WQ_HOST', '127.0.0.1')
PORT = int(os.environ.get('WQ_PORT', '5000'))
DEBUG = os.environ.get('WQ_DEBUG', '').lower() in ('1', 'true', 'yes')

# 反向代理 / 内网穿透支持。
# 穿透服务（frp / ngrok / cpolar / nginx …）会在前面终止连接再转发，
# 只有信任它带来的 X-Forwarded-* 头，应用才看得到用户真实使用的协议、
# 主机与路径前缀。**默认开启** —— 本项目的部署形态就是「容器 + 穿透 + 远程访问」。
# 若服务直接暴露在公网、前面没有可信代理，请设 WQ_TRUST_PROXY=0 以免头部被伪造。
TRUST_PROXY = os.environ.get('WQ_TRUST_PROXY', 'true').lower() in ('1', 'true', 'yes')

# 跨源白名单。单文件 HTML 以 file:// 打开时 origin 为 null，需要放行；
# 生产可收紧为具体来源（逗号分隔），例如 WQ_CORS_ORIGINS=http://localhost:5000
CORS_ORIGINS = [o.strip() for o in
                os.environ.get('WQ_CORS_ORIGINS', '*').split(',') if o.strip()]

# ─────────────── 后台调度（爬虫 + 清洗）───────────────
# 多 worker（gunicorn -w N）下每个 worker 都会 import app，
# 若各自启动调度器就会重复采集，因此必须能关。
ENABLE_SCHEDULER = os.environ.get('WQ_ENABLE_SCHEDULER', '0').lower() in ('1', 'true', 'yes')
PIPELINE_CRON_HOUR = int(os.environ.get('WQ_CRON_HOUR', '2'))
PIPELINE_CRON_MINUTE = int(os.environ.get('WQ_CRON_MINUTE', '0'))
PIPELINE_MISFIRE_GRACE = int(os.environ.get('WQ_MISFIRE_GRACE', '3600'))
PIPELINE_MAX_INSTANCES = int(os.environ.get('WQ_MAX_INSTANCES', '1'))

# ─────────────── 爬虫（可选依赖）───────────────
# 留空则由 webdriver-manager 自动下载匹配版本；填绝对路径则直接使用
CHROMEDRIVER_PATH = os.environ.get('WQ_CHROMEDRIVER', '').strip()
CHROME_DEBUG_PORT = int(os.environ.get('WQ_DEBUG_PORT', '9222'))
# 默认无头（保持原有行为）；设 WQ_HEADLESS=false 可显示浏览器窗口便于调试
CRAWLER_HEADLESS = os.environ.get('WQ_HEADLESS', 'true').lower() in ('1', 'true', 'yes')

# 采集目标页：中国环境监测总站「全国地表水水质自动监测实时数据」。
# 历史故障：此处曾被改成 https://www.google.com（调试占位地址），导致采集长期为空。
# 现已恢复为官方地址；该服务 2026-10 已被官方下线（多地实测 ERR_CONNECTION_REFUSED），
# 日常取数请走 pipeline.ingest 通用导入。
CRAWLER_URL = os.environ.get(
    'WQ_CRAWLER_URL',
    'https://szzdjc.cnemc.cn:8070/GJZ/Business/Publish/Main.html',
)
# 备用入口：总站主站 www.cnemc.cn 上同时引用了 8068 端口，用作主备回退
CRAWLER_URL_FALLBACKS = [
    'https://szzdjc.cnemc.cn:8068/GJZ/Business/Publish/Main.html',
]
# 单次采集总时限（秒）。到点主动收尾，避免任务卡死导致调度器永久 skip。
CRAWL_DEADLINE_SECONDS = int(os.environ.get('WQ_CRAWL_DEADLINE', '1800'))
# 预检与页面等待超时（秒）
CRAWL_PROBE_TIMEOUT = int(os.environ.get('WQ_PROBE_TIMEOUT', '8'))
CRAWL_PAGE_TIMEOUT = int(os.environ.get('WQ_PAGE_TIMEOUT', '90'))
# 单页 HTML 解析上限（字符）。正则解析是回溯式的，异常巨大的输入既慢又可能卡死；
# 超过则截断并告警，属于「防呆」而非正常路径。
CRAWL_MAX_HTML_CHARS = int(os.environ.get('WQ_MAX_HTML_CHARS', str(8 * 1024 * 1024)))

# ─────────────── 站点 ───────────────
# 目标断面（模型输出）：st9 在数据集中不存在
TARGET_STATION_IDS = ['st10', 'st11', 'st12', 'st13', 'st14']
VALID_STATION_IDS = {f'st{i}' for i in [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14]}

# 单份 AI 报告正文上限（字符）
AI_REPORT_MAX_CHARS = 20000
# 内存中保留的 AI 报告条数上限
AI_REPORT_MAX_ITEMS = 200


# ─────────────── 在线大模型（OpenAI 兼容接口） ───────────────
# 项目负责人的要求是：大模型部分先接**在线 API**，不接本地 Ollama。
#
# 密钥只从环境变量读，代码与配置文件里不出现明文：
#     WQ_LLM_API_KEY=sk-xxxx        # 必需；不设则整层自动禁用，报告回落为规则生成
#
# 以 DeepSeek 为默认实现。实测 model id 只有 deepseek-flash / deepseek-v4-pro 两个，
# 「V4.1 Flash」对应的是 deepseek-flash。
LLM_BASE_URL = (os.environ.get('WQ_LLM_BASE_URL') or 'https://api.deepseek.com/v1').strip()
LLM_MODEL = (os.environ.get('WQ_LLM_MODEL') or 'deepseek-flash').strip()
LLM_API_KEY = (os.environ.get('WQ_LLM_API_KEY') or '').strip()

# 思考链开关。负责人的要求是**暂时关闭**以提升速度：
# 实测同一请求 含思考 2642ms / 关思考 1077ms，输出质量对本场景够用。
# 置 1 则恢复默认的思考链（返回里会多 reasoning_content）。
LLM_THINKING = str(os.environ.get('WQ_LLM_THINKING', '0')).strip().lower() in ('1', 'true', 'yes', 'on')

LLM_TIMEOUT = int(os.environ.get('WQ_LLM_TIMEOUT') or 60)          # 秒
LLM_MAX_TOKENS = int(os.environ.get('WQ_LLM_MAX_TOKENS') or 2000)
try:
    LLM_TEMPERATURE = float(os.environ.get('WQ_LLM_TEMPERATURE') or 0.3)
except ValueError:
    LLM_TEMPERATURE = 0.3


# ─────────────── 登录鉴权 ───────────────
REQUIRE_LOGIN = os.environ.get('WQ_REQUIRE_LOGIN', '0').lower() in ('1', 'true', 'yes')
# 2026-10 加回。此前负责人否决过登录（见 app.py 的历史注释），
# 在一次凭据暴露事故之后由开发方要求重新加上。
#
# 初始管理员只在「库里没有任何用户」时创建：
#     WQ_ADMIN_USER=admin
#     WQ_ADMIN_PASSWORD=<至少 8 位>
# 刻意**没有默认口令** —— 避免 admin/admin 这种能被扫到的后门。
ADMIN_USER = (os.environ.get('WQ_ADMIN_USER') or '').strip()
ADMIN_PASSWORD = (os.environ.get('WQ_ADMIN_PASSWORD') or '').strip()

# 会话有效期（小时）。默认 12 —— 一个工作日，过期需重新登录。
try:
    SESSION_HOURS = float(os.environ.get('WQ_SESSION_HOURS') or 12)
except ValueError:
    SESSION_HOURS = 12.0

# cookie 是否只走 HTTPS。默认 0：本机/局域网用明文 HTTP；
# 若设 1 而实际访问是 HTTP，浏览器会直接丢弃 cookie，
# 表现为"登录成功后又跳回登录页"——这是最容易踩的坑。
COOKIE_SECURE = str(os.environ.get('WQ_COOKIE_SECURE', '0')).strip().lower() in ('1', 'true', 'yes', 'on')

# 会话 cookie 的 SameSite。前端与后端的关系决定它该取什么：
#   Lax（默认）：同源部署，或者"同一站点不同端口"。够了，也挡 CSRF。
#   None：前端在**另一个站点**（公网别的域名 / APK 内嵌页面）。
#         浏览器只在 Secure（HTTPS）下接受 None —— 所以设 None 时会强制 secure=True。
COOKIE_SAMESITE = (os.environ.get('WQ_COOKIE_SAMESITE') or 'Lax').strip().capitalize()
if COOKIE_SAMESITE not in ('Lax', 'Strict', 'None'):
    COOKIE_SAMESITE = 'Lax'
if COOKIE_SAMESITE == 'None' and not COOKIE_SECURE:
    # 静默失败最坑：None 不带 Secure 会被浏览器直接丢弃，登录永远不生效。
    # 这里强制打开 Secure，并记一条警告。
    COOKIE_SECURE = True
    WARNINGS.append('WQ_COOKIE_SAMESITE=None 需要 HTTPS，已自动启用 WQ_COOKIE_SECURE')



def station_csv_path(station_id: str) -> str:
    """由站点编号得到清洗后 CSV 的绝对路径（不做合法性校验）。"""
    return os.path.join(PROCESSED_DIR, '%s_processed.csv' % str(station_id).lower().strip())


def ensure_runtime_dirs() -> None:
    """创建所有运行时目录（幂等）。

    只读文件系统或有权限限制时不抛异常，而是记入 WARNINGS 并返回 False，
    让调用方自行决定是否致命 —— 容器里挂只读卷时不应直接崩在 import 阶段。
    """
    ok = True
    for d in (DATA_DIR, PROCESSED_DIR, RAW_DIR, MODELS_DIR, LOGS_DIR, BACKUP_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            ok = False
            WARNINGS.append('无法创建目录 %s：%s' % (d, e))
    return ok


def describe() -> str:
    """人读的配置摘要，便于启动日志与排障（尤其是容器里确认挂载对不对）。"""
    return '\n'.join([
        'PROJECT_ROOT = %s' % PROJECT_ROOT,
        'DATA_DIR     = %s' % DATA_DIR,
        'MODELS_DIR   = %s' % MODELS_DIR,
        'LOGS_DIR     = %s' % LOGS_DIR,
        'DB_PATH      = %s' % DB_PATH,
        'MODEL_PATH   = %s' % MODEL_PATH,
        'TZ           = %s' % TIMEZONE_NAME,
        'SCHEDULER    = %s (cron %02d:%02d %s)'
        % ('on' if ENABLE_SCHEDULER else 'off', PIPELINE_CRON_HOUR,
           PIPELINE_CRON_MINUTE, TIMEZONE_NAME),
        'HOST:PORT    = %s:%d' % (HOST, PORT),
        'CORS         = %s' % ','.join(CORS_ORIGINS),
        'LLM          = %s' % ('%s @ %s (thinking=%s)'
                                % (LLM_MODEL, LLM_BASE_URL,
                                   'on' if LLM_THINKING else 'off')
                                if LLM_API_KEY else '未配置（报告走规则生成）'),
        'PYTHON       = %s' % sys.version.split()[0],
    ])
