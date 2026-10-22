import os
import re
import json
import gzip
import time
import uuid
import logging
import atexit
import threading
import pandas as pd
from logging.handlers import TimedRotatingFileHandler
from flask import (Flask, request, jsonify, send_file,
                   make_response, Response, g, stream_with_context)
from apscheduler.schedulers.background import BackgroundScheduler

from .config import (
    HOST, PORT, DEBUG,
    LOGS_DIR, PROJECT_ROOT, TRUST_PROXY, DATA_DIR,
    AI_REPORT_MAX_CHARS, AI_REPORT_MAX_ITEMS,
    PIPELINE_CRON_HOUR, PIPELINE_CRON_MINUTE,
    PIPELINE_MISFIRE_GRACE, PIPELINE_MAX_INSTANCES,
    VALID_STATION_IDS, TARGET_STATION_IDS,
    CORS_ORIGINS, ENABLE_SCHEDULER, TZ, TIMEZONE_NAME,
    WARNINGS, now, REQUIRE_LOGIN,
    station_csv_path, ensure_runtime_dirs,
)
from .db import (
    init_db, save_predictions, get_prediction_history,
    insert_ai_report, list_ai_reports, get_ai_report, delete_ai_report,
    list_custom_stations, add_custom_station, delete_custom_station,
    next_custom_station_id,
    find_user, list_users, create_user, delete_user,
    set_user_role, set_user_password,
    get_user, get_user_stations, set_user_stations, list_all_station_grants,
)
from .ml.inference import run_prediction, init_model
from .pipeline.clean import run_daily_pipeline
from .rules import AI_WARNING_RULES, to_frontend_rules
from .stations_data import BUILTIN_STATIONS, public_station
from . import permissions
from . import modules
from . import llm
from . import auth
from .geo import (load_basin_boundary, list_layers, layer_payload,
                  layer_tile_path, transparent_tile)

# ─── Logging ───
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
ensure_runtime_dirs()                       # 失败不抛异常，改为追加 WARNINGS
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

logger = logging.getLogger(__name__)

# File logging: rotate daily, keep 30 days.
# 容器里挂只读卷 / 权限不足时不应让整个服务起不来，退化为仅控制台输出。
try:
    _file_handler = TimedRotatingFileHandler(
        os.path.join(LOGS_DIR, 'app.log'), when='midnight', backupCount=30, encoding='utf-8'
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(_file_handler)
except OSError as e:
    logger.warning('文件日志不可用（%s），仅输出到控制台', e)

for _w in WARNINGS:
    logger.warning(_w)
logger.info('项目根=%s 时区=%s 调度器=%s',
            PROJECT_ROOT, TIMEZONE_NAME, 'on' if ENABLE_SCHEDULER else 'off')

# ─── Inference lock (prevents concurrent model runs) ───
_inference_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════
#  AI 大模型预告报告：指标抽取 + 预警判定（第二页，接口待定→自包含实现）
#  阈值表已抽到 water_quality/rules.py（唯一真源）——
#  原先就放在这里，导致构建脚本为了拿这张常量表不得不 import 整个 app
#  （会建库、载模型、起调度器），而且它和两个前端页面各存一份、且已经漂移
#  （stations.js 只有 4 个指标，还用的是 camelCase）。
# ══════════════════════════════════════════════════════════════

# AI 报告已改为**落库**（见 db.insert_ai_report 等）。
# 原先这里是一个内存列表（上限 200），进程重启就全丢 —— 而报告恰恰是要留存追溯的。

_SENTENCE_SPLIT = re.compile(r'[。；;!?\n]+')


def _ai_extract_indicators(text: str):
    """用正则从 AI 报告文本抽取指标数值，并对照阈值给出等级。"""
    results = []
    for key, rule in AI_WARNING_RULES.items():
        hit = None
        for alias in rule['aliases']:
            # 指标名后 0~24 个非数字字符内出现的第一个数值
            for m in re.finditer(r'(?:' + alias + r')[^0-9\-]{0,24}?(-?\d+(?:\.\d+)?)', text, re.I):
                hit = m
                break
            if hit:
                break
        if not hit:
            continue

        try:
            value = float(hit.group(1))
        except ValueError:
            continue
        if not (-50 < value < 100000):
            continue

        evidence = ''
        for sent in _SENTENCE_SPLIT.split(text):
            if hit.group(0)[:12] in sent:
                evidence = sent.strip()[:120]
                break

        results.append(_ai_make_indicator(key, value, evidence))
    return results


def _ai_make_indicator(key, value, evidence=''):
    """按 rules.py 的统一口径构造一条指标明细（含阈值与等级）。

    抽出来是为了让「正则抽取」与「大模型兜底解析」共用同一套构造逻辑：
    两条路径得到的等级必须完全一致，否则同一份数据会出现两个结论。
    """
    rule = AI_WARNING_RULES[key]
    level, note = _ai_judge(key, value, rule)
    item = {
        'key': key, 'label': rule['label'], 'unit': rule.get('unit', ''),
        'value': round(value, 3), 'level': level, 'note': note, 'evidence': evidence,
    }
    if rule.get('type') == 'range':
        item.update({'warn': None, 'alarm': None, 'lowerIsWorse': False,
                     'range': [rule['low_alarm'], rule['high_alarm']],
                     'warnText': '适宜 %.1f~%.1f' % (rule['low_warn'], rule['high_warn']),
                     'alarmText': '安全 %.1f~%.1f' % (rule['low_alarm'], rule['high_alarm'])})
    else:
        item.update({'warn': rule['warn'], 'alarm': rule['alarm'],
                     'lowerIsWorse': bool(rule.get('lower_is_worse'))})
    return item


def _ai_judge(key, value, rule):
    """返回 (level, note)，level ∈ normal / warning / alarm。"""
    if rule.get('type') == 'range':
        if value < rule['low_alarm'] or value > rule['high_alarm']:
            return 'alarm', '超出 pH 安全区间 %.1f~%.1f，判定超标' % (rule['low_alarm'], rule['high_alarm'])
        if value < rule['low_warn'] or value > rule['high_warn']:
            return 'warning', '偏离 pH 适宜区间 %.1f~%.1f，判定预警' % (rule['low_warn'], rule['high_warn'])
        return 'normal', '处于 pH 适宜区间 %.1f~%.1f' % (rule['low_warn'], rule['high_warn'])

    warn, alarm = rule['warn'], rule['alarm']
    label = rule['label']
    if rule.get('lower_is_worse'):
        if value <= alarm:
            return 'alarm', '%s %.2f 低于超标阈值 %.2f' % (label, value, alarm)
        if value <= warn:
            return 'warning', '%s %.2f 低于预警阈值 %.2f' % (label, value, warn)
        return 'normal', '%s %.2f 高于预警阈值 %.2f' % (label, value, warn)
    if value >= alarm:
        return 'alarm', '%s %s 达到/超过超标阈值 %s' % (label, value, alarm)
    if value >= warn:
        return 'warning', '%s %s 达到/超过预警阈值 %s' % (label, value, warn)
    return 'normal', '%s %s 低于预警阈值 %s' % (label, value, warn)


def _ai_keyword_level(text: str):
    """指标抽取为空时的兜底：按结论性关键词判定。"""
    if re.search(r'严重超标|重度污染|重大风险|极度异常', text):
        return 'alarm', '文本未给出具体数值，按「严重超标」等结论性表述判定'
    if re.search(r'超标', text):
        return 'alarm', '文本未给出具体数值，按「超标」表述判定'
    if re.search(r'预警|风险|异常|恶化', text):
        return 'warning', '文本未给出具体数值，按「预警/风险」表述判定'
    if re.search(r'正常|达标|良好|无风险', text):
        return 'normal', '文本未给出具体数值，按「正常/达标」表述判定'
    return 'unknown', '文本中未识别到水质指标数值或结论性表述'


def _ai_build_report(station, content, report_date=None, source='', title=''):
    """解析 AI 文本 → 报告对象（含总体等级与结论摘要）。

    大模型在这里只做两件事，**都不参与定级**：
      1. 规则正则抽不到指标时，兜底把自由文本解析成结构化数值；
      2. 把「指标 + rules 判定结果」写成一段人读的结论与处置建议。
    等级始终由 rules.py 的阈值判定产出 —— 必须可复现、可审计。
    模型不可用 / 超时 / 报错时自动回落到原有的模板摘要，报告照常入库。
    """
    indicators = _ai_extract_indicators(content)
    extract_meta = None

    # ① 规则没抽到 → 让模型从自由文本里认数值（等级仍交给 rules）
    if not indicators and llm.is_configured():
        raw_items, extract_meta = llm.extract_indicators(content)
        for it in raw_items:
            key = it.get('key')
            if key in AI_WARNING_RULES:
                indicators.append(_ai_make_indicator(
                    key, it['value'], '由大模型从原文解析'))

    order = {'alarm': 3, 'warning': 2, 'normal': 1, 'unknown': 0}
    if indicators:
        overall = max(indicators, key=lambda i: order.get(i['level'], 0))['level']
        abnormal = [i for i in indicators if i['level'] != 'normal']
        if abnormal:
            parts = ['%s %s%s（%s）' % (i['label'], i['value'], i['unit'] and (' ' + i['unit']),
                                       {'alarm': '超标', 'warning': '预警'}[i['level']]) for i in abnormal]
            summary = '共抽取 %d 项指标，其中 %d 项异常：%s。总体判定：%s。' % (
                len(indicators), len(abnormal), '；'.join(parts),
                {'alarm': '超标', 'warning': '预警'}[overall])
        else:
            summary = '共抽取 %d 项指标，全部低于预警阈值。总体判定：正常。' % len(indicators)
    else:
        overall, reason = _ai_keyword_level(content)
        summary = '未能从文本中抽取到具体指标数值，%s。总体判定：%s。' % (
            reason, {'alarm': '超标', 'warning': '预警', 'normal': '正常', 'unknown': '未判定'}[overall])

    # ② 模型写结论（失败则保留上面的模板摘要）
    rule_summary = summary
    llm_meta = None
    generated_by = 'rules'
    if llm.is_configured():
        text, llm_meta = llm.summarize_report(station, overall, indicators, content)
        if text:
            summary = text
            generated_by = 'llm'
        else:
            logger.warning('LLM 生成结论失败，回落为规则摘要：%s',
                           (llm_meta or {}).get('error'))

    return {
        'id': uuid.uuid4().hex[:12],
        'station': station,
        'report_date': report_date or str(now().date()),
        'source': source or '未标注来源',
        'title': title or '',
        'content': content,
        'created_at': now().strftime('%Y-%m-%dT%H:%M:%S'),
        'overall': overall,
        'summary': summary,
        'rule_summary': rule_summary,
        'generated_by': generated_by,
        'llm': llm_meta,
        'llm_extract': extract_meta,
        'indicators': indicators,
    }

def _latest_station_values(sid):
    """取某断面处理后 CSV 的最后一条记录，返回 (指标字典, 日期字符串)。

    用于"指定断面、主动让模型生成报告"：用户不必粘贴文本，
    直接拿库里最新一条监测数据走阈值判定。
    """
    try:
        path = station_csv_path(sid)
        if not os.path.exists(path):
            return {}, None
        df = pd.read_csv(path)
        if df is None or df.empty:
            return {}, None
        last = df.iloc[-1]
        date = str(last.get('DATE') or '').strip()
        out = {}
        for key in AI_WARNING_RULES:
            if key not in df.columns:
                continue
            try:
                v = float(last[key])
            except (TypeError, ValueError):
                continue
            if v != v:                      # NaN
                continue
            out[key] = v
        return out, date
    except Exception as e:
        logger.warning('读取断面 %s 最新数据失败：%s', sid, e)
        return {}, None


def _ai_make_report(station, content, indicators, overall, summary, rule_summary,
                    source='', title='', report_date=None, generated_by='rules',
                    llm_meta=None):
    """组装报告对象。阻塞式与流式两条路径共用，字段口径一致。"""
    return {
        'id': uuid.uuid4().hex[:12],
        'station': station,
        'report_date': report_date or str(now().date()),
        'source': source or '未标注来源',
        'title': title or '',
        'content': content,
        'created_at': now().strftime('%Y-%m-%dT%H:%M:%S'),
        'overall': overall,
        'summary': summary,
        'rule_summary': rule_summary,
        'generated_by': generated_by,
        'llm': llm_meta,
        'indicators': indicators,
    }


def _ai_rule_summary(indicators, overall, content):
    """规则模板摘要（模型不可用时的兜底，也用于对照）。"""
    if indicators:
        abnormal = [i for i in indicators if i['level'] != 'normal']
        if abnormal:
            parts = ['%s %s%s（%s）' % (i['label'], i['value'], i['unit'] and (' ' + i['unit']),
                                       {'alarm': '超标', 'warning': '预警'}[i['level']])
                     for i in abnormal]
            return '共抽取 %d 项指标，其中 %d 项异常：%s。总体判定：%s。' % (
                len(indicators), len(abnormal), '；'.join(parts),
                {'alarm': '超标', 'warning': '预警'}[overall])
        return '共抽取 %d 项指标，全部低于预警阈值。总体判定：正常。' % len(indicators)
    overall2, reason = _ai_keyword_level(content or '')
    return '未能从文本中抽取到具体指标数值，%s。总体判定：%s。' % (
        reason, {'alarm': '超标', 'warning': '预警', 'normal': '正常',
                 'unknown': '未判定'}[overall2])


def _is_known_station(sid: str) -> bool:
    """已知站点：内置 14 个 + 用户新增（custom_stations 表）。"""
    sid = (sid or '').lower().strip()
    if sid in VALID_STATION_IDS:
        return True
    try:
        return any(r['id'] == sid for r in list_custom_stations())
    except Exception:
        return False


def _resolve_station_csv(station_raw: str):
    """校验站点编号并返回其 CSV 绝对路径；非法时 path 为 None。

    用户新增的站点（custom_stations 表）也算合法 id —— 它们通常还没有
    历史 CSV，调用方按「文件不存在」处理并返回空数据即可，不该报 400。
    """
    sid = station_raw.lower().strip()
    if _is_known_station(sid):
        return station_csv_path(sid), sid
    return None, sid


# 后端**只提供 JSON 接口**。它不再托管任何前端文件，也不产出 HTML：
#   · 前端是独立的静态应用，由 Caddy / nginx / CDN / 别人的机器托管；
#   · 前端可以在任意 origin（本机、局域网、公网、APK 内置）；
#   · 跨源访问靠 WQ_CORS_ORIGINS 放行，认证支持 cookie 与 Bearer token 两种方式。
#
# 这里刻意不设 static_folder / static_url_path：一旦设了，后端就又变成
# "顺便发网页"的服务器，前端换实现时后端也得跟着改 —— 那正是要拆掉的耦合。
app = Flask(__name__)

# 反向代理 / 内网穿透支持。
# 穿透服务（frp / ngrok / cpolar / nginx …）会在前面终止连接再转发，只有信任
# 它带来的 X-Forwarded-* 头，应用才看得到**用户真实使用的**协议、主机与路径前缀；
# 否则应用以为自己在 http://内网IP:5000 上，任何基于 URL 的生成都会指向错地址。
# x_prefix 专门处理「子路径穿透」：https://主站/wq/ → 容器 /。
if TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1,
                            x_host=1, x_port=1, x_prefix=1)
    logger.info('已启用代理头信任（WQ_TRUST_PROXY=1），适用于穿透/反向代理部署')
# 这里刻意不设置 app.secret_key：
# 它原先只为登录鉴权（session）服务，而该需求已被负责人否决（2026-10），
# 相关代码与 users 表均已移除。将来真要做会话时再加回来即可。
# （Flask 只在用到 session/flash 时才需要 secret_key，当前没有任何路由用到。）


# gzip 结果缓存：(路径, ETag) -> 压缩后的字节。
#
# 必须缓存！第一版每次请求都把整个文件读进内存再压缩 —— echarts.min.js 有 1 MB，
# 首屏要处理 ~19 个文件，等于每次加载都重复做一遍压缩，肉眼看就是卡顿。
# 现在按 ETag（Flask 由 mtime+size 生成）做键，命中直接返回，零 IO 零 CPU。
# 容量上限很小：静态资源总共就几十个，超了整体清空即可，不需要 LRU。
_GZIP_CACHE = {}
_GZIP_CACHE_MAX = 64
_GZIP_TYPES = ('application/javascript', 'application/json',
               'application/xml', 'image/svg+xml')


@app.after_request
def _no_store_api(resp):
    """动态接口一律不缓存。

    浏览器的启发式缓存会复用没有明确缓存头的 GET 响应，导致列表停在旧内容
    （"报告列表永远不刷新、点已删除的报告报 Report not found"就是这么来的）。
    瓦片与静态资源各有自己的 Cache-Control，这里只针对 /api/ 下的 JSON。
    """
    try:
        if (request.path.startswith('/api/')
                and not request.path.startswith('/api/geo_tile/')
                and (resp.headers.get('Content-Type') or '').startswith('application/json')):
            resp.headers['Cache-Control'] = 'no-store, must-revalidate'
            resp.headers.pop('ETag', None)
    except Exception:
        pass
    return resp


@app.after_request
def _gzip(resp):
    """对文本类响应做 gzip 压缩（带缓存）。

    这是降低客户端加载时间最有效的一步：echarts.min.js 从 1 MB 降到 334 KB。
    只压文本类、只压 >1KB 的响应；图片/字体本身已压缩，不处理。
    """
    try:
        if resp.status_code != 200:
            return resp
        if resp.headers.get('Content-Encoding'):
            return resp
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return resp
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        # SSE 必须逐条下发，gzip 会把整个流缓冲起来（流式就失效了）
        if ctype == 'text/event-stream':
            return resp
        if not (ctype.startswith('text/') or ctype in _GZIP_TYPES):
            return resp

        # 只缓存**带 ETag** 的响应。
        #
        # 曾经的写法是 `key = (path, ETag or ctype)`：动态 JSON 没有 ETag，
        # 键就退化成 (路径, 'application/json') 这种常量 —— 第一次的响应被永久
        # 复用，于是报告列表永远停在旧内容、点已删除的报告报 "Report not found"、
        # 删除后列表也不刷新（均已实测复现）。动态内容必须每次现压。
        etag = resp.headers.get('ETag')
        key = (request.path, etag) if etag else None
        packed = _GZIP_CACHE.get(key) if key else None
        if packed is None:
            # 静态文件走 send_file 时 direct_passthrough=True，body 不进内存，
            # 必须先关掉才能读到数据。
            resp.direct_passthrough = False
            data = resp.get_data()
            if len(data) < 1024:
                return resp
            packed = gzip.compress(data, 6)
            if len(packed) >= len(data):
                return resp
            if key:
                if len(_GZIP_CACHE) >= _GZIP_CACHE_MAX:
                    _GZIP_CACHE.clear()
                _GZIP_CACHE[key] = packed

        resp.direct_passthrough = False
        resp.set_data(packed)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = str(len(packed))
        resp.headers.add('Vary', 'Accept-Encoding')
    except Exception as e:                      # 压缩失败绝不能影响正常返回
        logger.warning('gzip skipped: %s', e)
    return resp


@app.after_request
def _static_cache(resp):
    """静态资源给一小时缓存。

    应用脚本都带 ?v= 版本号（见 index.html），改代码时 URL 变化会自动取新文件，
    所以这里可以放心用 max-age；index.html 本身不带长缓存，始终会重新校验，
    保证 ?v= 能及时生效。
    """
    if request.path.startswith('/assets/') and resp.status_code == 200:
        resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.after_request
def _cors(resp):
    """跨源放行。

    加登录之后这里必须改一处：浏览器对**带凭证**的跨源请求不接受
    `Access-Control-Allow-Origin: *`（规范禁止），必须回显具体 origin
    并加 `Access-Control-Allow-Credentials: true`，否则前端拿不到 cookie，
    表现是"登录成功但下一次请求又 401"。

    做法：请求带了 Origin 就回显它；没带（同源请求、curl）就不设。
    仍需收紧时用 WQ_CORS_ORIGINS 指定白名单。
    """
    origin = request.headers.get('Origin')
    allow_all = '*' in CORS_ORIGINS
    # `file://` 打开的前端（双击 index.html 的轻量软件）发出的 Origin 是字符串 "null"。
    # 这类客户端用 Authorization 头认证，**不依赖 cookie**，所以这里放行它，
    # 并且**不**返回 Allow-Credentials —— 对 null 源开启凭证是危险的（任意本地页面都能带 cookie）。
    is_file_origin = (origin == 'null')
    if origin and (allow_all or origin in CORS_ORIGINS or is_file_origin):
        resp.headers['Access-Control-Allow-Origin'] = origin
        if not is_file_origin:
            resp.headers['Access-Control-Allow-Credentials'] = 'true'
        resp.headers['Vary'] = 'Origin'
    elif allow_all:
        # 无 Origin 的请求给 * 也无害（不会被浏览器用于凭证校验）
        resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Auth-Mode, X-WQ-LLM-Config'
    return resp


# ══════════════════════════════════════════════════════════════
# 登录鉴权
#
# 保护范围：除 auth.PUBLIC_PATHS 之外的所有 /api/*。
# 凭证走 HttpOnly Cookie（瓦片与下载是 <img>/<a>，带不了自定义头）。
# ══════════════════════════════════════════════════════════════
@app.before_request
def _require_login():
    """未登录一律 401（预检请求与白名单除外）。"""
    if not request.path.startswith('/api/'):
        return None                       # 非 API 路径（静态/首页）不拦
    try:
        g.llm_settings = llm.request_settings(request.headers.get('X-WQ-LLM-Config'))
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    if not REQUIRE_LOGIN:
        # 无账号模式只开放业务功能，旧账号与用户管理接口不再提供。
        if (request.path.startswith(('/api/auth/', '/api/users'))
                or request.path == '/api/admin/stations_grants'):
            return jsonify({'success': False, 'message': '当前版本不提供账号管理'}), 404
        g.user = {'id': 'local', 'role': 'admin', 'username': 'local'}
        return None
    if auth.is_public_path(request.path):
        # 健康检查允许匿名；携带有效凭证时仍需建立身份，供数据状态按权限过滤。
        if request.path == '/api/data_status' and request.method != 'OPTIONS':
            user, _sess = auth.current_user()
            if user:
                g.user = user
        return None
    user, _sess = auth.current_user()
    if user:
        g.user = user
        return None
    return jsonify({'ok': False, 'success': False,
                    'error': {'code': 'UNAUTHORIZED', 'message': '请先登录'},
                    'message': '请先登录'}), 401


def _is_admin():
    u = getattr(g, 'user', None)
    return bool(u and u.get('role') == 'admin')


# ── 按站点授权（管理员维护） ──

@app.route('/api/users/<uid>/stations', methods=['GET'])
def api_user_stations_get(uid):
    """某个用户被授权了哪些断面 + 系统里全部断面（给管理界面画勾选框）。"""
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    if not get_user(uid):
        return jsonify({'ok': False, 'message': '用户不存在'}), 404
    return jsonify({'ok': True, 'success': True, 'data': {
        'user_id': uid,
        'station_ids': get_user_stations(uid),
        'all_station_ids': permissions.all_station_ids(),
    }})


@app.route('/api/users/<uid>/stations', methods=['PUT'])
def api_user_stations_put(uid):
    """整体替换某个用户的断面授权。非法 id 自动丢弃，不报错。"""
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    if not get_user(uid):
        return jsonify({'ok': False, 'message': '用户不存在'}), 404
    body = request.get_json(silent=True) or {}
    raw = body.get('station_ids') or []
    if not isinstance(raw, list):
        return jsonify({'ok': False, 'message': 'station_ids 必须是数组'}), 400
    valid = {s.lower() for s in permissions.all_station_ids()}
    clean = []
    for s in raw:
        sid = str(s or '').strip().lower()
        if sid in valid and sid not in clean:
            clean.append(sid)
    saved = set_user_stations(uid, clean)
    logger.info('更新站点授权：user=%s 断面=%s', uid, ','.join(saved) or '(无)')
    return jsonify({'ok': True, 'success': True, 'data': {'station_ids': saved}})


@app.route('/api/admin/stations_grants', methods=['GET'])
def api_admin_station_grants():
    """全部用户的断面授权，管理界面一次取全（避免 N+1）。"""
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    return jsonify({'ok': True, 'success': True,
                    'data': {'grants': list_all_station_grants(),
                             'all_station_ids': permissions.all_station_ids()}})


# ══════════════════════════════════════════════════════════════
# 按站点授权：统一拦截
#
# 为什么不把检查写进每个接口函数里：
#   按站点授权最典型的失败不是"没做"，而是"漏了一个接口"。
#   那等于留了一个 `curl` 就能越权的洞，而且很难被发现。
#   所以这里用**一张路由表**统一拦截，新增接口只要登记一行；
#   评审时只需看这张表是否覆盖了所有带站点的路径。
#
# 判定顺序：
#   _require_login（先跑）-> 设置 g.user -> 本函数 -> 具体 handler
# ══════════════════════════════════════════════════════════════

# 站点从 URL 路径里取
_SCOPE_PATH_RULES = [
    re.compile(r'^/api/history/([^/]+)$'),
    re.compile(r'^/api/prediction_history/([^/]+)$'),
    re.compile(r'^/api/download/([^/]+)$'),
]

# 站点从 JSON body 或查询串里取
_SCOPE_BODY_PATHS = {'/api/ai_report', '/api/ai_report/generate'}


def _deny_station(sid):
    msg = permissions.denied_response_message()
    logger.warning('拒绝访问断面：user=%s sid=%s path=%s',
                   (getattr(g, 'user', None) or {}).get('username'), sid, request.path)
    return jsonify({'ok': False, 'success': False,
                    'error': {'code': 'FORBIDDEN_STATION', 'message': msg},
                    'message': msg}), 403


def _filter_by_scope(mapping):
    """把 ``{站点: 数据}`` 过滤成当前用户可见的站点。

    用于**出口**过滤（预测结果、数据状态这类聚合字典）：
    计算照常全量做，只在这里按权限裁剪，避免把权限逻辑混进业务逻辑。
    """
    if not isinstance(mapping, dict):
        return mapping
    allowed = permissions.allowed_station_ids(getattr(g, 'user', None))
    if allowed is None:
        return mapping
    return {k: v for k, v in mapping.items() if str(k).lower() in allowed}


@app.before_request
def _station_scope_guard():
    """任何带断面的写/读请求，先过一遍站点权限。"""
    user = getattr(g, 'user', None)
    if not user:
        return None                       # 未登录由 _require_login 处理

    # ① 路径里带断面
    for rx in _SCOPE_PATH_RULES:
        m = rx.match(request.path)
        if m:
            sid = m.group(1)
            if not permissions.can_access_station(user, sid):
                return _deny_station(sid)
            return None

    # ② body / 查询串里带断面
    if request.path in _SCOPE_BODY_PATHS:
        body = request.get_json(silent=True) or {}
        sid = str(body.get('station') or '').strip()
        if sid and not permissions.can_access_station(user, sid):
            return _deny_station(sid)
    if request.path == '/api/ai_reports':
        sid = (request.args.get('station') or '').strip()
        if sid and not permissions.can_access_station(user, sid):
            return _deny_station(sid)
    return None


@app.before_request
def _api_only_guard():
    """**后端永不输出网页** —— 只进出 API 数据流。

    这是硬约束，不靠"记得别写静态路由"来维持：
      · 只放行 /api/* 与根路径 /（根路径返回的是 JSON 服务发现，不是网页）；
      · 其它任何路径一律 404 JSON，即使将来有人往 Flask 里塞了模板或静态目录，
        也不会被浏览器当成页面打开。

    为什么要这道闸：前后端分离最容易被破坏的方式，是某天为了"临时方便"
    在后端加一个 HTML 路由。加了闸之后，这种改动会立刻失效而不是悄悄生效。
    """
    path = request.path or '/'
    if path == '/' or path.startswith('/api/'):
        return None
    return jsonify({
        'ok': False, 'success': False,
        'error': {'code': 'NOT_AN_API_PATH',
                  'message': '本服务只提供 /api/* 数据接口，不输出任何网页'},
        'message': '本服务只提供 /api/* 数据接口，不输出任何网页',
    }), 404


@app.before_request
def _module_guard():
    """模块被禁用时，它的所有接口统一返回 503（并说明原因）。

    这样"插拔"是真的：关掉 ai 模块，/api/ai_report/generate 就不存在了，
    而不是留一个会报内部错的半死接口。
    """
    key = modules.module_for_path(request.path)
    if not key or modules.is_enabled(key):
        return None
    return jsonify({'ok': False, 'success': False,
                    'error': {'code': 'MODULE_DISABLED',
                              'message': '模块「%s」未启用' % key},
                    'message': '模块「%s」未启用' % key}), 503


@app.route('/api/admin/modules', methods=['GET'])
def api_admin_modules():
    """管理界面用的模块清单（仅管理员）。

    前端不需要硬编码有哪些模块：拿到这个列表就渲染，
    新增模块只要在后端登记，界面自动出现。
    """
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    return jsonify({'ok': True, 'success': True,
                    'data': {'modules': modules.list_modules()}})


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username') or '').strip().lower()
    password = str(data.get('password') or '')
    ip = request.remote_addr or '-'

    if not username or not password:
        return jsonify({'ok': False, 'success': False,
                        'error': {'code': 'BAD_REQUEST', 'message': '用户名和密码不能为空'}}), 400

    if auth.rate_limited(ip):
        return jsonify({'ok': False, 'success': False,
                        'error': {'code': 'TOO_MANY_ATTEMPTS',
                                  'message': '尝试过于频繁，请 5 分钟后再试'}}), 429

    u = find_user(username)
    if not u or u.get('disabled') or not auth.verify_password(u['pwd_hash'], password):
        auth.note_fail(ip)
        logger.warning('登录失败：user=%s ip=%s', username, ip)
        time.sleep(0.4)                   # 轻微延时，抬高暴力破解成本
        return jsonify({'ok': False, 'success': False,
                        'error': {'code': 'BAD_CREDENTIALS', 'message': '用户名或密码错误'}}), 401

    auth.note_success(ip)
    token = auth.create_session_for(u)

    # 凭证默认只走 HttpOnly cookie（同源前端）。
    #
    # 但**打包形态**（Capacitor APK / Electron / 独立静态托管）拿不到 cookie：
    # WebView 的 origin 是 http://localhost 或 capacitor://localhost，访问 API 属跨源，
    # cookie 会被第三方 cookie 策略拦截。这类客户端显式声明要 token 时，
    # 才把 token 放进响应体，由客户端**只保存在内存**（不写 localStorage/文件）。
    #
    # 默认路径不变：不带这个头就不返回 token，同源前端连读都读不到。
    body = {'ok': True, 'success': True, 'data': {
        'username': u['username'], 'role': u['role']}}
    if (request.headers.get('X-Auth-Mode') or '').strip().lower() == 'bearer':
        body['data']['token'] = token
        body['data']['auth_mode'] = 'bearer'

    resp = jsonify(body)
    auth.set_session_cookie(resp, token)      # 同源前端仍然用 cookie
    logger.info('登录成功：user=%s role=%s ip=%s mode=%s',
                u['username'], u['role'], ip, body['data'].get('auth_mode', 'cookie'))
    return resp


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    auth.logout_current()
    resp = jsonify({'ok': True, 'success': True})
    auth.clear_session_cookie(resp)
    return resp


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """登录态查询。

    刻意**总是返回 200**，用 ``authenticated`` 字段表达状态：
    浏览器会把 401 记成红色错误，控制台一开就被刷屏，
    让人误以为系统有故障。这里用 200 + 布尔值，语义一样清楚。
    """
    u, sess = auth.current_user()
    if not u:
        return jsonify({'ok': True, 'success': True, 'data': {
            'authenticated': False, 'login_required': True}})
    return jsonify({'ok': True, 'success': True, 'data': {
        'authenticated': True,
        'username': u['username'], 'role': u['role'],
        'last_login': u.get('last_login'),
        'expires_at': (sess or {}).get('expires_at')}})


# ─── 用户管理（仅 admin） ───
@app.route('/api/users', methods=['GET'])
def api_users_list():
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    return jsonify({'ok': True, 'success': True, 'data': list_users()})


@app.route('/api/users', methods=['POST'])
def api_users_create():
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    data = request.get_json(silent=True) or {}
    username = str(data.get('username') or '').strip().lower()
    password = str(data.get('password') or '')
    role = str(data.get('role') or 'viewer').strip().lower()
    if not username or len(password) < 8:
        return jsonify({'ok': False, 'message': '用户名不能为空，密码至少 8 位'}), 400
    if not create_user(username, auth.hash_password(password), role=role):
        return jsonify({'ok': False, 'message': '用户名已存在'}), 409

    # **新用户默认拥有全部站点权限**（按需求：注册即全量可见）。
    # 写成"创建时显式授予全部"而不是"空集合视为全部"，是为了保留
    # "一个都看不见"这个可表达的状态 —— 管理员可以事后清空。
    granted = []
    try:
        u = find_user(username)
        if u:
            granted = set_user_stations(u['id'], permissions.all_station_ids())
    except Exception as e:
        logger.warning('给新用户授予默认站点失败：%s', e)

    return jsonify({'ok': True, 'success': True,
                    'data': {'station_ids': granted}})


@app.route('/api/users/<uid>', methods=['DELETE'])
def api_users_delete(uid):
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    me = getattr(g, 'user', None) or {}
    if me.get('id') == uid:
        return jsonify({'ok': False, 'message': '不能删除当前登录的账号'}), 400
    return jsonify({'ok': True, 'success': True, 'data': {'deleted': delete_user(uid)}})


@app.route('/api/users/<uid>', methods=['PUT'])
def api_users_update(uid):
    """改角色或重置密码，都只有 admin 能做。"""
    if not _is_admin():
        return jsonify({'ok': False, 'message': '需要管理员权限'}), 403
    data = request.get_json(silent=True) or {}
    ok = True
    if data.get('role'):
        ok = set_user_role(uid, str(data['role']).strip().lower()) and ok
    if data.get('password'):
        pwd = str(data['password'])
        if len(pwd) < 8:
            return jsonify({'ok': False, 'message': '密码至少 8 位'}), 400
        ok = set_user_password(uid, auth.hash_password(pwd)) and ok
    return jsonify({'ok': True, 'success': True, 'data': {'updated': ok}})


# Initialize database
init_db()
if REQUIRE_LOGIN:
    auth.bootstrap_admin()


def _backfill_station_grants_once():
    """升级兼容：把"老版本创建、没有任何授权记录"的用户补授全部站点。

    只执行一次（靠一个标记文件），因为规则已经改成"新用户默认全站点"，
    如果每次启动都检查，管理员主动清空某人的授权后会被重新授予。
    """
    try:
        marker = os.path.join(DATA_DIR, '.station_grants_backfilled')
        if os.path.exists(marker):
            return
        if not list_all_station_grants():
            ids = permissions.all_station_ids()
            for u in list_users():
                if str(u.get('role')) != 'admin':
                    set_user_stations(u['id'], ids)
                    logger.info('补授默认站点权限：%s -> %d 个断面', u['username'], len(ids))
        with open(marker, 'w', encoding='utf-8') as f:
            f.write('done\n')
    except Exception as e:
        logger.warning('补授站点权限失败（可忽略）：%s', e)


if REQUIRE_LOGIN:
    _backfill_station_grants_once()

# Warmup the model scalers
init_model()

# ─── 后台调度 ───
# 关键：调度器在【import 期】启动，而多 worker 部署（gunicorn -w N）会让
# 每个 worker 各 import 一次 app → 每个 worker 都跑一遍采集，重复写库。
# 因此必须能整体关闭：容器里建议 WQ_ENABLE_SCHEDULER=0 只留单个调度进程，
# 或者干脆 -w 1 单进程部署（本项目规模推荐）。
scheduler = None
if ENABLE_SCHEDULER:
    # 显式传 timezone：容器基础镜像默认 UTC，
    # 不指定的话 cron 2:00 会变成北京时间 10:00，数据日期归属整体错位。
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(
        func=run_daily_pipeline,
        trigger='cron',
        hour=PIPELINE_CRON_HOUR,
        minute=PIPELINE_CRON_MINUTE,
        timezone=TZ,
        id='daily_pipeline',
        replace_existing=True,
        misfire_grace_time=PIPELINE_MISFIRE_GRACE,
        max_instances=PIPELINE_MAX_INSTANCES,
        coalesce=True,
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info('调度器已启动：每日 %02d:%02d（%s）执行 run_daily_pipeline',
                PIPELINE_CRON_HOUR, PIPELINE_CRON_MINUTE, TIMEZONE_NAME)
else:
    logger.info('调度器未启动（WQ_ENABLE_SCHEDULER=0）')


# ══════════════════════════════════════════════════════════════
# 根路径：只报"这是个 API 服务"，不再发网页
#
# 这里曾经是前端外壳（读 index.html 替换 %V%）。删掉的原因：
# 那是把"前端构建"这件纯前端的事塞进了后端 —— 后端必须知道前端文件树长什么样，
# 前端一换目录结构后端就得改，而且前端放到别处托管时这段代码完全用不上。
# 现在前端由 Caddy 托管，资源版本号由 scripts/build_frontend.py 在构建时算好。
# ══════════════════════════════════════════════════════════════
@app.route('/')
def api_index():
    """服务发现：告诉调用方这里有哪些接口。

    任何前端（本机 / 局域网 / 公网 / APK）都可以只看这一条就知道该调什么，
    不需要读后端源码，也不需要知道后端是什么框架。
    """
    return jsonify({
        'ok': True,
        'service': '湘江流域水质智能预警平台 · API',
        'type': 'json-api',
        'api_version': 'v1',
        'frontend': '由独立静态服务器托管，与本服务无关',
        'auth': {
            'required': REQUIRE_LOGIN,
        },
        'endpoints': {
            '数据': ['GET /api/predict', 'GET /api/history/<station>',
                     'GET /api/prediction_history/<station>', 'GET /api/data_status'],
            '站点': ['GET /api/stations', 'POST /api/stations', 'DELETE /api/stations/<sid>'],
            '报告': ['POST /api/ai_report', 'GET /api/ai_reports',
                     'GET /api/ai_report/<rid>', 'DELETE /api/ai_report/<rid>',
                     'POST /api/ai_report/generate（SSE 流式）'],
            '地图': ['GET /api/geo_layers', 'GET /api/geo_layer/<key>',
                     'GET /api/geo_tile/<key>/<z>/<x>/<y>.png'],
            '阈值': ['GET /api/ai_warning_rules'],
            '模型': ['GET /api/llm_status'],
        },
    })


@app.route('/api/predict', methods=['GET'])
def predict():
    if not _inference_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': '推理正在进行中，请稍候...'}), 429

    try:
        results = run_prediction()
        save_predictions(results)          # ← persist to DB
        # 只返回当前用户有权的断面（admin 不受限）。模型照常全量推理并落库，
        # 过滤只发生在**出口**——这样后台统计与权限互不影响。
        return jsonify({'success': True, 'data': _filter_by_scope(results)})
    except Exception as e:
        logger.error(f"Prediction failed: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        _inference_lock.release()

@app.route('/api/history/<station>', methods=['GET'])
def get_history(station):
    """Return the last N rows of a station's processed CSV as JSON."""
    file_path, sid = _resolve_station_csv(station)
    if file_path is None:
        return jsonify({'success': False, 'message': 'Invalid station'}), 400

    if not os.path.exists(file_path):
        # 用户新增的站点还没有历史 CSV 属正常状态，返回空数组而不是 404，
        # 前端就能直接画"暂无数据"，不必走异常分支。
        if sid not in VALID_STATION_IDS:
            return jsonify({'success': True, 'data': []})
        return jsonify({'success': False, 'message': 'Data file not found'}), 404

    try:
        limit = request.args.get('limit', 30, type=int)
        limit = min(max(limit, 1), 200)

        df = pd.read_csv(file_path)
        for col in df.columns:
            if col != 'DATE':
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.tail(limit)
        # Use df.to_json → json.loads to reliably convert NaN → null (not the non-standard JS NaN)
        records = json.loads(df.to_json(orient='records'))
        return jsonify({'success': True, 'data': records})
    except Exception as e:
        logger.error(f"History fetch failed for {sid}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/prediction_history/<station>', methods=['GET'])
def prediction_history(station):
    """Return stored prediction records for a station."""
    station = station.upper().strip()   # normalise to "ST10" etc.
    limit = request.args.get('limit', 60, type=int)
    limit = min(max(limit, 1), 365)
    rows = get_prediction_history(station, limit)
    return jsonify({'success': True, 'data': rows})

@app.route('/api/data_status', methods=['GET'])
def data_status():
    """Return latest data date and staleness (days) for each target station."""
    today = now().date()
    status = {}
    for sid in TARGET_STATION_IDS:
        file_path = station_csv_path(sid)
        try:
            if not os.path.exists(file_path):
                status[sid.upper()] = {'latest_date': None, 'days_old': None}
                continue
            df = pd.read_csv(file_path, usecols=['DATE'])
            df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce', format='mixed')
            df = df.dropna(subset=['DATE'])
            if df.empty:
                status[sid.upper()] = {'latest_date': None, 'days_old': None}
            else:
                latest = df['DATE'].max().date()
                status[sid.upper()] = {
                    'latest_date': str(latest),
                    'days_old': (today - latest).days
                }
        except Exception as e:
            logger.warning(f"data_status failed for {sid}: {e}")
            status[sid.upper()] = {'latest_date': None, 'days_old': None}

    return jsonify({'success': True, 'data': _filter_by_scope(status)})

@app.route('/api/download/<station>', methods=['GET'])
def download_data(station):
    file_path, sid = _resolve_station_csv(station)
    if file_path is None:
        return jsonify({'success': False, 'message': 'Invalid station'}), 400

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'message': 'Data file not found'}), 404
        
    return send_file(file_path, as_attachment=True)


# ══════════════════════════════════════════════════════════════
#  AI 预告报告接口（第二页；接口形态待定，先提供自包含实现）
#   POST /api/ai_report          录入 AI 报告 → 抽取指标 + 判定预警等级
#   GET  /api/ai_reports         报告列表（默认去掉正文与指标明细）
#   GET  /api/ai_report/<rid>    单份报告详情
#   DELETE /api/ai_report/<rid>  删除
#   GET  /api/ai_warning_rules   阈值表（供前端/对接方对齐）
# ══════════════════════════════════════════════════════════════
@app.route('/api/model_eval', methods=['GET'])
def api_model_eval():
    """模型评估简报（只读）。

    数据来自 models/*.meta.json 的 extra.performance_metrics ——
    那是训练时算好的 MAE / RMSE / R2 / MAPE / Pearson 与 true/pred 序列。
    只读不算，既不重新推理，也不编造任何数值。

    诚实边界：这是训练期验证集结果，不是上线实测；
    元数据未记录时间切分方式，所以页面不声称"按时间切分"。
    """
    import json as _json
    try:
        meta_path = os.path.join(PROJECT_ROOT, 'models',
                                 'sequential_river_tn_model.meta.json')
        if not os.path.exists(meta_path):
            return jsonify({'ok': False, 'success': False,
                            'message': '模型元数据不存在'}), 404
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = _json.load(f)
        perf = (meta.get('extra') or {}).get('performance_metrics') or {}
        metrics, sample = {}, {}
        for k, v in perf.items():
            if not isinstance(v, dict):
                continue
            metrics[k] = {kk: v.get(kk) for kk in
                          ('MAE', 'RMSE', 'R2', 'MAPE', 'Pearson_r')}
            t = v.get('true') or []
            pr = v.get('pred') or []
            if t and pr:
                sample[k] = {'true': t[-60:], 'pred': pr[-60:]}
        return jsonify({'ok': True, 'success': True, 'data': {
            'metrics': metrics,
            'sample': sample,
            'model': 'sequential_river_tn_model',
            'source': 'models/sequential_river_tn_model.meta.json',
            'caveat': '训练期验证集指标，非上线实测；元数据未记录时间切分方式',
        }})
    except Exception as e:
        logger.error('model_eval failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'success': False, 'message': str(e)}), 500


@app.route('/api/llm_status', methods=['GET'])
def llm_status():
    """在线大模型状态。不含密钥，只露打码后的尾巴。

    默认只报配置情况（不发请求、毫秒级返回）；带 ``?ping=1`` 时**真的调一次**
    模型，用于验收时确认网络与密钥是否可用。ping 结果只回延迟与内容长度，
    不回显完整回复，避免把状态接口变成任意提示词通道。
    """
    st = llm.status()
    if request.args.get('ping') in ('1', 'true', 'yes'):
        if not st['configured']:
            st['ping'] = {'ok': False, 'error': 'unconfigured'}
        else:
            t0 = time.time()
            text, meta = llm.chat(
                [{'role': 'user', 'content': '回复两个字：可用'}],
                max_tokens=24, temperature=0.0, timeout=30)
            st['ping'] = {
                'ok': bool(text),
                'ms': int((time.time() - t0) * 1000),
                'model': (meta or {}).get('model'),
                'thinking_on': (meta or {}).get('thinking_on'),
                'usage': (meta or {}).get('usage'),
                'chars': len(text or ''),
                'error': (meta or {}).get('error'),
            }
    return jsonify({'success': True, 'data': st})


@app.route('/api/ai_report/generate', methods=['POST'])
def ai_report_generate():
    """指定断面，主动让在线模型生成一份报告（SSE 流式，像网页客户端那样逐字出现）。

    流程：读该断面最新一条监测记录 → rules 判定等级 → 模型逐字写结论。
    等级始终由规则决定；模型只负责叙述。生成完成后入库，并在 done 事件里回传。
    """
    data = request.get_json(silent=True) or {}
    station = str(data.get('station', '')).lower().strip()
    if not station or not _is_known_station(station):
        return jsonify({'success': False, 'message': '请指定有效断面'}), 400
    if not llm.is_configured():
        return jsonify({'success': False,
                        'message': '请先在“大模型 API”中填写服务地址、模型名称和 API Key'}), 400

    note = str(data.get('note') or '').strip()[:500]
    title = str(data.get('title') or '').strip() or ('%s 智能预警报告' % station.upper())

    # 两种文体，共用同一套「规则判定 + 模型叙述」的流水线，只是提示词与长度不同：
    #   report（默认）—— 正式报告，800-1200 字四节，**落库**，进报告列表；
    #   brief          —— 2-4 句短评，断面详情里的一句话解读，**不落库**（按方案）。
    style = str(data.get('style') or 'report').strip().lower()
    if style not in ('report', 'brief'):
        return jsonify({'success': False,
                        'message': "style 只能是 'report' 或 'brief'"}), 400

    values, date = _latest_station_values(station)
    indicators = [_ai_make_indicator(k, v, '取自该断面最新一条监测记录')
                  for k, v in values.items()]
    order = {'alarm': 3, 'warning': 2, 'normal': 1, 'unknown': 0}
    overall = (max(indicators, key=lambda i: order.get(i['level'], 0))['level']
               if indicators else 'unknown')

    lines = []
    if date:
        lines.append('数据日期：%s' % date)
    for it in indicators:
        lines.append('%s %s %s（%s）' % (it['label'], it['value'], it['unit'] or '',
                                         {'alarm': '超标', 'warning': '预警',
                                          'normal': '正常'}.get(it['level'], '')))
    if note:
        lines.append('补充说明：%s' % note)
    content = '\n'.join(lines) or '（该断面暂无可用的监测记录）'

    rule_summary = _ai_rule_summary(indicators, overall, note)
    messages = llm.build_summary_messages(station.upper(), overall, indicators, note,
                                           style=style)

    def sse(event, payload):
        return 'event: %s\ndata: %s\n\n' % (
            event, json.dumps(payload, ensure_ascii=False))

    def gen():
        yield sse('meta', {'station': station, 'overall': overall,
                           'indicators': indicators, 'date': date,
                           'rule_summary': rule_summary,
                           'model': llm.status().get('model'),
                           'count': len(indicators)})
        parts = []
        for delta in llm.chat_stream(messages, max_tokens=(260 if style == 'brief' else 1700)):
            parts.append(delta)
            yield sse('delta', {'t': delta})
        text = ''.join(parts).strip()
        report = _ai_make_report(
            station=station, content=content, indicators=indicators,
            overall=overall, summary=(text or rule_summary),
            rule_summary=rule_summary, source='模型生成', title=title,
            generated_by='llm' if text else 'rules',
            llm_meta={'model': llm.status().get('model'), 'stream': True,
                      'ok': bool(text)})
        if style == 'brief':
            # 一句话解读不落库：不占报告列表，也不会在库里留下半成品。
            # done 里回传轻量对象（没有 id），前端只取正文用。
            yield sse('done', {'report': report, 'persisted': False, 'style': 'brief'})
            return
        try:
            insert_ai_report(report, max_items=AI_REPORT_MAX_ITEMS)
        except Exception as e:
            logger.error('保存生成报告失败：%s', e)
        yield sse('done', {'report': report, 'persisted': True, 'style': 'report'})

    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-store',
                             'X-Accel-Buffering': 'no'})


@app.route('/api/ai_report', methods=['POST'])
def ai_report_create():
    data = request.get_json(silent=True) or {}
    station = str(data.get('station', '')).lower().strip()
    content = str(data.get('content', '')).strip()

    # 允许空断面：断面概览类报告不针对单一断面。非空时必须是已知站点
    # （内置或用户新增），避免把报告挂到不存在的编号上。
    if station and not _is_known_station(station):
        return jsonify({'success': False, 'message': 'Invalid station'}), 400
    if not content:
        return jsonify({'success': False, 'message': '报告内容不能为空'}), 400
    if len(content) > AI_REPORT_MAX_CHARS:
        return jsonify({'success': False,
                        'message': '报告内容过长（上限 %d 字）' % AI_REPORT_MAX_CHARS}), 400

    report = _ai_build_report(
        station=station,
        content=content,
        report_date=str(data.get('report_date') or '').strip() or None,
        source=str(data.get('source') or '').strip(),
        title=str(data.get('title') or '').strip(),
    )
    insert_ai_report(report, max_items=AI_REPORT_MAX_ITEMS)

    logger.info(f"AI report received for {station}: overall={report['overall']}, "
                f"{len(report['indicators'])} indicator(s) parsed")
    return jsonify({'success': True, 'data': report})


@app.route('/api/ai_reports', methods=['GET'])
def ai_report_list():
    station = request.args.get('station', '', type=str).lower().strip()
    limit = request.args.get('limit', 50, type=int)
    limit = min(max(limit, 1), 200)
    # 列表不返回正文与逐项明细，控制响应体积
    rows = list_ai_reports(station=station or None, limit=limit, light=True)
    return jsonify({'success': True, 'data': rows})


@app.route('/api/ai_report/<rid>', methods=['GET'])
def ai_report_detail(rid):
    r = get_ai_report(rid)
    if r is None:
        return jsonify({'success': False, 'message': 'Report not found'}), 404
    return jsonify({'success': True, 'data': r})


@app.route('/api/ai_report/<rid>', methods=['DELETE'])
def ai_report_delete(rid):
    if delete_ai_report(rid):
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Report not found'}), 404


@app.route('/api/ai_warning_rules', methods=['GET'])
def ai_warning_rules():
    """阈值表（去掉正则别名，便于前端展示与外部对接）。

    返回的是**前端键名口径**（camelCase，区间型写作 isRange），与注入单文件版的
    window.AI_WARNING_RULES 结构完全一致 —— 在线页刷新阈值与离线页内嵌阈值
    因此走的是同一份数据、同一套键名，不会再各自漂移。
    """
    rules = {}
    for key, rule in to_frontend_rules(include_aliases=False).items():
        rule['key'] = key
        rules[key] = rule
    return jsonify({'success': True, 'data': rules})


# ══════════════════════════════════════════════════════════════
# 用户新增站点
#
# 内置 14 个断面写在前端 stations.js（原始台账）。这里只增删"额外站点"，
# 落到 custom_stations 表（SQLite，位于挂载卷里，重建容器不丢）。
# ══════════════════════════════════════════════════════════════

@app.route('/api/stations', methods=['GET'])
def api_stations_list():
    """断面台账 —— 前端**唯一**的站点数据来源。

    返回「内置 14 个 + 用户新增」，前端不再自带 stations.js。
    这样改坐标/加站点只改后端一处，Web、APK、第三方前端都立刻一致。
    """
    try:
        builtin = [public_station(s) for s in BUILTIN_STATIONS]
        custom = []
        for r in list_custom_stations():
            d = dict(r)
            d['code'] = str(d.get('id') or '').upper()
            d['builtin'] = False
            custom.append(d)
        # 前端只拿到自己有权看的断面：既省得前端再过滤，
        # 也避免"看得见站名但点进去 403"的割裂感。
        visible = permissions.filter_station_dicts(getattr(g, 'user', None), builtin)
        visible += permissions.filter_station_dicts(getattr(g, 'user', None), custom)
        return jsonify({'success': True, 'ok': True,
                        'data': visible,
                        'next_id': next_custom_station_id()})
    except Exception as e:
        logger.error('list stations failed: %s', e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/stations', methods=['POST'])
def api_stations_add():
    """新增一个站点。必填：name / lon / lat；id 可留空由后端分配。"""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '站点名称不能为空'}), 400
    try:
        lon = float(body.get('lon'))
        lat = float(body.get('lat'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '经纬度必须是数字'}), 400
    if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
        return jsonify({'success': False, 'message': '经纬度超出合法范围'}), 400
    try:
        rec = add_custom_station({
            'id': body.get('id'),
            'name': name,
            'river': (body.get('river') or '').strip(),
            'city': (body.get('city') or '').strip(),
            'lon': lon, 'lat': lat,
            'role': (body.get('role') or 'upstream').strip(),
        })
        logger.info('新增站点 %s（%s）', rec['id'], rec['name'])
        return jsonify({'success': True, 'data': rec})
    except Exception as e:
        logger.error('add custom station failed: %s', e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/stations/<sid>', methods=['DELETE'])
def api_stations_delete(sid):
    """删除一个用户新增站点。内置断面删不掉（不在这张表里）。"""
    try:
        if sid.lower() in VALID_STATION_IDS:
            return jsonify({'success': False,
                            'message': '内置断面不允许删除'}), 400
        ok = delete_custom_station(sid.lower())
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.error('delete custom station failed: %s', e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# 流域矢量边界
#
# 文件放在 data/geo/ 下（GeoJSON）。服务端在此统一做 WGS-84 → GCJ-02 纠偏，
# 前端拿到的坐标可直接叠加到高德底图 —— 不纠偏会整体偏约 600 米。
# 文件不存在时返回 available=False，前端静默不显示，不影响其它功能。
# ══════════════════════════════════════════════════════════════

@app.route('/api/basin_boundary', methods=['GET'])
def api_basin_boundary():
    """兼容旧接口：只返回流域边界。新代码请用 /api/geo_layers。"""
    try:
        obj = load_basin_boundary()
    except Exception as e:
        logger.error('basin boundary load failed: %s', e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500
    if obj is None:
        return jsonify({'success': True, 'available': False, 'data': None})
    return jsonify({'success': True, 'available': True, 'data': obj})


# ══════════════════════════════════════════════════════════════
# 通用地理图层（流域边界 / 土地利用 / 以后新增的）
#
# 拆成三个接口，目的是"按需加载"：清单很轻，矢量与影像本体在用户真正
# 打开对应图层时才取，避免首屏被几十 MB 的栅格拖慢。
#   GET /api/geo_layers          图层清单（含名称、类型、默认是否开启、类别表）
#   GET /api/geo_layer/<key>     单个图层载荷（矢量已纠偏到 GCJ-02）
#   GET /api/geo_image/<key>     栅格图层的图片本体
# ══════════════════════════════════════════════════════════════

@app.route('/api/geo_layers', methods=['GET'])
def api_geo_layers():
    try:
        return jsonify({'success': True, 'data': list_layers()})
    except Exception as e:
        logger.error('geo layer list failed: %s', e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/geo_layer/<key>', methods=['GET'])
def api_geo_layer(key):
    key = (key or '').strip().lower()
    try:
        data = layer_payload(key)
    except Exception as e:
        logger.error('geo layer %s failed: %s', key, e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500
    if data is None:
        return jsonify({'success': False, 'message': '图层不存在或文件缺失'}), 404
    return jsonify({'success': True, 'data': data})


@app.route('/api/geo_tile/<key>/<int:z>/<int:x>/<int:y>.png', methods=['GET'])
def api_geo_tile(key, z, x, y):
    """土地利用瓦片。

    空区域（未保存的透明瓦片）**返回全透明 PNG + 200**，而不是 404：
    Leaflet 不会把 404 的瓦片标记为已加载，每次拖动都会重试，
    实测一半请求是 404 —— 这是拖动卡顿的主要来源。
    """
    key = (key or '').strip().lower()
    path = layer_tile_path(key, z, x, y)
    if path:
        # 走 send_file（direct_passthrough），不把文件读进内存
        resp = send_file(path, mimetype='image/png', conditional=True)
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
    # 只对合法坐标返回空瓦片；非法请求仍然 404，避免被当成图片服务探测
    valid = (0 <= z <= 20 and 0 <= x < (1 << z) and 0 <= y < (1 << z))
    if not valid or key != 'landuse':
        return jsonify({'success': False, 'message': 'Invalid tile'}), 404
    resp = make_response(transparent_tile())
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


if __name__ == '__main__':
    app.run(host=HOST, port=PORT, debug=DEBUG)
