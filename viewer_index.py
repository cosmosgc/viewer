"""SQLite-backed media index for fast gallery loads.

Replaces per-request ``os.walk`` + ``stat`` + N per-file lookup queries with
a single indexed SQL query. The index lives in the same sqlite file as the
reverse-lookup cache (``reverse_lookup.sqlite3``) under the ``media_index``
table.

Sync strategy: a full filesystem walk still happens, but only in a
background thread (startup + periodic + explicit rescan + incremental hooks
after upload/ingest/import/delete). Page loads never walk the filesystem.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_index (
    rel_path TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    year TEXT NOT NULL DEFAULT '',
    month TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    sort_ts REAL NOT NULL DEFAULT 0,
    display_dt TEXT NOT NULL DEFAULT '',
    name_lower TEXT NOT NULL DEFAULT '',
    rel_lower TEXT NOT NULL DEFAULT '',
    has_lookup_data INTEGER NOT NULL DEFAULT 0,
    up_score REAL NOT NULL DEFAULT 0,
    down_score REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_scope ON media_index(year, month, day);
CREATE INDEX IF NOT EXISTS idx_media_kind ON media_index(kind);
CREATE INDEX IF NOT EXISTS idx_media_sort_ts ON media_index(sort_ts);
CREATE INDEX IF NOT EXISTS idx_media_size ON media_index(size_bytes);
CREATE INDEX IF NOT EXISTS idx_media_name ON media_index(file_name);
CREATE INDEX IF NOT EXISTS idx_media_up ON media_index(up_score);
CREATE INDEX IF NOT EXISTS idx_media_down ON media_index(down_score);
CREATE INDEX IF NOT EXISTS idx_media_rel_lower ON media_index(rel_lower);
CREATE INDEX IF NOT EXISTS idx_media_dir ON media_index(dir_path);
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

_lock = threading.Lock()
_sync_state = {"running": False, "last_error": "", "started_at": None}


def _paths():
    from viewer_context import LOOKUP_DB, RESULT_DIR

    return RESULT_DIR, LOOKUP_DB


def connect():
    _, db_path = _paths()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def ensure_schema():
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def get_meta(key, default=""):
    ensure_schema()
    with _lock, connect() as conn:
        row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value):
    ensure_schema()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO index_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()


def get_status():
    with _lock, connect() as conn:
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM media_index").fetchone()["c"]
        except Exception:
            count = 0
    return {
        "running": _sync_state["running"],
        "started_at": _sync_state["started_at"],
        "last_error": _sync_state["last_error"],
        "last_sync": get_meta("last_sync"),
        "last_duration_s": get_meta("last_duration_s"),
        "file_count": count,
    }


def _row_for_file(p, rel_path, stat_res, parse_dt_from_name, dt_module):
    from viewer_support import media_type_for_ext

    kind = media_type_for_ext(p.suffix.lower())
    if kind == "other":
        return None
    parts = Path(rel_path).parts
    year = parts[0] if len(parts) > 0 else ""
    month = parts[1] if len(parts) > 1 else ""
    day = parts[2] if len(parts) > 2 else ""
    parsed = parse_dt_from_name(p.name)
    sort_ts = parsed.timestamp() if parsed else stat_res.st_mtime
    if parsed:
        display_dt = parsed.strftime("%Y-%m-%d %H:%M:%S")
    else:
        display_dt = dt_module.datetime.fromtimestamp(stat_res.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "rel_path": rel_path,
        "file_name": p.name,
        "dir_path": str(Path(rel_path).parent).replace("\\", "/"),
        "kind": kind,
        "year": year,
        "month": month,
        "day": day,
        "size_bytes": stat_res.st_size,
        "mtime": stat_res.st_mtime,
        "sort_ts": sort_ts,
        "display_dt": display_dt,
        "name_lower": p.name.lower(),
        "rel_lower": rel_path.lower(),
    }


def _lookup_map(conn, rel_paths):
    """Batch-fetch lookup flags/scores for rel_paths. Single round-trip per chunk."""
    out = {}
    chunk = 800
    for i in range(0, len(rel_paths), chunk):
        batch = rel_paths[i : i + chunk]
        qmarks = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT rel_path, result_json, summary_json FROM reverse_lookup_cache WHERE rel_path IN ({qmarks})",
            batch,
        ).fetchall()
        for r in rows:
            try:
                result = json.loads(r["result_json"]) if r["result_json"] else {}
            except Exception:
                result = {}
            try:
                summary = json.loads(r["summary_json"]) if r["summary_json"] else {}
            except Exception:
                summary = {}
            has_data = 0
            if isinstance(result, dict):
                has_data = 0 if (isinstance(result, dict) and result.get("error")) else 1
            elif result:
                has_data = 1
            try:
                up = float(summary.get("up_score") or 0)
            except Exception:
                up = 0
            try:
                down = float(summary.get("down_score") or 0)
            except Exception:
                down = 0
            out[r["rel_path"]] = (has_data, up, down)
    return out


def sync_full():
    """Full rescan: walk once, upsert changed, delete missing. Returns summary dict."""
    import datetime as dt

    from viewer_support import parse_dt_from_name

    if _sync_state["running"]:
        return {"ok": False, "message": "Sync already running"}
    _sync_state["running"] = True
    _sync_state["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _sync_state["last_error"] = ""
    t0 = time.time()
    ensure_schema()
    result_dir, _ = _paths()
    try:
        found = {}
        if result_dir.exists():
            for root, _, files in os.walk(result_dir):
                for file_name in files:
                    p = Path(root) / file_name
                    # cheap ext pre-filter before stat
                    if p.suffix.lower() not in (
                        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
                        ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
                    ):
                        continue
                    try:
                        rel = str(p.resolve().relative_to(result_dir)).replace("\\", "/")
                    except Exception:
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    row = _row_for_file(p, rel, st, parse_dt_from_name, dt)
                    if row:
                        found[rel] = row

        with _lock, connect() as conn:
            existing = {}
            for r in conn.execute(
                "SELECT rel_path, size_bytes, mtime, has_lookup_data, up_score, down_score FROM media_index"
            ).fetchall():
                existing[r["rel_path"]] = (r["size_bytes"], r["mtime"], r["has_lookup_data"], r["up_score"], r["down_score"])
            lookup_needed = [rel for rel in found]
            lookups = _lookup_map(conn, lookup_needed)

            to_upsert = []
            for rel, row in found.items():
                prev = existing.get(rel)
                lk = lookups.get(rel, (0, 0, 0))
                if prev and prev[0] == row["size_bytes"] and prev[1] == row["mtime"]:
                    # file unchanged; still refresh lookup flags if they differ
                    if (prev[2], prev[3], prev[4]) == lk:
                        continue
                to_upsert.append(
                    (
                        row["rel_path"], row["file_name"], row["dir_path"], row["kind"],
                        row["year"], row["month"], row["day"], row["size_bytes"],
                        row["mtime"], row["sort_ts"], row["display_dt"],
                        row["name_lower"], row["rel_lower"],
                        lk[0], lk[1], lk[2],
                    )
                )
            if to_upsert:
                conn.executemany(
                    """INSERT INTO media_index (rel_path, file_name, dir_path, kind, year, month, day,
                        size_bytes, mtime, sort_ts, display_dt, name_lower, rel_lower,
                        has_lookup_data, up_score, down_score)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(rel_path) DO UPDATE SET
                        file_name=excluded.file_name, dir_path=excluded.dir_path, kind=excluded.kind,
                        year=excluded.year, month=excluded.month, day=excluded.day,
                        size_bytes=excluded.size_bytes, mtime=excluded.mtime, sort_ts=excluded.sort_ts,
                        display_dt=excluded.display_dt, name_lower=excluded.name_lower,
                        rel_lower=excluded.rel_lower, has_lookup_data=excluded.has_lookup_data,
                        up_score=excluded.up_score, down_score=excluded.down_score""",
                    to_upsert,
                )
            missing = [rel for rel in existing if rel not in found]
            removed = 0
            if missing:
                chunk = 800
                for i in range(0, len(missing), chunk):
                    batch = missing[i : i + chunk]
                    qmarks = ",".join("?" for _ in batch)
                    cur = conn.execute(f"DELETE FROM media_index WHERE rel_path IN ({qmarks})", batch)
                    removed += cur.rowcount or 0
            conn.execute(
                "INSERT INTO index_meta(key, value) VALUES('last_sync', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (dt.datetime.now().isoformat(timespec="seconds"),),
            )
            conn.execute(
                "INSERT INTO index_meta(key, value) VALUES('last_duration_s', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"{time.time() - t0:.1f}",),
            )
            conn.commit()
        return {
            "ok": True,
            "files_found": len(found),
            "upserted": len(to_upsert),
            "removed": removed,
            "duration_s": round(time.time() - t0, 1),
        }
    except Exception as exc:
        _sync_state["last_error"] = str(exc)
        return {"ok": False, "message": str(exc)}
    finally:
        _sync_state["running"] = False


def sync_in_background():
    t = threading.Thread(target=sync_full, daemon=True)
    t.start()
    return t


def upsert_path(abs_path):
    """Incrementally index one file (call after upload/ingest/import)."""
    import datetime as dt

    from viewer_support import parse_dt_from_name

    ensure_schema()
    result_dir, _ = _paths()
    p = Path(abs_path)
    try:
        rel = str(p.resolve().relative_to(result_dir)).replace("\\", "/")
    except Exception:
        return False
    try:
        st = p.stat()
    except OSError:
        return remove_rel_path(rel)
    row = _row_for_file(p, rel, st, parse_dt_from_name, dt)
    if not row:
        return False
    with _lock, connect() as conn:
        lookups = _lookup_map(conn, [rel])
        lk = lookups.get(rel, (0, 0, 0))
        conn.execute(
            """INSERT INTO media_index (rel_path, file_name, dir_path, kind, year, month, day,
                size_bytes, mtime, sort_ts, display_dt, name_lower, rel_lower,
                has_lookup_data, up_score, down_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rel_path) DO UPDATE SET
                file_name=excluded.file_name, dir_path=excluded.dir_path, kind=excluded.kind,
                year=excluded.year, month=excluded.month, day=excluded.day,
                size_bytes=excluded.size_bytes, mtime=excluded.mtime, sort_ts=excluded.sort_ts,
                display_dt=excluded.display_dt, name_lower=excluded.name_lower,
                rel_lower=excluded.rel_lower, has_lookup_data=excluded.has_lookup_data,
                up_score=excluded.up_score, down_score=excluded.down_score""",
            (
                row["rel_path"], row["file_name"], row["dir_path"], row["kind"],
                row["year"], row["month"], row["day"], row["size_bytes"],
                row["mtime"], row["sort_ts"], row["display_dt"],
                row["name_lower"], row["rel_lower"], lk[0], lk[1], lk[2],
            ),
        )
        conn.commit()
    return True


def remove_rel_path(rel_path):
    ensure_schema()
    rel = str(rel_path or "").replace("\\", "/").strip()
    if not rel:
        return False
    with _lock, connect() as conn:
        conn.execute("DELETE FROM media_index WHERE rel_path = ?", (rel,))
        conn.commit()
    return True


def refresh_lookup_fields(rel_paths):
    """Refresh denormalized lookup scores after reverse-search fetches."""
    ensure_schema()
    rels = [str(r).replace("\\", "/") for r in (rel_paths or []) if r]
    if not rels:
        return 0
    with _lock, connect() as conn:
        lookups = _lookup_map(conn, rels)
        for rel, (has_data, up, down) in lookups.items():
            conn.execute(
                "UPDATE media_index SET has_lookup_data=?, up_score=?, down_score=? WHERE rel_path=?",
                (has_data, up, down, rel),
            )
        conn.commit()
    return len(lookups)


SORT_SQL = {
    "date_desc": "sort_ts DESC, file_name COLLATE NOCASE ASC",
    "date_asc": "sort_ts ASC, file_name COLLATE NOCASE ASC",
    "size_desc": "size_bytes DESC, sort_ts DESC",
    "size_asc": "size_bytes ASC, sort_ts ASC",
    "name_asc": "file_name COLLATE NOCASE ASC, sort_ts ASC",
    "name_desc": "file_name COLLATE NOCASE DESC, sort_ts DESC",
    "path_asc": "rel_path COLLATE NOCASE ASC, sort_ts ASC",
    "path_desc": "rel_path COLLATE NOCASE DESC, sort_ts DESC",
    "type_asc": "kind ASC, file_name COLLATE NOCASE ASC, sort_ts ASC",
    "type_desc": "kind DESC, file_name COLLATE NOCASE DESC, sort_ts DESC",
    "up_score_desc": "up_score DESC, sort_ts DESC",
    "up_score_asc": "up_score ASC, sort_ts ASC",
    "down_score_desc": "down_score DESC, sort_ts DESC",
    "down_score_asc": "down_score ASC, sort_ts ASC",
}


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_media(year="", month="", day="", q="", media="all", sort_order="date_desc",
                page=1, page_size=120, pinned_abs=None):
    """Single-SQL filtered/sorted/paginated query. Returns (items, total, page, pages, norm_sort)."""
    ensure_schema()
    result_dir, _ = _paths()
    mode = (sort_order or "date_desc").lower()
    mode = {"desc": "date_desc", "asc": "date_asc"}.get(mode, mode)
    if mode not in SORT_SQL:
        mode = "date_desc"
    order_sql = SORT_SQL[mode]

    where = []
    params = []
    if year:
        where.append("year = ?")
        params.append(year)
    if month:
        where.append("month = ?")
        params.append(month)
    if day:
        where.append("day = ?")
        params.append(day)
    media = (media or "all").lower()
    if media in {"image", "video"}:
        where.append("kind = ?")
        params.append(media)
    q = (q or "").strip().lower()
    if q:
        where.append("(name_lower LIKE ? ESCAPE '\\' OR rel_lower LIKE ? ESCAPE '\\')")
        like = f"%{_escape_like(q)}%"
        params.extend([like, like])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    page_size = max(1, min(int(page_size or 120), 500))
    with _lock, connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM media_index {where_sql}", params).fetchone()["c"]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, int(page or 1)), pages)
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT rel_path, file_name, kind, year, month, day, display_dt, sort_ts,
                size_bytes, has_lookup_data, up_score, down_score
                FROM media_index {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()

    items = []
    for r in rows:
        abs_path = str((result_dir / r["rel_path"]).resolve())
        items.append({
            "name": r["file_name"],
            "abs_path": abs_path,
            "rel_path": r["rel_path"],
            "kind": r["kind"],
            "year": r["year"],
            "month": r["month"],
            "day": r["day"],
            "display_dt": r["display_dt"],
            "sort_ts": r["sort_ts"],
            "size_bytes": r["size_bytes"],
            "lookup_summary": {"up_score": r["up_score"], "down_score": r["down_score"]},
            "has_lookup_data": bool(r["has_lookup_data"]),
            "up_score": r["up_score"] or 0,
            "down_score": r["down_score"] or 0,
        })
    if pinned_abs:
        for item in items:
            item["is_pinned"] = item["abs_path"] in pinned_abs
    return items, total, page, pages, mode


def collect_calendar():
    """Real directories only (no filesystem walk).

    Derived from ``dir_path`` prefixes rather than the year/month/day
    columns: files stored flat (e.g. ``2030/clip.mp4``) would otherwise leak
    filenames into the month/day navigation as if they were folders.
    Only directories that (transitively) contain indexed media are listed.
    """
    ensure_schema()
    with _lock, connect() as conn:
        dir_rows = conn.execute("SELECT DISTINCT dir_path FROM media_index").fetchall()

    def _sort_key(name):
        return (0 if name.isdigit() else 1, name.lower())

    years_set = set()
    months_set = set()
    days_set = set()
    for r in dir_rows:
        d = (r["dir_path"] or "").replace("\\", "/").strip("/")
        if not d or d == ".":
            continue
        parts = d.split("/")
        years_set.add(parts[0])
        if len(parts) >= 2:
            months_set.add((parts[0], parts[1]))
        if len(parts) >= 3:
            days_set.add((parts[0], parts[1], parts[2]))

    years_sorted = sorted(years_set, key=_sort_key, reverse=True)
    months_by_year = {}
    for y, m in sorted(months_set, key=lambda t: (_sort_key(t[0]), _sort_key(t[1]))):
        months_by_year.setdefault(y, []).append(m)
    for y in months_by_year:
        months_by_year[y] = sorted(months_by_year[y], key=_sort_key)
    days_by_ym = {}
    for y, m, day in sorted(days_set):
        days_by_ym.setdefault(f"{y}-{m}", []).append(day)
    for k in days_by_ym:
        days_by_ym[k] = sorted(days_by_ym[k], key=_sort_key)
    return years_sorted, months_by_year, days_by_ym


def timeline_counts():
    """GROUP BY counts from the index. Returns (daily, monthly, yearly) dicts."""
    ensure_schema()
    from collections import defaultdict

    daily = defaultdict(lambda: {"all": 0, "image": 0, "video": 0})
    monthly = defaultdict(lambda: {"all": 0, "image": 0, "video": 0})
    yearly = defaultdict(lambda: {"all": 0, "image": 0, "video": 0})
    with _lock, connect() as conn:
        for r in conn.execute(
            "SELECT year, month, day, kind, COUNT(*) AS c FROM media_index GROUP BY year, month, day, kind"
        ).fetchall():
            if not (r["year"] and r["month"] and r["day"]):
                continue
            dk, mk, yk = f"{r['year']}-{r['month']}-{r['day']}", f"{r['year']}-{r['month']}", r["year"]
            daily[dk]["all"] += r["c"]
            monthly[mk]["all"] += r["c"]
            yearly[yk]["all"] += r["c"]
            if r["kind"] in ("image", "video"):
                daily[dk][r["kind"]] += r["c"]
                monthly[mk][r["kind"]] += r["c"]
                yearly[yk][r["kind"]] += r["c"]
    return daily, monthly, yearly


def build_chart_series(granularity="daily", media="all", year_filter="", month_filter="", limit="all"):
    granularity = (granularity or "daily").lower()
    media = (media or "all").lower()
    year_filter = (year_filter or "").strip()
    month_filter = (month_filter or "").strip()
    limit = (limit or "all").lower()
    if granularity not in {"daily", "monthly", "annually"}:
        granularity = "daily"
    if media not in {"all", "image", "video"}:
        media = "all"
    if limit not in {"all", "30", "90", "180", "365"}:
        limit = "all"

    daily, monthly, yearly = timeline_counts()
    source = {"daily": daily, "monthly": monthly, "annually": yearly}[granularity]
    labels = sorted(source.keys())
    if granularity == "daily" and year_filter:
        labels = [lb for lb in labels if lb.startswith(f"{year_filter}-")]
    if granularity == "daily" and month_filter:
        labels = [lb for lb in labels if lb.startswith(f"{year_filter}-{month_filter}-")]
    if granularity == "monthly" and year_filter:
        labels = [lb for lb in labels if lb.startswith(f"{year_filter}-")]
    if limit != "all":
        labels = labels[-int(limit):]
    points = [{"label": lb, "count": source[lb][media]} for lb in labels]
    total = sum(p["count"] for p in points)
    return {
        "granularity": granularity,
        "media": media,
        "year_filter": year_filter,
        "month_filter": month_filter,
        "limit": limit,
        "labels": labels,
        "points": points,
        "total": total,
        "peak": max((p["count"] for p in points), default=0),
        "average": round(total / len(points), 2) if points else 0,
    }
