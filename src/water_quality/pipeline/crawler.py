# -*- coding: utf-8 -*-
"""
crawler.py — 全国地表水水质自动监测「实时数据」采集器

目标站点
    中国环境监测总站 · 全国地表水水质自动监测实时数据
    主入口  https://szzdjc.cnemc.cn:8070/GJZ/Business/Publish/Main.html
    备用    https://szzdjc.cnemc.cn:8068/GJZ/Business/Publish/Main.html

本文件针对历史故障逐条做了定向修复（每条都有现场证据，见行内注释）
    A. URL 回归  —— 代码曾被改成 https://www.google.com，采集必然为空。
    B. 表头当数据 —— 数据库 raw_data 第 1 行的 station_name 就是「断面名称」。
    C. 省份标签错乱 —— 「上海市」名下 110 条记录全是海河流域断面（三小营、
       衡水湖大湖心…），证明省份切换从未成功，而循环变量被直接当成了数据。
    D. 死等 45 秒 —— 改为显式等待数据行出现。
    E. 失败不发声 —— 现在明确区分 DNS / TCP / HTTP / 无表头 / 0 条目标断面，
       并在启动浏览器之前做秒级网络预检，避免又跑十几分钟才发现一无所获。
    F. 任务卡死 —— 全程受 CRAWL_DEADLINE_SECONDS 约束，到点收尾。

⚠ 2026-10 现场结论：主机 106.37.208.243 能 ping 通（38ms），
   但 TCP 8070/8068/80/443 全部超时 —— 属服务端按来源网络做访问控制，
   或服务已迁移/下线。这是**网络侧问题，代码无法绕过**。
   请用 `--check` 一键分辨「网络不通」与「代码/DOM 问题」。

离线可测（解析与取数分离，解析路径与线上完全同一条代码）
    python -m water_quality.pipeline.crawler --check                 网络+站点结构预检
    python -m water_quality.pipeline.crawler --from-file page.html   用本地 HTML 回放解析
    python -m water_quality.pipeline.crawler --provinces 湖南省     只抓指定省
"""
import argparse
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd

from ..config import (
    CHROMEDRIVER_PATH,
    CHROME_DEBUG_PORT,
    CRAWLER_HEADLESS,
    CRAWLER_URL,
    CRAWLER_URL_FALLBACKS,
    CRAWL_PAGE_TIMEOUT,
    CRAWL_PROBE_TIMEOUT,
    CRAWL_DEADLINE_SECONDS,
    CRAWL_MAX_HTML_CHARS,
    RAW_DIR,
    now as config_now,
)

logger = logging.getLogger(__name__)

# ────────────────────────── 站点常量 ──────────────────────────

URL_CANDIDATES = [CRAWLER_URL] + [u for u in CRAWLER_URL_FALLBACKS if u != CRAWLER_URL]

# 需要遍历的省份（保持原有清单）
PROVINCES = ["上海市", "江苏省", "浙江省", "安徽省", "江西省", "河南省", "湖北省",
             "湖南省", "重庆市", "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省"]

# 目标字段
TARGET_HEADERS = ["省份", "流域", "断面名称", "监测时间", "水质类别",
                  "水温", "pH", "溶解氧", "电导率", "浊度",
                  "高锰酸盐", "氨氮", "总磷", "总氮"]

# 记录里除「断面名称 / 监测时间」外的可选指标字段
OPTIONAL_FIELDS = ["流域", "水质类别", "水温", "pH", "溶解氧", "电导率",
                   "浊度", "高锰酸盐", "氨氮", "总磷", "总氮"]

# 页面上的无效值标记：统一转成空串，由下游 to_numeric(errors='coerce') 变成 NaN。
# 旧实现把它们转成 '0.0'，等于把「缺测」伪造成「实测 0」，会污染模型输入。
# 故意用公开名：pipeline.ingest 处理外部文件时也要认同一套标记。
# 两处若各写一份，就会出现「导入路径把缺测当成 0」的口径分裂。
INVALID_TOKENS = {"-", "--", "", "None", "None ", "N/A", "—", "*", "/", "－"}


# ────────────────────────── 纯解析层（可离线测试）──────────────────────────

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _plain_text(html_fragment):
    """剥标签 + 解实体 + 压空白。"""
    s = _TAG_RE.sub(" ", html_fragment)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return _WS_RE.sub(" ", s).strip()


def rows_from_html(html):
    """把 HTML 里的 <tr> 全部抽成 [[单元格文本, ...], ...]。

    同时吃 <td> 与 <th>，因此在线页面（driver.page_source）与本地保存的
    HTML 文件走的是**同一条解析路径** —— 离线单测即等价于线上解析。

    正则是惰性的 (.*?)</tr>：遇到「有 <tr> 却没有 </tr>」的畸形 HTML 会退化成
    大量回溯，输入越大越慢。这里对超长输入截断防呆（正常页面远达不到该量级）。
    """
    html = html or ""
    if len(html) > CRAWL_MAX_HTML_CHARS:
        logger.warning("页面 HTML 达 %d 字符，超过上限 %d，截断后再解析",
                       len(html), CRAWL_MAX_HTML_CHARS)
        html = html[:CRAWL_MAX_HTML_CHARS]
    out = []
    for tr in _TR_RE.findall(html):
        cells = [_plain_text(td) for td in _TD_RE.findall(tr)]
        if any(c for c in cells):
            out.append(cells)
    return out


def detect_column_map(rows):
    """在前若干行里找出表头行，返回 {字段: 列号}。

    取「命中字段最多」的那一行，避免把某个数据行误判成表头。
    """
    best = {}
    for cells in rows[:5]:
        m = {}
        for idx, text in enumerate(cells):
            for target in TARGET_HEADERS:
                if target in text and target not in m:
                    m[target] = idx
        if len(m) > len(best):
            best = m
    return best


def _clean_value(text):
    """无效标记 -> 空串（而不是 '0.0'，避免把缺测伪造成实测值）。"""
    t = (text or "").strip()
    if t in INVALID_TOKENS:
        return ""
    return t


def _looks_like_timestamp(s):
    """'04-05 20:00' / '2026-04-05 20:00' / '2026-04-05' 都算时间。"""
    if not s:
        return False
    if re.search(r"\d{1,2}:\d{2}", s):
        return True
    return bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", s))


def row_to_record(cells, province, col_map):
    """把一行单元格转成记录；不是数据行则返回 None。

    这里有两道独立防线，专治「表头行被当成数据行」这一历史故障：
      防线 1：断面名称/监测时间单元格里出现表头字面量 -> 丢弃
      防线 2：监测时间不像时间 -> 丢弃
    """
    if not col_map:
        return None
    name_i = col_map.get("断面名称")
    time_i = col_map.get("监测时间")
    if name_i is None or time_i is None:
        return None
    if name_i >= len(cells) or time_i >= len(cells):
        return None

    section = (cells[name_i] or "").strip()
    m_time = (cells[time_i] or "").strip()

    # 防线 1
    if "断面名称" in section or "监测时间" in m_time:
        return None
    # 防线 2
    if not _looks_like_timestamp(m_time):
        return None
    if not section:
        return None

    record = {"省份": province, "断面名称": section, "监测时间": m_time}
    for fn in OPTIONAL_FIELDS:
        idx = col_map.get(fn)
        record[fn] = _clean_value(cells[idx]) if (idx is not None and idx < len(cells)) else ""
    return record


def records_from_rows(rows, province, col_map=None):
    """从行列表中抽取记录（表头行自动跳过）。"""
    cmap = col_map or detect_column_map(rows)
    out = []
    for cells in rows:
        rec = row_to_record(cells, province, cmap)
        if rec:
            out.append(rec)
    return out


def normalize_measure_time(raw, now=None):
    """把站点给出的「无年份」时间补成完整时间戳。

    站点给的是 'MM-DD HH:MM'（如 '04-05 20:00'），没有年份。直接交给 pandas
    会按「当前年」推断，跨年时段会整体错一年。这里显式补年：
    若推断出的日期明显晚于当前时间（>1 天），说明是去年的记录，年份减一。
    """
    now = now or config_now()
    s = (raw or "").strip()
    m = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$", s)
    if m:
        mm, dd, hh, mi = (int(x) for x in m.groups())
        try:
            cand = datetime(now.year, mm, dd, hh, mi)
        except ValueError:
            return s
        if cand > now + timedelta(days=1):
            try:
                cand = cand.replace(year=now.year - 1)
            except ValueError:
                return s
        return cand.strftime("%Y-%m-%d %H:%M")
    return s


# ────────────────────────── 网络预检层 ──────────────────────────

def _parse_target(url):
    p = urlparse(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return p.hostname, port


def probe_url(url, timeout=None):
    """逐层探测：DNS -> TCP -> HTTP。返回 (是否可用, 说明文本)。"""
    timeout = timeout or CRAWL_PROBE_TIMEOUT
    host, port = _parse_target(url)
    if not host:
        return False, "URL 无法解析出主机名: %s" % url

    # 1) DNS
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        dns_note = "%s -> %s" % (host, ", ".join(ips))
    except Exception as e:
        return False, "DNS 解析失败: %s" % e

    # 2) TCP
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            tcp_note = "TCP %d 通 (%.0f ms)" % (port, (time.time() - t0) * 1000)
    except Exception as e:
        return False, ("%s；但 %s（%s，%.0fs）→ 该主机的此端口不可达。"
                       "常见原因：服务端按来源网络做了访问控制，或服务已迁移/下线。"
                       % (dns_note, tcp_note_str(port), type(e).__name__,
                          time.time() - t0))

    # 3) HTTP
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # 兼容政府站点的证书链
    try:
        with urllib.request.urlopen(req, timeout=max(timeout, 20), context=ctx) as resp:
            body = resp.read(400000).decode("utf-8", "replace")
            note = "HTTP %s，页面 %d 字符，含表头「断面名称」=%s" % (
                resp.status, len(body), "断面名称" in body)
            return True, "%s；%s；%s" % (dns_note, tcp_note, note)
    except urllib.error.HTTPError as e:
        return False, "%s；%s；HTTP %s" % (dns_note, tcp_note, e.code)
    except Exception as e:
        return False, "%s；%s；HTTP 请求失败: %s" % (dns_note, tcp_note, e)


def tcp_note_str(port):
    return "TCP %d 不通" % port


def pick_reachable_url(urls=None, timeout=None):
    """返回第一个 TCP 可达的 URL；都不可达返回 None。"""
    for u in (urls or URL_CANDIDATES):
        host, port = _parse_target(u)
        try:
            with socket.create_connection((host, port), timeout=(timeout or CRAWL_PROBE_TIMEOUT)):
                return u
        except Exception:
            continue
    return None


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")


# ────────────────────────── 浏览器采集层 ──────────────────────────

def _make_driver(headless=None):
    """构造 Chrome driver：优先用配置的 chromedriver，否则 webdriver-manager 自动匹配。"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    headless = CRAWLER_HEADLESS if headless is None else headless
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--remote-debugging-port=%d" % CHROME_DEBUG_PORT)
    opts.add_argument("user-agent=" + _UA)
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    if CHROMEDRIVER_PATH:
        if not os.path.exists(CHROMEDRIVER_PATH):
            raise FileNotFoundError("配置的 chromedriver 不存在: %s" % CHROMEDRIVER_PATH)
        service = Service(CHROMEDRIVER_PATH)
    else:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _enter_data_frame(driver):
    """切到含「断面名称」的那个 frame（含顶层文档）。成功返回 True。"""
    from selenium.webdriver.common.by import By

    driver.switch_to.default_content()
    if "断面名称" in driver.page_source:
        return True
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except Exception:
        frames = []
    for i in range(len(frames)):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(i)
            if "断面名称" in driver.page_source:
                logger.info("    已进入 iframe[%d]（含数据表）", i)
                return True
        except Exception:
            continue
    driver.switch_to.default_content()
    return False


def _wait_for_table(driver, timeout=None):
    """显式等待数据表出现，而不是死等固定秒数。"""
    timeout = timeout or CRAWL_PAGE_TIMEOUT
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _enter_data_frame(driver)
            html = driver.page_source
        except Exception:
            time.sleep(1)
            continue
        rows = rows_from_html(html)
        cmap = detect_column_map(rows)
        if cmap.get("断面名称") is not None and cmap.get("监测时间") is not None:
            if records_from_rows(rows, "_probe_", cmap):
                return rows, cmap
        time.sleep(1.5)
    return None, None


def _province_anchors(driver):
    """页面上所有文本等于某个省名的 <a>。"""
    from selenium.webdriver.common.by import By

    found = {}
    try:
        for a in driver.find_elements(By.TAG_NAME, "a"):
            try:
                t = (a.text or "").strip()
            except Exception:
                continue
            if t in PROVINCES and t not in found:
                found[t] = a
    except Exception:
        pass
    return found


def _current_selection(driver):
    """读取页面上省份选择器当前显示的文本（用于校验切换是否真的生效）。"""
    from selenium.webdriver.common.by import By

    for sel in ("#provinceTitle", "button.dropdown-toggle", ".dropdown-toggle"):
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = (el.text or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def _switch_province(driver, province, wait=None):
    """切到指定省份，并**校验切换成功**。

    返回 (ok, detail)。ok=False 时调用方必须跳过该省 —— 这正是历史故障 C 的根治点：
    旧代码不校验，切换失败后依旧把循环变量当成数据来源，于是把海河流域的断面
    成批错标成「上海市」写进了数据库。
    """
    wait = wait or 20
    if province in _current_selection(driver):
        return True, "页面已在该省"

    anchors = _province_anchors(driver)
    if not anchors:
        return False, "页面上找不到任何省份链接（下拉菜单结构可能已改版）"

    anchor = anchors.get(province)
    if anchor is None:
        return False, "下拉菜单里没有「%s」（现有: %s）" % (
            province, "、".join(sorted(anchors)[:8]))

    # 需要时先展开它所属的下拉菜单
    try:
        if not anchor.is_displayed():
            driver.execute_script(
                "var a=arguments[0];var p=a.closest('.dropdown')||a.parentElement;"
                "var t=p?p.querySelector('.dropdown-toggle'):null;if(t)t.click();",
                anchor)
            time.sleep(0.6)
            anchors = _province_anchors(driver)
            anchor = anchors.get(province) or anchor
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", anchor)
        anchor.click()
    except Exception as e:
        return False, "点击失败: %s" % e

    # 轮询校验
    deadline = time.time() + wait
    while time.time() < deadline:
        if province in _current_selection(driver):
            return True, "已切换"
        time.sleep(0.5)
    return False, "点击后 %d 秒内页面未切到「%s」（当前显示：%s）" % (
        wait, province, _current_selection(driver) or "未知")


def _collect_rows(driver, deadline, quiet_rounds=4, scroll_pause=1.2):
    """滚动并反复取行，直到连续若干轮没有新行或到达总时限。"""
    from selenium.webdriver.common.by import By

    seen = set()
    rows = []
    quiet = 0
    while quiet < quiet_rounds and time.time() < deadline:
        try:
            driver.execute_script("window.scrollBy(0, 600);")
        except Exception:
            pass
        time.sleep(scroll_pause)
        try:
            html = driver.page_source
        except Exception:
            break
        cur = rows_from_html(html)
        added = 0
        for cells in cur:
            key = tuple(cells)
            if key not in seen:
                seen.add(key)
                rows.append(cells)
                added += 1
        # 兜底：表格可能是分页/懒加载的，尝试点「下一页」
        if added == 0:
            quiet += 1
            if quiet == 1:
                try:
                    for btn in driver.find_elements(By.PARTIAL_LINK_TEXT, "下一页"):
                        if btn.is_displayed() and btn.is_enabled():
                            btn.click()
                            time.sleep(1.5)
                            quiet = 0
                            break
                except Exception:
                    pass
        else:
            quiet = 0
    return rows


def crawl(url=None, provinces=None, headless=None, deadline_seconds=None):
    """执行一次采集，返回记录列表（dict 列表，键与 db.insert_raw_data 对齐）。"""
    provinces = provinces or PROVINCES
    deadline = time.time() + (deadline_seconds or CRAWL_DEADLINE_SECONDS)
    urls = [url] if url else URL_CANDIDATES

    logger.info("水质全指标抓取任务启动")

    # ── 预检：秒级分辨「网络不通」还是「站点可访问」 ──
    target = pick_reachable_url(urls)
    if not target:
        for u in urls:
            ok, detail = probe_url(u)
            logger.error("预检失败 [%s] %s", "可达" if ok else "不可达", detail)
        logger.error("所有候选入口均不可达，已放弃采集。"
                     "请用 `python -m water_quality.pipeline.crawler --check` 查看完整诊断；"
                     "若确认是网络限制，需要在可访问该站点的网络环境下运行（或改用其它数据源）。")
        return []
    logger.info("预检通过，使用入口: %s", target)

    driver = None
    try:
        driver = _make_driver(headless)
        logger.info("正在访问: %s", target)
        driver.get(target)

        rows, cmap = _wait_for_table(driver)
        if not rows:
            logger.error("页面已打开，但 %d 秒内未等到可解析的数据表。"
                         "DOM 结构可能已改版，请用 `--from-file` 回放人工保存的页面来定位。",
                         CRAWL_PAGE_TIMEOUT)
            return []
        logger.info("数据表就绪：识别到 %d 个表头字段（断面名称=第%s列，监测时间=第%s列）",
                    len(cmap), cmap.get("断面名称"), cmap.get("监测时间"))

        results = {}
        failed_provinces = []
        for prov in provinces:
            if time.time() > deadline:
                logger.warning("已超出总时限 %ds，提前结束采集", CRAWL_DEADLINE_SECONDS)
                break
            ok, detail = _switch_province(driver, prov)
            if not ok:
                # 关键：切换失败就不采集，绝不把数据错标成该省
                logger.warning("  %s 切换失败（%s），跳过该省", prov, detail)
                failed_provinces.append(prov)
                continue

            rows = _collect_rows(driver, deadline)
            recs = records_from_rows(rows, prov, cmap)
            new = 0
            for r in recs:
                r["监测时间"] = normalize_measure_time(r["监测时间"])
                key = "%s_%s" % (r["断面名称"], r["监测时间"])
                if key not in results:
                    results[key] = r
                    new += 1
            logger.info("  %s 扫描结束：解析 %d 行，新增 %d 条（累计 %d）",
                        prov, len(rows), new, len(results))

        if failed_provinces:
            logger.warning("有 %d 个省份切换失败被跳过：%s",
                           len(failed_provinces), "、".join(failed_provinces))
        logger.info("采集完成，共 %d 条唯一记录", len(results))
        return list(results.values())

    except Exception as e:
        logger.error("采集过程异常: %s", e, exc_info=True)
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ────────────────────────── 兼容入口（clean.py 依赖）──────────────────────────

def get_yangtze_final_mission():
    """保持原有函数名，供 pipeline.clean 调用。"""
    return crawl()


# ────────────────────────── 诊断与离线回放 ──────────────────────────

def check(urls=None):
    """采集链路自检：逐层打印 DNS / TCP / HTTP 结果，并给出结论。"""
    urls = urls or URL_CANDIDATES
    print("=" * 70)
    print(" 采集链路自检 · 目标：全国地表水水质自动监测实时数据")
    print("=" * 70)
    reachable = []
    for u in urls:
        host, port = _parse_target(u)
        print("\n目标: %s" % u)
        print("  主机/端口: %s:%d" % (host, port))
        ok, detail = probe_url(u)
        print("  [%s] %s" % ("可达" if ok else "不可达", detail))
        if ok:
            reachable.append(u)
    print("\n" + "-" * 70)
    if reachable:
        print("结论：站点可达，可以采集。建议先跑 --from-file 之外的正式采集：")
        print("      python -m water_quality.pipeline.crawler --provinces 湖南省")
    else:
        print("结论：所有候选入口在 TCP 层即不可达 —— 这是网络/服务侧问题，非本程序 bug。")
        print("      判断依据：若 `ping <主机>` 通而 TCP 不通，说明服务端按来源网络做了")
        print("      访问控制，或服务已迁移/下线。需要换到可访问该站点的网络环境。")
    print("=" * 70)
    return bool(reachable)


def replay_file(path, province="湖南省", ingest=False):
    """用本地保存的 HTML 回放解析流程（离线定位 DOM/解析问题）。

    ingest=True 时复用 pipeline.clean.ingest_records()，把结果真正写入数据库
    并刷新 data/processed/*.csv —— 这样即使站点暂时不可达，也能靠
    「浏览器另存页面 -> 离线入库」把数据链路走通。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    rows = rows_from_html(html)
    cmap = detect_column_map(rows)
    recs = records_from_rows(rows, province, cmap)
    for r in recs:
        r["监测时间"] = normalize_measure_time(r["监测时间"])

    print("=" * 70)
    print(" 本地 HTML 回放解析")
    print("=" * 70)
    print("文件      : %s" % path)
    print("总 <tr> 数 : %d" % len(rows))
    print("识别表头   : %s" % (("命中 %d 个字段" % len(cmap)) if cmap else "未识别到表头"))
    if cmap:
        print("字段列号   : %s" % json.dumps(cmap, ensure_ascii=False))
    print("抽到记录   : %d 条" % len(recs))
    for r in recs[:5]:
        print("   %s | %s | TN=%s PH=%s" % (r.get("断面名称"), r.get("监测时间"),
                                            r.get("总氮"), r.get("pH")))

    if ingest:
        if not recs:
            print("没有可入库的记录。")
        else:
            # 延迟导入：clean 模块顶部会 import 本模块，模块级互相 import 会成环
            from .clean import ingest_records
            stats = ingest_records(recs)
            print("入库结果   : %s" % stats)
            print("            （raw_data 写入 %d 行，刷新 %d 个断面 CSV）"
                  % (stats.get("inserted", 0), stats.get("updated_csv", 0)))
    print("=" * 70)
    return recs


def main(argv=None):
    ap = argparse.ArgumentParser(description="全国地表水水质数据采集器")
    ap.add_argument("--check", action="store_true", help="只做网络与站点预检，不启动浏览器")
    ap.add_argument("--from-file", metavar="HTML", help="用本地保存的 HTML 回放解析（离线）")
    ap.add_argument("--province-for-file", default="湖南省", help="--from-file 时使用的省份标签")
    ap.add_argument("--ingest", action="store_true",
                    help="配合 --from-file：把解析结果真正写入数据库并刷新 CSV")
    ap.add_argument("--provinces", nargs="*", help="只采集指定省份（默认全部）")
    ap.add_argument("--url", help="覆盖采集入口")
    ap.add_argument("--no-headless", action="store_true", help="显示浏览器窗口（调试用）")
    ap.add_argument("--deadline", type=int, help="本次采集总时限（秒）")
    ap.add_argument("--save-raw", action="store_true", help="把原始记录另存为 data/raw/YYYY_MM_DD.csv")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.check:
        return 0 if check() else 1

    if args.from_file:
        recs = replay_file(args.from_file, args.province_for_file, ingest=args.ingest)
        if not args.ingest:
            print("\n（本次仅解析、未写入。要真正入库并刷新 CSV，请再加 --ingest）")
        return 0 if recs else 1

    data = crawl(url=args.url, provinces=args.provinces,
                 headless=(False if args.no_headless else None),
                 deadline_seconds=args.deadline)

    if not data:
        logger.error("未采集到任何数据。")
        return 1

    if args.save_raw:
        os.makedirs(RAW_DIR, exist_ok=True)
        df = pd.DataFrame(data).drop_duplicates(subset=["断面名称", "监测时间"])
        out = os.path.join(RAW_DIR, config_now().strftime("%Y_%m_%d") + ".csv")
        df.to_csv(out, index=False, encoding="utf_8_sig")
        logger.info("原始记录已另存: %s（%d 条）", out, len(df))

    logger.info("共采集 %d 条记录", len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
