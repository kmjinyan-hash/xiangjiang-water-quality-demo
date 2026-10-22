# -*- coding: utf-8 -*-
"""把 frontend/ 打成可直接部署的纯静态包（默认输出到 dist/）。

为什么需要这个脚本
------------------
index.html 里的 ``%V%`` 原本由 Flask 在**请求时**替换（app.py 的 _asset_version）。
这等于把"前端资源版本"这件纯前端的事绑在了后端身上：

  * 前端一旦交给 nginx / CDN / APK，%V% 会变成死字符串；
  * 改前端不会换版本号，用户可能一直拿缓存里的旧 JS；
  * 后端必须知道前端文件树长什么样（os.walk(FRONTEND_DIR)）。

本脚本把这步搬回前端侧：构建时算一个**内容指纹**，一次性替换掉全部 %V%。
替换后 dist/ 就是纯静态产物，放哪都能跑，后端完全不参与。

用法
----
    python scripts/build_frontend.py                          # 输出到 dist/
    python scripts/build_frontend.py --api-base http://10.0.0.5:5000
    python scripts/build_frontend.py --out D:/www --api-base http://10.0.0.5:5000

``--api-base`` 会把后端地址写进产物里的 app.config.js（apiBase 字段），
这样前端不必靠 ?api= 查询串指定后端 —— 打包 APK 时同样适用。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import sys

# Windows 控制台默认 GBK，中文输出会乱码；强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(PROJECT_ROOT, 'frontend')
DEFAULT_OUT = os.path.join(PROJECT_ROOT, 'dist')

# 参与指纹计算的文件后缀（二进制大文件按 size+mtime 参与，不读内容）
TEXT_EXT = {'.html', '.js', '.css', '.svg', '.json'}


def compute_version(src_dir: str) -> str:
    """内容指纹：文本文件取内容 sha1，其它按 路径+大小+修改时间 参与。

    变化即换版本，与后端原来"取最大 mtime"的目的一致，但由前端自己算。
    """
    h = hashlib.sha1()
    for root, dirs, files in os.walk(src_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, src_dir).replace('\\', '/')
            st = os.stat(path)
            h.update(rel.encode('utf-8'))
            h.update(str(st.st_size).encode())
            if os.path.splitext(name)[1].lower() in TEXT_EXT and st.st_size < 8 * 1024 * 1024:
                with open(path, 'rb') as f:
                    h.update(f.read())
            else:
                h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()[:10]


def inject_api_base(config_path: str, api_base: str) -> None:
    """把 apiBase 写进 app.config.js（只改产物，不动源文件）。"""
    with io.open(config_path, encoding='utf-8') as f:
        text = f.read()
    new, n = re.subn(r"apiBase\s*:\s*'[^']*'", "apiBase: '%s'" % api_base, text, count=1)
    if n == 0:
        print('  !! 未在 app.config.js 里找到 apiBase 字段，跳过注入')
        return
    with io.open(config_path, 'w', encoding='utf-8', newline='') as f:
        f.write(new)
    print('  apiBase -> %s' % api_base)


def main() -> int:
    ap = argparse.ArgumentParser(description='把 frontend/ 打成纯静态包')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出目录（默认 dist/）')
    ap.add_argument('--api-base', default='', help='注入到 app.config.js 的后端地址')
    ap.add_argument('--keep', action='store_true', help='保留输出目录已有文件（默认先清空）')
    args = ap.parse_args()

    if not os.path.isdir(SRC_DIR):
        print('找不到 frontend/ 目录：%s' % SRC_DIR)
        return 2

    out = os.path.abspath(args.out)
    if out == os.path.abspath(SRC_DIR):
        print('输出目录不能是 frontend/ 本身')
        return 2

    version = compute_version(SRC_DIR)
    print('源目录 : %s' % SRC_DIR)
    print('输出   : %s' % out)
    print('指纹   : %s' % version)

    if os.path.isdir(out) and not args.keep:
        shutil.rmtree(out)
    shutil.copytree(SRC_DIR, out, dirs_exist_ok=True)

    # 替换 %V%
    index_path = os.path.join(out, 'index.html')
    with io.open(index_path, encoding='utf-8') as f:
        html = f.read()
    hits = html.count('%V%')
    html = html.replace('%V%', version)
    with io.open(index_path, 'w', encoding='utf-8', newline='') as f:
        f.write(html)
    print('  替换 %%V%% -> %s（%d 处）' % (version, hits))

    if args.api_base:
        inject_api_base(os.path.join(out, 'app.config.js'), args.api_base.rstrip('/'))

    # 自检：产物里不应再出现 %V%
    leftover = 0
    for root, _dirs, files in os.walk(out):
        for name in files:
            if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                continue
            with io.open(os.path.join(root, name), encoding='utf-8', errors='ignore') as f:
                if '%V%' in f.read():
                    leftover += 1
    total = sum(len(fs) for _r, _d, fs in os.walk(out))
    size = sum(os.path.getsize(os.path.join(r, n))
               for r, _d, fs in os.walk(out) for n in fs)
    print('  产物: %d 个文件, %.1f MB' % (total, size / 1048576.0))
    print('  残留 %%V%% 的文件数: %d（应为 0）' % leftover)
    return 0 if leftover == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
