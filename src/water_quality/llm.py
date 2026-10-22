# -*- coding: utf-8 -*-
"""在线大模型调用层（OpenAI 兼容的 /chat/completions 接口）。

为什么是"在线 API"而不是本地模型：
    项目负责人明确要求大模型部分先接在线 API。默认实现为 DeepSeek，
    换成任何 OpenAI 兼容服务只需改 WQ_LLM_BASE_URL / WQ_LLM_MODEL。

三条设计原则
------------
1. **只用标准库**。请求用 urllib，不给镜像加依赖。闭源 + 本地部署的项目，
   依赖越少越好维护。
2. **永不向上抛异常**。所有失败都收敛成 (None, meta)，调用方回落到规则生成。
   外部 API 挂掉不能导致"报告存不下来"这种业务事故。
3. **用户密钥仅用于当前请求**，不修改共享配置。无登录模式不使用服务器环境密钥。

关于思考链
----------
负责人要求暂时关闭以提升速度。请求体里带 ``thinking: {"type": "disabled"}``。
实测同一请求：含思考 2642ms / 关思考 1077ms，本场景输出质量够用。
"""
from __future__ import annotations

import contextlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import g, has_request_context

from . import config


# ══════════════════════════════════════════════════════════════
# 地址探测：跳过失效 IP
#
# 实测问题：api.deepseek.com 会解析出两个 IP，其中一个已失效 ——
#     113.240.66.218   TCP 11 ms    OK
#     175.12.122.167   TCP 21068 ms 失败(Connection refused)
# 只要先连到坏的那个，就要卡满 21 秒等防火墙丢弃 SYN，再换下一个。
# 表现是"接口偶尔要等 20 多秒"，但模型其实只用了不到 1 秒。
#
# 处理：首次请求前用短超时逐个探测，只把**能连上的地址**交给 getaddrinfo，
# 并缓存 10 分钟。TLS 仍按域名做 SNI，证书校验不受影响。
# ══════════════════════════════════════════════════════════════

_PIN_TTL = 600           # 地址缓存有效期（秒）
_PIN = {'host': None, 'addrs': [], 'ts': 0.0}


def _probe_addresses(host: str, port: int, per_timeout: float = 2.5) -> list:
    """逐个试连，返回可用的 getaddrinfo 条目（保持原顺序）。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        return []
    good, seen = [], set()
    for fam, typ, proto, _canon, sa in infos:
        ip = sa[0]
        if ip in seen:
            continue
        seen.add(ip)
        sock = None
        try:
            sock = socket.socket(fam, typ, proto)
            sock.settimeout(per_timeout)
            sock.connect(sa)
            good.append((fam, typ, proto, '', sa))
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return good


@contextlib.contextmanager
def _pinned_host(host: str, port: int):
    """在 with 块内让 getaddrinfo 只返回探测通过的地址。"""
    # 用户自带接口并发调用时使用标准 DNS，避免全局 socket 补丁跨请求干扰。
    if has_request_context():
        yield
        return
    now = time.time()
    if _PIN['host'] != host or (now - _PIN['ts']) > _PIN_TTL:
        addrs = _probe_addresses(host, port)
        _PIN.update(host=host, addrs=addrs, ts=now)
    addrs = _PIN['addrs']
    if not addrs:
        # 探不出来（例如 ICMP 全被封）就按系统默认走，不要因此失败
        yield
        return

    orig = socket.getaddrinfo

    def patched(h, p, *a, **kw):
        if h == host:
            return addrs
        return orig(h, p, *a, **kw)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = orig


def invalidate_address_cache() -> None:
    """清掉地址缓存（请求失败后重试用）。"""
    _PIN.update(host=None, addrs=[], ts=0.0)


def mask_key(key: str) -> str:
    """日志用的密钥打码。只露前 6 位和后 4 位，其余替换。"""
    if not key:
        return ''
    if len(key) <= 12:
        return '***'
    return '%s***%s' % (key[:6], key[-4:])


def _server_settings():
    return {'api_key': config.LLM_API_KEY, 'base_url': config.LLM_BASE_URL,
            'model': config.LLM_MODEL, 'thinking': config.LLM_THINKING,
            'timeout': config.LLM_TIMEOUT, 'max_tokens': config.LLM_MAX_TOKENS,
            'temperature': config.LLM_TEMPERATURE}


def request_settings(header):
    """配置随当前请求传递，不修改环境变量、不保存用户密钥。"""
    empty = {**_server_settings(), 'api_key': '', 'base_url': '', 'model': ''}
    if not header:
        return _server_settings() if config.REQUIRE_LOGIN else empty
    if len(header) > 16384:
        raise ValueError('API 配置过长')
    try:
        value = json.loads(urllib.parse.unquote(header))
    except (ValueError, TypeError):
        raise ValueError('API 配置格式错误') from None
    if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
        raise ValueError('API 配置格式错误')
    if not value['enabled']:
        return empty
    result = dict(empty)
    for name, limit in (('api_key', 4096), ('base_url', 2048), ('model', 256)):
        item = value.get(name)
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            raise ValueError('请完整填写服务地址、模型名称和 API Key')
        if any(ord(char) < 32 or ord(char) == 127 for char in item):
            raise ValueError('API 配置包含无效字符')
        result[name] = item.strip()
    try:
        url = urllib.parse.urlsplit(result['base_url'])
        port = url.port
    except ValueError:
        raise ValueError('服务地址无效') from None
    if (url.scheme not in ('https', 'http') or not url.hostname
            or url.username or url.password or url.query or url.fragment):
        raise ValueError('服务地址须为 HTTP(S) URL，不能包含账号、查询参数或片段')
    result['base_url'] = result['base_url'].rstrip('/')
    # thinking 是部分服务商扩展；通用接口默认不发送。
    result['thinking'] = True
    return result


def _settings():
    if has_request_context():
        return getattr(g, 'llm_settings', request_settings(None))
    return _server_settings()


def is_configured() -> bool:
    """是否具备调用条件。缺任一必需项就整层禁用（不是报错）。"""
    settings = _settings()
    return bool(settings['api_key'] and settings['base_url'] and settings['model'])


def status() -> dict:
    """给前端/排障用的状态摘要。**不含密钥**。"""
    settings = _settings()
    return {
        'configured': is_configured(),
        'provider': 'openai-compatible',
        'base_url': settings['base_url'],
        'model': settings['model'],
        'thinking': bool(settings['thinking']),
        'timeout_s': settings['timeout'],
        'max_tokens': settings['max_tokens'],
        'api_key': '***' if settings['api_key'] else '',
    }


def _endpoint() -> str:
    base = (_settings()['base_url'] or '').rstrip('/')
    # 允许用户直接把 /chat/completions 写进 base_url，避免拼出双份
    if base.endswith('/chat/completions'):
        return base
    return base + '/chat/completions'


def chat(messages, max_tokens=None, temperature=None, timeout=None) -> tuple:
    """调用一次对话补全。

    返回 ``(text, meta)``：

    * 成功：``text`` 为回复正文，``meta = {'ok': True, 'model':..., 'ms':..., 'usage':...}``
    * 失败：``text`` 为 None，``meta = {'ok': False, 'error':..., ...}``

    注意正文与思考链是分开的：``reasoning_content`` 只进 meta，绝不当成报告内容。
    """
    if not is_configured():
        return None, {'ok': False, 'error': 'unconfigured'}

    settings = _settings()
    body = {
        'model': settings['model'],
        'messages': messages,
        'max_tokens': int(max_tokens or settings['max_tokens']),
        'temperature': (settings['temperature'] if temperature is None else float(temperature)),
        'stream': False,
    }
    if not settings['thinking']:
        # DeepSeek 扩展参数；不支持的兼容服务通常会忽略未知字段
        body['thinking'] = {'type': 'disabled'}

    # ensure_ascii=False + encode('utf-8') 是必须的：
    # 中文若按本地编码（GBK）编码发出，模型会收到乱码并答非所问。
    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        _endpoint(), data=raw, method='POST',
        headers={
            'Authorization': 'Bearer %s' % settings['api_key'],
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'application/json',
        })

    host = urllib.parse.urlsplit(_endpoint()).hostname or ''
    effective_timeout = timeout or settings['timeout']
    t0 = time.time()
    payload = None

    for attempt in (1, 2):
        try:
            with _pinned_host(host, urllib.parse.urlsplit(_endpoint()).port or
                              (443 if _endpoint().startswith('https:') else 80)):
                with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
            break
        except urllib.error.HTTPError as e:
            # 4xx/5xx 属于确定性错误（密钥错、参数错、限流），重试没有意义
            detail = ''
            try:
                detail = e.read().decode('utf-8', 'replace')[:400]
            except Exception:
                pass
            return None, {'ok': False, 'http_status': e.code,
                          'error': (detail or ('HTTP %d' % e.code)).replace(settings['api_key'], '[redacted]'),
                          'ms': int((time.time() - t0) * 1000)}
        except Exception as e:
            if attempt == 1:
                # 可能是缓存里锁了个已经失效的地址，清掉重探一次
                invalidate_address_cache()
                continue
            return None, {'ok': False, 'error': ('%s: %s' % (type(e).__name__, e)).replace(settings['api_key'], '[redacted]'),
                          'ms': int((time.time() - t0) * 1000)}

    if payload is None:
        return None, {'ok': False, 'error': 'no response',
                      'ms': int((time.time() - t0) * 1000)}

    ms = int((time.time() - t0) * 1000)
    try:
        choice = (payload.get('choices') or [{}])[0]
        msg = choice.get('message') or {}
        text = (msg.get('content') or '').strip()
    except Exception as e:
        return None, {'ok': False, 'error': 'bad response: %s' % e, 'ms': ms}

    meta = {
        'ok': bool(text),
        'model': payload.get('model') or settings['model'],
        'ms': ms,
        'usage': payload.get('usage') or {},
        'thinking_on': bool(msg.get('reasoning_content')),
        'finish_reason': choice.get('finish_reason'),
    }
    if not text:
        meta['error'] = 'empty content'
    return (text or None), meta


def summarize_report(station_label: str, overall: str, indicators: list,
                     raw_text: str, max_chars: int = 600) -> tuple:
    """阻塞式写结论（粘贴文本建报告的路径用）。判定等级由调用方决定，这里只表达。"""
    messages = build_summary_messages(station_label, overall, indicators, raw_text)
    return chat(messages, max_tokens=max_chars, temperature=0.3)


# ─────────────── 本项目的两个具体用途 ───────────────

_SUMMARY_SYSTEM = (
    '你是湘江流域水质智能预警系统的分析助手。'
    '请只依据用户给出的指标数值与系统判定写结论，**不得编造未提供的数据**。'
    '输出 2-4 句连贯的中文：先给总体判定，再点出关键超标/预警项及其倍数，最后给处置建议。'
    '使用水质监测的专业表述，不要口语化，不要表情符号，不要分点罗列，不要重复原始表格。'
)

# 正式报告用的系统提示。与上面的 _SUMMARY_SYSTEM（2-4 句短评）区分开：
#   · 短评 -> 断面详情页的"一句话解读"，塞在数据旁边，长了反而碍事；
#   · 本提示 -> 「预警报告」视图里的正式报告，要求结构完整、篇幅足够。
_REPORT_SYSTEM = (
    '你是湘江流域水质智能预警系统的分析员，负责编写正式的水质预警简报。'
    '只能依据用户给出的指标数值与系统判定展开，不得编造未提供的数据，'
    '不得推测未给出的历史趋势或地理信息。'
    '写作要求：'
    '（1）篇幅 800-1200 字，使用分节小标题，条理清晰；'
    '（2）结构固定为四节：'
    '一、总体评价（先给等级判定与依据，再点明主要超标项）；'
    '二、指标分析（逐项说明实测值、超标阈值、超标倍数，只写已提供的指标）；'
    '三、成因研判（基于超标项之间的组合关系推断，如氮磷同高提示生活或农业面源；'
    '措辞用"可能""提示"，不下绝对结论）；'
    '四、处置建议（分 2-4 条，每条可操作，说明建议对象与观察频次）；'
    '（3）使用水质监测的专业表述，不使用口语、表情符号；'
    '（4）所有数值必须与用户提供的数据一致，不得改写或补充；'
    '（5）缺少某类数据时明确写"本次未提供"，不得用推测填充。'
)

_EXTRACT_SYSTEM = (
    '你是水质监测数据解析器。从用户提供的文本中抽取各项指标，'
    '只输出一个 JSON 数组，不要任何解释、不要 Markdown 代码块。'
    '数组元素格式：{"key": "指标键", "label": "中文名", "value": 数值, "unit": "单位"}。'
    '指标键**必须**是以下大写键之一，不得自造：'
    'TN（总氮）, TP（总磷）, CODMN（高锰酸盐指数）, NH3N（氨氮）, '
    'DO（溶解氧）, PH（pH）, NTU（浊度）, TDS（溶解性总固体）。'
    '数值必须是纯数字（不带单位）。看不出的指标不要臆造。'
)


def build_summary_messages(station_label: str, overall: str, indicators: list,
                           raw_text: str = '', style: str = 'report') -> list:
    """构造"写结论"用的 messages。阻塞调用与流式调用共用，保证口径一致。

    style:
      'report' 正式报告（默认）—— 800-1200 字、四节结构；
      'brief'  2-4 句短评 —— 用于断面详情页的"一句话解读"。
    """
    level_cn = {'alarm': '超标', 'warning': '预警', 'normal': '正常',
                'unknown': '未判定'}.get(overall, '未判定')

    lines = ['站点：%s' % (station_label or '（未指定）'),
             '系统判定等级：%s' % level_cn]
    if indicators:
        lines.append('已抽取指标：')
        for it in indicators:
            limit = it.get('limit')
            if limit is None:
                if it.get('range'):
                    limit = '%s~%s' % (it['range'][0], it['range'][1])
                elif it.get('alarm') is not None:
                    limit = it['alarm']
                else:
                    limit = '—'
            lines.append('  - %s = %s %s（判定：%s，超标阈值 %s）' % (
                it.get('label') or it.get('key'),
                it.get('value'),
                it.get('unit') or '',
                {'alarm': '超标', 'warning': '预警', 'normal': '正常',
                 'unknown': '未判定'}.get(it.get('level'), '未判定'),
                limit))
    else:
        lines.append('（未能从原文抽取到具体指标数值）')
    if raw_text:
        lines.append('原文节选：')
        lines.append(raw_text[:1200])

    # style='report' 走正式报告提示（长文分节）；'brief' 走 2-4 句短评。
    _sys = _REPORT_SYSTEM if style == 'report' else _SUMMARY_SYSTEM
    return [{'role': 'system', 'content': _sys},
            {'role': 'user', 'content': '\n'.join(lines)}]


def chat_stream(messages, max_tokens=None, temperature=None, timeout=None):
    """流式调用，逐段 yield 正文增量（供 SSE 转发给浏览器做打字机效果）。

    只在拿到真正的正文增量时 yield；思考链内容一律不输出。
    异常不外抛：出错就直接结束生成，由调用方用已累计的内容或规则摘要收尾。
    """
    if not is_configured():
        return

    settings = _settings()
    body = {
        'model': settings['model'],
        'messages': messages,
        'max_tokens': int(max_tokens or settings['max_tokens']),
        'temperature': (settings['temperature'] if temperature is None else float(temperature)),
        'stream': True,
    }
    if not settings['thinking']:
        body['thinking'] = {'type': 'disabled'}

    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        _endpoint(), data=raw, method='POST',
        headers={
            'Authorization': 'Bearer %s' % settings['api_key'],
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'text/event-stream',
        })
    host = urllib.parse.urlsplit(_endpoint()).hostname or ''
    try:
        with _pinned_host(host, urllib.parse.urlsplit(_endpoint()).port or
                          (443 if _endpoint().startswith('https:') else 80)):
            resp = urllib.request.urlopen(req, timeout=timeout or settings['timeout'])
        try:
            for bline in resp:
                line = bline.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                chunk = line[5:].strip()
                if chunk == '[DONE]':
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                delta = ((obj.get('choices') or [{}])[0].get('delta') or {}).get('content')
                if delta:
                    yield delta
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception:
        return



def extract_indicators(raw_text: str) -> tuple:
    """规则抽取失败时的兜底：让模型把文本解析成结构化指标。

    返回 ``(list, meta)``，list 里的元素仍需交给 rules 判定等级 ——
    模型只负责"从文本里认出数值"，不负责定级。
    """
    text, meta = chat(
        [{'role': 'system', 'content': _EXTRACT_SYSTEM},
         {'role': 'user', 'content': (raw_text or '')[:4000]}],
        max_tokens=1800,
        temperature=0.0)
    if not text:
        return [], meta
    # 容忍模型偶尔包一层代码块
    s = text.strip()
    if s.startswith('```'):
        s = s.split('```')[1] if '```' in s[3:] else s.strip('`')
        s = s.replace('json', '', 1) if s.lstrip().lower().startswith('json') else s
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            data = data.get('indicators') or data.get('data') or []
        if not isinstance(data, list):
            return [], meta
        out = []
        for it in data:
            if not isinstance(it, dict):
                continue
            # 键统一成大写，与 rules.AI_WARNING_RULES 对齐（TN/TP/...）
            key = str(it.get('key') or '').strip().upper()
            try:
                val = float(it.get('value'))
            except (TypeError, ValueError):
                continue
            if not key:
                continue
            out.append({'key': key,
                        'label': str(it.get('label') or key).strip(),
                        'value': val,
                        'unit': str(it.get('unit') or '').strip()})
        meta['parsed'] = len(out)
        return out, meta
    except Exception as e:
        meta['error'] = 'json parse: %s' % e
        return [], meta
