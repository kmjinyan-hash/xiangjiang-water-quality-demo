import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta

from .config import AI_REPORT_MAX_ITEMS, DB_PATH, ensure_runtime_dirs

ensure_runtime_dirs()

logger = logging.getLogger(__name__)

# ─────────────── 表结构 ───────────────
# 建表语句集中在这里，写入路径也用它 —— 原因见 _ensure_schema 的注释。
_SCHEMA_DDL = (
    '''
    CREATE TABLE IF NOT EXISTS raw_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        province TEXT,
        basin TEXT,
        station_name TEXT,
        measure_time TEXT,
        water_quality_category TEXT,
        tem TEXT,
        ph TEXT,
        do TEXT,
        tds TEXT,
        ntu TEXT,
        codmn TEXT,
        nh3n TEXT,
        tp TEXT,
        tn TEXT,
        UNIQUE(station_name, measure_time)
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS predictions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        station      TEXT    NOT NULL,
        predict_date TEXT    NOT NULL,   -- D+1: the date being predicted
        base_date    TEXT,               -- D:   last known real-data date
        predicted_TN REAL,
        actual_TN    REAL,               -- real TN on base_date (last known at inference time)
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(station, predict_date)
    )
    ''',
    # AI 预警报告。原先只存在 app.py 的内存列表里，进程一重启全丢 ——
    # 而「报告」恰恰是需要留存和追溯的东西，落库才是对的。
    # indicators 用 JSON 存（逐项判定结果是一个小数组，单独建表不值得）。
    '''
    CREATE TABLE IF NOT EXISTS ai_reports (
        id          TEXT PRIMARY KEY,    -- uuid4 hex[:12]，由上层生成
        station     TEXT NOT NULL,
        report_date TEXT,
        source      TEXT,
        title       TEXT,
        overall     TEXT,                -- normal / warning / alarm / unknown
        summary     TEXT,
        content     TEXT,                -- 报告正文
        indicators  TEXT,                -- JSON 数组：逐项抽取与判定结果
        created_at  TEXT,
        generated_by TEXT DEFAULT 'rules',  -- llm / rules：结论由谁生成
        llm_meta    TEXT                 -- JSON：模型名、耗时、token
    )
    ''',
    # 用户新增的站点（负责人要求"后续有其他站点加入，可以直接在系统里增加"）。
    # 内置的 14 个断面仍然写在前端 stations.js —— 这里只存"额外新增"的，
    # 所以新增/删除都不会动到原始台账，内置站点也不会因为误删而消失。
    '''
    CREATE TABLE IF NOT EXISTS custom_stations (
        id          TEXT PRIMARY KEY,    -- st90 起编号，避开内置 st0–st14
        name        TEXT NOT NULL,
        river       TEXT,
        city        TEXT,
        lon         REAL NOT NULL,
        lat         REAL NOT NULL,
        role        TEXT,                -- target / upstream
        created_at  TEXT
    )
    ''',
    # 列表按 created_at 倒序、同秒按 rowid 兜底，避免同一秒内多份报告顺序不稳
    'CREATE INDEX IF NOT EXISTS idx_ai_reports_created ON ai_reports(created_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_ai_reports_station ON ai_reports(station)',
    # ── 登录鉴权（2026-10 加回，安全事件后由开发方要求）──
    # 密码只存哈希，永不存明文；role: admin / viewer。
    '''
    CREATE TABLE IF NOT EXISTS users (
        id          TEXT PRIMARY KEY,
        username    TEXT NOT NULL UNIQUE,
        pwd_hash    TEXT NOT NULL,       -- werkzeug.security 的哈希串（自带算法与盐）
        role        TEXT NOT NULL DEFAULT 'viewer',
        disabled    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT,
        last_login  TEXT
    )
    ''',
    # 会话放服务端而不是签名 cookie：可以主动踢人、能看在线设备、能一键失效。
    # 表里只存 token 的 sha256 —— 库被看到也拿不到可用的 token。
    '''
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash  TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        created_at  TEXT,
        expires_at  TEXT,
        ip          TEXT,
        ua          TEXT
    )
    ''',
    'CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at)',
    # 按站点授权：一个用户能看到哪些断面。
    # 不在这张表里的用户 = 没有任何站点权限（viewer 默认看不见数据，需要显式分配）。
    # admin 不受此表限制（全部可见），判断在 permissions.py 里集中一处。
    '''
    CREATE TABLE IF NOT EXISTS user_stations (
        user_id     TEXT NOT NULL,
        station_id  TEXT NOT NULL,
        PRIMARY KEY (user_id, station_id)
    )
    ''',
    'CREATE INDEX IF NOT EXISTS idx_user_stations_user ON user_stations(user_id)',
)


# ─────────────── 用户与会话 ───────────────

def count_users():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        return cur.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    finally:
        conn.close()


def create_user(username, pwd_hash, role='viewer'):
    """新建用户；用户名重复返回 False（不抛异常，调用方给出友好提示）。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        try:
            cur.execute(
                'INSERT INTO users (id, username, pwd_hash, role, disabled, created_at) '
                'VALUES (?, ?, ?, ?, 0, ?)',
                (uuid.uuid4().hex[:12], username.strip().lower(), pwd_hash,
                 role if role in ('admin', 'viewer') else 'viewer', _now()))
        except sqlite3.IntegrityError:
            return False
        conn.commit()
        return True
    finally:
        conn.close()


def find_user(username):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        row = cur.execute('SELECT * FROM users WHERE username = ?',
                          (str(username or '').strip().lower(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user(uid):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        row = cur.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        sql = ('SELECT id, username, role, disabled, created_at, last_login '
               'FROM users ORDER BY created_at')
        return [dict(r) for r in cur.execute(sql).fetchall()]
    finally:
        conn.close()


def set_user_role(uid, role):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('UPDATE users SET role = ? WHERE id = ?',
                    (role if role in ('admin', 'viewer') else 'viewer', uid))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_user_password(uid, pwd_hash):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('UPDATE users SET pwd_hash = ? WHERE id = ?', (pwd_hash, uid))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_user(uid):
    """删用户时连带清掉他的会话，避免已删账号还留着有效 token。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('DELETE FROM sessions WHERE user_id = ?', (uid,))
        cur.execute('DELETE FROM users WHERE id = ?', (uid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def touch_login(uid):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('UPDATE users SET last_login = ? WHERE id = ?', (_now(), uid))
        conn.commit()
    finally:
        conn.close()


def create_session(token_hash, user_id, expires_at, ip='', ua=''):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute(
            'INSERT OR REPLACE INTO sessions '
            '(token_hash, user_id, created_at, expires_at, ip, ua) VALUES (?, ?, ?, ?, ?, ?)',
            (token_hash, user_id, _now(), expires_at, (ip or '')[:64], (ua or '')[:200]))
        conn.commit()
    finally:
        conn.close()


def get_session(token_hash):
    """返回 (user, session) 或 (None, None)。过期即视为无效并顺手删除。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        row = cur.execute('SELECT * FROM sessions WHERE token_hash = ?',
                          (token_hash,)).fetchone()
        if not row:
            return None, None
        sess = dict(row)
        if sess.get('expires_at') and sess['expires_at'] < _now():
            cur.execute('DELETE FROM sessions WHERE token_hash = ?', (token_hash,))
            conn.commit()
            return None, None
        u = cur.execute('SELECT * FROM users WHERE id = ?', (sess['user_id'],)).fetchone()
        if not u or u['disabled']:
            return None, None
        return dict(u), sess
    finally:
        conn.close()


def delete_session(token_hash):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('DELETE FROM sessions WHERE token_hash = ?', (token_hash,))
        conn.commit()
    finally:
        conn.close()


def purge_expired_sessions():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('DELETE FROM sessions WHERE expires_at < ?', (_now(),))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _now():
    """本地时间字符串，与表里其他时间字段格式一致（可比较）。"""
    from .config import now
    return now().strftime('%Y-%m-%dT%H:%M:%S')


# ─────────────── 按站点授权 ───────────────

def get_user_stations(user_id):
    """返回该用户被授权的断面 id 列表（顺序稳定）。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        rows = cur.execute(
            'SELECT station_id FROM user_stations WHERE user_id = ? ORDER BY station_id',
            (user_id,)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def set_user_stations(user_id, station_ids):
    """整体替换某个用户的断面授权（先删后插，一次事务）。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('DELETE FROM user_stations WHERE user_id = ?', (user_id,))
        seen = set()
        for sid in station_ids or []:
            sid = str(sid or '').strip().lower()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            cur.execute('INSERT INTO user_stations (user_id, station_id) VALUES (?, ?)',
                        (user_id, sid))
        conn.commit()
        return sorted(seen)
    finally:
        conn.close()


def list_all_station_grants():
    """{user_id: [station_id...]}，管理界面一次取全，避免 N+1 查询。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        out = {}
        for uid, sid in cur.execute(
                'SELECT user_id, station_id FROM user_stations').fetchall():
            out.setdefault(uid, []).append(sid)
        for v in out.values():
            v.sort()
        return out
    finally:
        conn.close()


def _ensure_schema(cursor):
    """确保所需表都存在（幂等）。

    为什么要放进写入路径，而不是只在应用启动时建一次：
      insert_raw_data 原先假定 init_db() 已经跑过。但 pipeline.ingest 是一个
      **独立 CLI**，可以直连一个全新的数据库 —— 例如在全新容器里挂了个空数据卷，
      直接 `python -m water_quality.pipeline.ingest --csv x.csv`，就会撞上
      `sqlite3.OperationalError: no such table: raw_data`（已实测复现）。
      建表是 CREATE TABLE IF NOT EXISTS，代价可忽略，放在这里最稳。
    """
    for ddl in _SCHEMA_DDL:
        cursor.execute(ddl)
    _migrate_columns(cursor)


# 老库升级：CREATE TABLE IF NOT EXISTS 不会给**已存在**的表补列，
# 所以新增字段必须显式 ALTER。按 pragma 检查，可重复执行、不会重复加列。
_COLUMN_MIGRATIONS = {
    'ai_reports': [
        ('generated_by', "TEXT DEFAULT 'rules'"),   # llm / rules
        ('llm_meta', 'TEXT'),                        # JSON：模型、耗时、token
    ],
}


def _migrate_columns(cursor):
    for table, cols in _COLUMN_MIGRATIONS.items():
        try:
            have = {r[1] for r in cursor.execute('PRAGMA table_info(%s)' % table).fetchall()}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for name, decl in cols:
            if name in have:
                continue
            try:
                cursor.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, decl))
                logger.info('已为 %s 补充字段 %s', table, name)
            except sqlite3.Error as e:
                logger.warning('补字段 %s.%s 失败：%s', table, name, e)


def _normalize_report_row(d):
    """把库行整理成接口结构：llm_meta(JSON 字符串) → llm(dict)。"""
    raw = d.pop('llm_meta', None)
    try:
        d['llm'] = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        d['llm'] = None
    d['generated_by'] = d.get('generated_by') or 'rules'
    return d


def get_db_connection():
    """新建一个数据库连接（调用方负责 close）。

    刻意**不共享**连接：gunicorn 的线程模型下跨线程复用 sqlite3 连接是线程不安全的，
    而本项目的单次请求只做一两次查询，建连接的开销可忽略。

    WAL 模式让「读」不再阻塞「写」——否则调度器在后台写库时，
    前端接口会遇到 "database is locked"。busy_timeout 作为二次兜底。
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
    except sqlite3.Error:
        pass                      # 只读文件系统等极端情况下不阻断主流程
    return conn


def init_db():
    conn = get_db_connection()
    try:
        _ensure_schema(conn.cursor())
        conn.commit()
        logger.info("Database tables initialized successfully.")
    finally:
        conn.close()


def insert_raw_data(data_list):
    """批量写入原始记录，返回真正新增的行数（重复的按 UNIQUE 约束忽略）。

    缺失处理必须写空串，**绝不能写 "0.0"**：
    那等于把「这项没测」伪造成「测出来是 0」，是本项目历史上最严重的数据缺陷
    （旧爬虫的 _clean_text 就是这么干的，导致异常值污染训练数据）。
    上游记录若缺某个指标键，这里必须落空串，由下游统一变 NaN。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        _ensure_schema(cursor)          # CLI 直连新库时表可能还不存在
        inserted_count = 0
        for item in data_list:
            try:
                cursor.execute('''
                    INSERT INTO raw_data (
                        province, basin, station_name, measure_time,
                        water_quality_category, tem, ph, do, tds, ntu, codmn, nh3n, tp, tn
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item.get("省份") or "", item.get("流域") or "",
                    item.get("断面名称") or "", item.get("监测时间") or "",
                    item.get("水质类别") or "",
                    item.get("水温") or "", item.get("pH") or "",
                    item.get("溶解氧") or "", item.get("电导率") or "",
                    item.get("浊度") or "", item.get("高锰酸盐") or "",
                    item.get("氨氮") or "", item.get("总磷") or "",
                    item.get("总氮") or "",
                ))
                inserted_count += 1
            except sqlite3.IntegrityError:
                # Ignore duplicates
                continue
        conn.commit()
        return inserted_count
    finally:
        conn.close()


def save_predictions(results: dict):
    """Persist run_prediction() results to the predictions table."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        _ensure_schema(cursor)
        for station, d in results.items():
            base_date = d.get('last_recorded_date', '')
            try:
                predict_date = (datetime.strptime(base_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
            except Exception:
                predict_date = base_date
            # INSERT OR REPLACE so re-running same day overwrites cleanly
            cursor.execute('''
                INSERT OR REPLACE INTO predictions
                    (station, predict_date, base_date, predicted_TN, actual_TN)
                VALUES (?, ?, ?, ?, ?)
            ''', (station, predict_date, base_date, d.get('predicted_TN'), d.get('actual_TN')))
        conn.commit()
    finally:
        conn.close()


def get_prediction_history(station: str, limit: int = 60):
    """Return up to `limit` rows for `station` ordered oldest-first (for chart rendering)."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            '''SELECT station, predict_date, base_date, predicted_TN, actual_TN, created_at
               FROM predictions WHERE station = ?
               ORDER BY predict_date ASC LIMIT ?''',
            (station, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────── AI 预警报告 ───────────────
# 原先是 app.py 里的一个内存列表（上限 200），进程重启即全丢。
# 报告是需要留存追溯的东西，改为落库；保留条数上限依然生效（超出删最旧的）。

def insert_ai_report(report, max_items=None):
    """写入一份 AI 报告；max_items 为保留上限，超出的按时间删最旧。"""
    max_items = AI_REPORT_MAX_ITEMS if max_items is None else max_items
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('''
            INSERT OR REPLACE INTO ai_reports
                (id, station, report_date, source, title, overall, summary,
                 content, indicators, created_at, generated_by, llm_meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            report['id'], report['station'], report.get('report_date', ''),
            report.get('source', ''), report.get('title', ''),
            report.get('overall', ''), report.get('summary', ''),
            report.get('content', ''),
            json.dumps(report.get('indicators') or [], ensure_ascii=False),
            report.get('created_at', ''),
            report.get('generated_by') or 'rules',
            json.dumps(report.get('llm'), ensure_ascii=False) if report.get('llm') else None,
        ))
        if max_items and max_items > 0:
            cur.execute('''
                DELETE FROM ai_reports WHERE id NOT IN (
                    SELECT id FROM ai_reports ORDER BY created_at DESC, rowid DESC LIMIT ?
                )
            ''', (max_items,))
        conn.commit()
    finally:
        conn.close()


def list_ai_reports(station=None, limit=50, light=True):
    """按时间倒序列出报告。light=True 时不返回正文与逐项明细（列表用）。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        sql = 'SELECT * FROM ai_reports'
        params = []
        if station:
            sql += ' WHERE station = ?'
            params.append(station)
        sql += ' ORDER BY created_at DESC, rowid DESC LIMIT ?'
        params.append(limit)
        out = []
        for r in cur.execute(sql, params).fetchall():
            d = _normalize_report_row(dict(r))
            d['indicators'] = json.loads(d.get('indicators') or '[]')
            if light:
                d.pop('content', None)
                d.pop('indicators', None)
            out.append(d)
        return out
    finally:
        conn.close()


def get_ai_report(rid):
    """取单份报告（含正文与逐项明细）；不存在返回 None。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        r = cur.execute('SELECT * FROM ai_reports WHERE id = ?', (rid,)).fetchone()
        if not r:
            return None
        d = _normalize_report_row(dict(r))
        d['indicators'] = json.loads(d.get('indicators') or '[]')
        return d
    finally:
        conn.close()


def delete_ai_report(rid):
    """删除一份报告；返回是否真的删掉了。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('DELETE FROM ai_reports WHERE id = ?', (rid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 用户新增站点
#
# 内置 14 个断面写在前端 stations.js（原始台账，不动）。这里只管理
# "额外新增"的站点：新增/删除只影响这张表，内置断面永远不受影响。
# ══════════════════════════════════════════════════════════════

def list_custom_stations():
    """返回全部用户新增站点，按创建时间升序。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('SELECT id, name, river, city, lon, lat, role, created_at '
                    'FROM custom_stations ORDER BY created_at ASC')
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def next_custom_station_id():
    """生成下一个可用编号：内置是 st0–st14，新增从 st90 起往后排。

    特意与内置编号隔开一段，避免任何情况下与已有站点撞号。
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('SELECT id FROM custom_stations')
        used = {r[0] for r in cur.fetchall()}
        n = 90
        while ('st%d' % n) in used:
            n += 1
        return 'st%d' % n
    finally:
        conn.close()


def add_custom_station(rec):
    """新增一个站点。rec 需含 name/lon/lat，其余可选；返回落库后的完整记录。"""
    from datetime import datetime
    sid = (rec.get('id') or '').strip() or next_custom_station_id()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        row = (sid,
               (rec.get('name') or '').strip(),
               (rec.get('river') or '').strip(),
               (rec.get('city') or '').strip(),
               float(rec['lon']),
               float(rec['lat']),
               (rec.get('role') or 'upstream').strip(),
               datetime.now().strftime('%Y-%m-%dT%H:%M:%S'))
        cur.execute('INSERT INTO custom_stations '
                    '(id, name, river, city, lon, lat, role, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', row)
        conn.commit()
        return dict(zip(('id', 'name', 'river', 'city', 'lon', 'lat', 'role', 'created_at'), row))
    finally:
        conn.close()


def delete_custom_station(sid):
    """删除一个用户新增站点；返回是否真的删掉了。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema(cur)
        cur.execute('DELETE FROM custom_stations WHERE id = ?', (sid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()