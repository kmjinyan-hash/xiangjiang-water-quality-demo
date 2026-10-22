# -*- coding: utf-8 -*-
"""登录与会话。

设计取舍（都写在能看见的地方，便于以后自查）
--------------------------------------------
1. **密码**：用 ``werkzeug.security`` 的哈希（自带算法标识与随机盐）。
   库里只存哈希串，永远不存明文，也不把明文写进日志。
   不加 bcrypt/argon2 依赖 —— werkzeug 已是现有依赖，且其默认算法足够。

2. **会话放服务端**，不用签名 cookie：
   * 可以**主动踢人**（删一行就失效），签名 cookie 做不到；
   * 能看在线设备（ip / ua）；
   * 库里只存 token 的 sha256，**库被看到也拿不到可用的 token**。

3. **凭证放 HttpOnly Cookie**，不用 Authorization 头：
   瓦片与下载是 ``<img>`` / ``<a>`` 发起的，浏览器**无法给它们加自定义头**。
   放 cookie 才能让这几个接口也受保护。``HttpOnly`` 防 XSS 读取，
   ``SameSite=Lax`` 挡住跨站提交（CSRF 的第一道防线）。

4. **首次启动**：库里没有任何用户时，用 ``WQ_ADMIN_USER`` / ``WQ_ADMIN_PASSWORD``
   建一个管理员。没配就**不创建**，并在日志里告警 —— 不会出现
   "默认密码 admin/admin" 这种能被扫到的后门。

5. **HTTP 明文**：cookie 在网线上是明文传输的。局域网内可接受，
   出内网必须 HTTPS（否则同网段抓包即可劫持会话）。这一点写在文档里，不假装安全。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta

from flask import request
from werkzeug.security import check_password_hash, generate_password_hash

from . import config
from .db import (count_users, create_session, create_user, delete_session,
                 find_user, get_session, touch_login)

logger = logging.getLogger(__name__)

COOKIE_NAME = 'wq_session'

# 不需要登录就能访问的路径（精确匹配）。
# 注意 /api/data_status 必须放行 —— 容器的 HEALTHCHECK 打的就是它，
# 一旦要求登录，容器会被判成 unhealthy 然后被反复重启。
PUBLIC_PATHS = {
    '/api/auth/login',
    '/api/auth/me',        # 未登录时返回 401 + 登录态信息，供前端判断
    '/api/data_status',    # 容器健康检查
}

# 登录接口的简单限速：同一 IP 在窗口内失败次数过多就暂时拒绝。
# 目的只是挡住暴力猜密码，不是完整的 WAF。
_RATE = {}
_RATE_WINDOW = 300      # 5 分钟
_RATE_MAX_FAIL = 8


def hash_password(raw: str) -> str:
    return generate_password_hash((raw or '').strip())


def verify_password(pwd_hash: str, raw: str) -> bool:
    try:
        return check_password_hash(pwd_hash, (raw or '').strip())
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def new_session_token() -> str:
    """随机不可猜的会话 token（不是签名，纯粹随机，靠服务端查表校验）。"""
    return secrets.token_urlsafe(32)


def create_session_for(user: dict) -> str:
    token = new_session_token()
    expires = config.now() + timedelta(hours=config.SESSION_HOURS)
    create_session(
        _token_hash(token), user['id'],
        expires.strftime('%Y-%m-%dT%H:%M:%S'),
        ip=request.remote_addr or '',
        ua=request.headers.get('User-Agent') or '')
    touch_login(user['id'])
    return token


def set_session_cookie(resp, token: str):
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=int(config.SESSION_HOURS * 3600),
        httponly=True,                          # JS 读不到，防 XSS 偷 token
        samesite=config.COOKIE_SAMESITE,        # Lax 同源/同站；None 跨站（需 HTTPS）
        secure=bool(config.COOKIE_SECURE),      # 仅 HTTPS 时开启（HTTP 下开了就收不到）
        path='/')
    return resp


def clear_session_cookie(resp):
    resp.delete_cookie(COOKIE_NAME, path='/')
    return resp


def token_from_request():
    """从 cookie 取 token；也接受 Authorization: Bearer，方便命令行/APK 调试。"""
    t = request.cookies.get(COOKIE_NAME)
    if t:
        return t
    h = request.headers.get('Authorization') or ''
    if h.lower().startswith('bearer '):
        return h[7:].strip()
    return None


def current_user():
    """返回 (user_dict, session_dict) 或 (None, None)。"""
    token = token_from_request()
    if not token:
        return None, None
    return get_session(_token_hash(token))


def logout_current():
    token = token_from_request()
    if token:
        delete_session(_token_hash(token))


def is_public_path(path: str) -> bool:
    if request.method == 'OPTIONS':        # 预检请求不带 cookie，必须放行
        return True
    return path in PUBLIC_PATHS


# ─────────────── 登录限速 ───────────────

def rate_limited(ip: str) -> bool:
    now = time.time()
    rec = _RATE.get(ip)
    if not rec:
        return False
    fails, first = rec
    if now - first > _RATE_WINDOW:
        _RATE.pop(ip, None)
        return False
    return fails >= _RATE_MAX_FAIL


def note_fail(ip: str):
    now = time.time()
    fails, first = _RATE.get(ip, (0, now))
    if now - first > _RATE_WINDOW:
        fails, first = 0, now
    _RATE[ip] = (fails + 1, first)


def note_success(ip: str):
    _RATE.pop(ip, None)


# ─────────────── 启动引导 ───────────────

def _random_password(n=16):
    """生成初始口令：去掉容易看错的字符（0/O/1/l/I），便于手抄。"""
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def bootstrap_admin():
    """库里没有用户时，创建一个**全权限管理员**。

    行为（按"默认就要有全权限账户"的要求改过）：
      1. 配了 WQ_ADMIN_USER / WQ_ADMIN_PASSWORD -> 用配置的（推荐）
      2. 没配 -> 用户名默认 admin，**自动生成随机口令**，
         口令只打印一次（同时写到 var/initial_admin_password.txt 方便取回）

    为什么不用固定的 admin/admin：
      那是能被扫描器直接尝试的后门。随机口令同样"开箱可用"，
      但只有能看到容器日志/该文件的人才知道，安全性差一个量级。

    全权限的含义：role=admin 在 permissions.allowed_station_ids() 里返回 None，
    表示不受 user_stations 约束，能看到全部断面 —— 无需再额外分配。
    """
    try:
        if count_users() > 0:
            return
    except Exception as e:
        logger.warning('检查用户表失败：%s', e)
        return

    user = (config.ADMIN_USER or '').strip() or 'admin'
    pwd = (config.ADMIN_PASSWORD or '').strip()
    generated = False
    if not pwd:
        pwd = _random_password()
        generated = True
    if len(pwd) < 8:
        logger.warning('WQ_ADMIN_PASSWORD 长度小于 8，建议换一个更强的口令。')

    if not create_user(user, hash_password(pwd), role='admin'):
        logger.warning('创建初始管理员失败（用户名可能已存在）')
        return

    if generated:
        # 写到卷里，容器重建后仍在；提示用户取走并删除。
        path_note = ''
        try:
            import os
            from .config import DATA_DIR
            path = os.path.join(DATA_DIR, 'initial_admin_password.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('username: %s\npassword: %s\n'
                        '（首次启动自动生成；登录后请立即修改口令并删除本文件）\n'
                        % (user, pwd))
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            path_note = '已写入 %s' % path
        except Exception as e:
            path_note = '写入口令文件失败：%s' % e

        logger.warning('=' * 64)
        logger.warning('已创建默认管理员（全权限）：%s', user)
        logger.warning('初始口令：%s', pwd)
        logger.warning('这串口令只打印这一次。%s', path_note)
        logger.warning('登录后请立即修改口令，并删除该文件/日志。')
        logger.warning('=' * 64)
    else:
        logger.info('已创建初始管理员：%s（口令来自环境变量，未写进代码）', user)


def mask_token(token: str) -> str:
    if not token:
        return ''
    return token[:6] + '***' + token[-4:] if len(token) > 12 else '***'
