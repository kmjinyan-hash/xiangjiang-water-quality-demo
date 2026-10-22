# -*- coding: utf-8 -*-
"""前后端解耦检查 —— 可反复运行，用事实代替"我觉得没问题"。

检查三类东西：

一、静态隔离
    后端源码里不能出现前端的东西（dist/、frontend/、%V%、index.html、
    static_folder、render_template）；前端里不能出现后端实现细节
    （water_quality、flask、app.py、/app/）。

二、接口契约
    前端用到的 /api/* 路径，必须都能在后端路由表里找到。
    这条能抓住"前端调了一个不存在的接口"这种最容易漏的错。

三、互不影响实验
    A. 重启后端 -> 前端文件哈希不变
       （前端是静态文件，后端重启不该碰它）
    B. 改动前端文件 -> 后端接口响应不变
       （后端不该读前端文件，改前端也不该影响数据）

用法：
    python scripts/check_decoupling.py
退出码 0 表示全部通过。
"""
import hashlib
import io
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
FRONTEND = os.path.join(ROOT, 'frontend')
DIST = os.path.join(ROOT, 'dist')
BASE = 'https://localhost:8443'

FAIL = []


def ok(msg):
    print('  [OK]   %s' % msg)


def bad(msg):
    print('  [FAIL] %s' % msg)
    FAIL.append(msg)


def read(p):
    with io.open(p, encoding='utf-8', errors='replace') as f:
        return f.read()


def walk(top, exts):
    for dirpath, _dirs, files in os.walk(top):
        if 'vendor' in dirpath or '__pycache__' in dirpath:
            continue
        for n in files:
            if n.endswith(exts):
                yield os.path.join(dirpath, n)


# ─────────────── 一、静态隔离 ───────────────
def check_static_isolation():
    print('\n[1] 静态隔离')

    banned_backend = [
        (re.compile(r'\bdist/'), 'dist/'),
        (re.compile(r'\bfrontend/'), 'frontend/'),
        (re.compile(r'%V%'), '%V%'),
        (re.compile(r'static_folder'), 'static_folder'),
        (re.compile(r'render_template'), 'render_template'),
        (re.compile(r'serve_frontend'), 'serve_frontend'),
    ]
    hits = []
    for p in walk(SRC, ('.py',)):
        in_doc = False
        for lineno, line in enumerate(read(p).splitlines(), 1):
            # 文档字符串里讲历史不算耦合，跳过
            quotes = line.count('"""') + line.count("'''")
            if quotes % 2 == 1:
                in_doc = not in_doc
                continue
            if in_doc:
                continue
            code = line.split('#')[0]        # 行注释也不算耦合
            for rx, name in banned_backend:
                if rx.search(code):
                    hits.append('%s:%d 引用 %s' % (os.path.relpath(p, ROOT), lineno, name))
    if hits:
        for h in hits:
            bad('后端引用前端：' + h)
    else:
        ok('后端源码零前端引用（dist/、frontend/、%V%、static_folder 等）')

    banned_front = [
        (re.compile(r'water_quality'), 'water_quality'),
        (re.compile(r'\bflask\b', re.I), 'flask'),
        (re.compile(r'app\.py'), 'app.py'),
        (re.compile(r'/app/'), '/app/'),
        (re.compile(r'localhost:5000'), 'localhost:5000'),
    ]
    hits = []
    for p in walk(FRONTEND, ('.js', '.html', '.css')):
        for lineno, line in enumerate(read(p).splitlines(), 1):
            for rx, name in banned_front:
                if rx.search(line):
                    hits.append('%s:%d 引用 %s' % (os.path.relpath(p, ROOT), lineno, name))
    if hits:
        for h in hits:
            bad('前端引用后端内部：' + h)
    else:
        ok('前端源码零后端内部引用（water_quality、flask、app.py 等）')


# ─────────────── 二、接口契约 ───────────────
def backend_routes():
    """从 app.py 里抽出所有 @app.route 路径（静态分析，不启动服务）。"""
    text = read(os.path.join(SRC, 'water_quality', 'app.py'))
    routes = []
    for m in re.finditer(r"@app\.route\(\s*['\"]([^'\"]+)['\"]", text):
        p = m.group(1)
        p = re.sub(r'<[^>]+>', 'X', p)      # 参数占位
        routes.append(p)
    return routes


def frontend_paths():
    """前端 JS 里出现的 /api/... 字面量。"""
    paths = set()
    for p in walk(FRONTEND, ('.js',)):
        if 'core.js' not in p and 'views.js' not in p and 'main.js' not in p:
            continue
        for m in re.finditer(r"['\"](/api/[^'\"\s]*)['\"]", read(p)):
            v = m.group(1)
            v = re.sub(r'<[^>]+>', 'X', v)
            v = re.sub(r"' \+ [^+]+ \+ '", 'X', v)
            paths.add(v)
    return sorted(paths)


def matches_route(fp, routes):
    """把前端路径贴到后端路由上：参数段用 X 通配。"""
    if fp.endswith('/'):
        # 前缀形式：'/api/history/' + id。只要有路由以它开头就算匹配。
        for r in routes:
            if r.startswith(fp):
                return r
        return None
    fparts = [s for s in fp.split('?')[0].split('/') if s != '']
    for r in routes:
        rparts = [s for s in r.split('/') if s != '']
        if len(fparts) != len(rparts):
            continue
        good = True
        for a, b in zip(fparts, rparts):
            if b == 'X':
                continue
            if a != b:
                good = False
                break
        if good:
            return r
    return None


def check_contract():
    print('\n[2] 接口契约（前端调用的 /api 路径必须存在）')
    routes = backend_routes()
    used = frontend_paths()
    missing = []
    for fp in used:
        if fp == '/api/x':               # 模板拼接产生的占位，不是真实路径
            continue
        if matches_route(fp, routes) is None:
            missing.append(fp)
    if missing:
        for m in missing:
            bad('前端调用不存在的接口：%s' % m)
    else:
        ok('%d 个前端接口路径全部能匹配到后端路由' % len(used))


# ─────────────── 三、互不影响实验 ───────────────
def ssl_ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def api_snapshot():
    """取一组**不需要登录**的接口响应作为后端指纹。"""
    out = {}
    for p in ('/', '/api/data_status'):
        try:
            with urllib.request.urlopen(BASE + p, context=ssl_ctx(), timeout=15) as r:
                out[p] = hashlib.sha256(r.read()).hexdigest()[:16]
        except Exception as e:
            out[p] = 'ERR:%s' % e
    return out


def hash_tree(top):
    h = {}
    for dirpath, _d, files in os.walk(top):
        for n in files:
            p = os.path.join(dirpath, n)
            with open(p, 'rb') as f:
                h[os.path.relpath(p, top)] = hashlib.sha256(f.read()).hexdigest()
    return h


def check_independence():
    print('\n[3] 互不影响实验')

    # A. 重启后端 -> 前端文件哈希不变
    before = hash_tree(DIST)
    try:
        subprocess.run(['docker', 'restart', 'wq'], capture_output=True, timeout=180)
    except Exception as e:
        bad('无法重启后端：%s' % e)
    after = hash_tree(DIST)
    if before == after:
        ok('重启后端后，前端 %d 个文件哈希完全不变' % len(before))
    else:
        diff = set(before) ^ set(after) | {k for k in before if before.get(k) != after.get(k)}
        bad('重启后端改变了前端文件：%s' % list(diff)[:5])

    # B. 改动前端 -> 后端响应不变
    snap1 = api_snapshot()
    target = os.path.join(DIST, 'index.html')
    try:
        st = os.stat(target)
        with open(target, 'ab') as f:
            f.write(b'\n<!-- decoupling-probe -->\n')
        snap2 = api_snapshot()
        # 还原
        with open(target, 'r+b') as f:
            f.truncate(st.st_size)
        os.utime(target, (st.st_atime, st.st_mtime))
    except Exception as e:
        bad('改动前端文件失败：%s' % e)
        return
    if snap1 == snap2:
        ok('改动前端 index.html 后，后端接口响应哈希不变（%s）'
           % ', '.join('%s=%s' % (k, v) for k, v in snap1.items()))
    else:
        bad('改动前端影响了后端响应：%s -> %s' % (snap1, snap2))


def main():
    print('=== 前后端解耦检查 ===')
    check_static_isolation()
    check_contract()
    check_independence()
    print()
    if FAIL:
        print('未通过 %d 项：' % len(FAIL))
        for f in FAIL:
            print('  - ' + f)
        return 1
    print('全部通过：前后端在代码、契约、运行三个层面都是独立的。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
