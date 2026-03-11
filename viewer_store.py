import datetime as dt
import json
import os
from collections import defaultdict
from pathlib import Path

from resource_lookup import ReverseSearchService
from viewer_context import INBOX_DIR, INBOX_MARKER, PINNED_JSON, RESULT_DIR
from viewer_support import list_subdirs, media_type_for_ext, parse_dt_from_name


def ensure_inbox_dir():
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_MARKER.touch(exist_ok=True)


def load_pins():
    if not PINNED_JSON.exists():
        return set()
    try:
        with open(PINNED_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(x) for x in data)
    except Exception:
        return set()
    return set()


def save_pins(pins):
    with open(PINNED_JSON, "w", encoding="utf-8") as f:
        json.dump(sorted(pins), f, indent=2, ensure_ascii=False)


def safe_rel_path(abs_path):
    try:
        return str(Path(abs_path).resolve().relative_to(RESULT_DIR)).replace("\\", "/")
    except Exception:
        return None


lookup_service = ReverseSearchService(
    result_dir=RESULT_DIR,
    media_type_for_ext=media_type_for_ext,
    safe_rel_path=safe_rel_path,
)


def calendar_path(year="", month="", day=""):
    parts = [part for part in (year, month, day) if part]
    if not parts:
        return None
    return RESULT_DIR.joinpath(*parts)


def scan_resources(base_dir):
    if not base_dir.exists():
        return []

    items = []
    lookup_cache_by_dir = {}
    for root, _, files in os.walk(base_dir):
        for file_name in files:
            p = Path(root) / file_name
            ext = p.suffix.lower()
            kind = media_type_for_ext(ext)
            if kind == "other":
                continue

            rel_path = safe_rel_path(p)
            if not rel_path:
                continue

            parts = Path(rel_path).parts
            year = parts[0] if len(parts) > 0 else ""
            month = parts[1] if len(parts) > 1 else ""
            day = parts[2] if len(parts) > 2 else ""

            parsed_dt = parse_dt_from_name(file_name)
            stat = p.stat()
            sort_ts = parsed_dt.timestamp() if parsed_dt else stat.st_mtime
            display_dt = (
                parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
                if parsed_dt
                else dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            )
            cached_lookup = None
            if kind == "image":
                dir_key = str(p.parent.resolve())
                if dir_key not in lookup_cache_by_dir:
                    lookup_cache_by_dir[dir_key] = lookup_service.load_dir_lookup_cache(p)
                resources = lookup_cache_by_dir[dir_key].get("resources", {})
                candidate = resources.get(p.name) if isinstance(resources, dict) else None
                if isinstance(candidate, dict):
                    result_data = candidate.get("result")
                    fetched_at_raw = candidate.get("fetched_at")
                    fetched_at = None
                    if fetched_at_raw:
                        try:
                            fetched_at = dt.datetime.fromisoformat(str(fetched_at_raw))
                        except ValueError:
                            fetched_at = None

                    is_short_limit = isinstance(result_data, dict) and result_data.get("error") == "ShortLimitReached"
                    is_stale = fetched_at is not None and fetched_at <= (dt.datetime.now() - dt.timedelta(days=1))
                    if is_short_limit and is_stale:
                        refreshed_payload, refreshed_status = lookup_service.get_or_update_lookup_data(
                            safe_rel_path(p),
                            force=True,
                        )
                        if refreshed_status == 200 and refreshed_payload.get("ok"):
                            refreshed_resource = refreshed_payload.get("data")
                            if isinstance(refreshed_resource, dict):
                                resources[p.name] = refreshed_resource
                                candidate = refreshed_resource

                    if not isinstance(candidate.get("summary"), dict):
                        candidate["summary"] = lookup_service.summarize_resource(candidate)
                    cached_lookup = candidate
            lookup_summary = cached_lookup.get("summary") if isinstance(cached_lookup, dict) else {}

            items.append(
                {
                    "name": file_name,
                    "abs_path": str(p.resolve()),
                    "rel_path": rel_path,
                    "kind": kind,
                    "year": year,
                    "month": month,
                    "day": day,
                    "display_dt": display_dt,
                    "sort_ts": sort_ts,
                    "size_bytes": stat.st_size,
                    "lookup_summary": lookup_summary,
                    "up_score": lookup_summary.get("up_score") or 0,
                    "down_score": lookup_summary.get("down_score") or 0,
                }
            )
    return items


def apply_filters(items, q, media):
    q = (q or "").strip().lower()
    media = (media or "all").lower()

    filtered = []
    for item in items:
        if media in {"image", "video"} and item["kind"] != media:
            continue
        if q and q not in item["name"].lower() and q not in item["rel_path"].lower():
            continue
        filtered.append(item)
    return filtered


def apply_sort(items, sort_order):
    mode = (sort_order or "date_desc").lower()
    aliases = {
        "desc": "date_desc",
        "asc": "date_asc",
    }
    mode = aliases.get(mode, mode)

    if mode == "date_asc":
        items.sort(key=lambda x: (x["sort_ts"], x["name"].lower()))
    elif mode == "size_desc":
        items.sort(key=lambda x: (x.get("size_bytes", 0), x["sort_ts"]), reverse=True)
    elif mode == "size_asc":
        items.sort(key=lambda x: (x.get("size_bytes", 0), x["sort_ts"]))
    elif mode == "name_asc":
        items.sort(key=lambda x: (x["name"].lower(), x["sort_ts"]))
    elif mode == "name_desc":
        items.sort(key=lambda x: (x["name"].lower(), x["sort_ts"]), reverse=True)
    elif mode == "path_asc":
        items.sort(key=lambda x: (x["rel_path"].lower(), x["sort_ts"]))
    elif mode == "path_desc":
        items.sort(key=lambda x: (x["rel_path"].lower(), x["sort_ts"]), reverse=True)
    elif mode == "type_asc":
        items.sort(key=lambda x: (x["kind"], x["name"].lower(), x["sort_ts"]))
    elif mode == "type_desc":
        items.sort(key=lambda x: (x["kind"], x["name"].lower(), x["sort_ts"]), reverse=True)
    elif mode == "up_score_desc":
        items.sort(key=lambda x: (x.get("up_score", 0), x["sort_ts"]), reverse=True)
    elif mode == "up_score_asc":
        items.sort(key=lambda x: (x.get("up_score", 0), x["sort_ts"]))
    elif mode == "down_score_desc":
        items.sort(key=lambda x: (x.get("down_score", 0), x["sort_ts"]), reverse=True)
    elif mode == "down_score_asc":
        items.sort(key=lambda x: (x.get("down_score", 0), x["sort_ts"]))
    else:
        mode = "date_desc"
        items.sort(key=lambda x: (x["sort_ts"], x["name"].lower()), reverse=True)

    return mode


def collect_calendar():
    if not RESULT_DIR.exists():
        return [], {}, {}

    years = []
    months_by_year = {}
    days_by_ym = {}

    for year_dir in list_subdirs(RESULT_DIR, reverse=True):
        y = year_dir.name
        years.append(y)
        months = []
        for month_dir in list_subdirs(year_dir):
            m = month_dir.name
            months.append(m)
            days = [day_dir.name for day_dir in list_subdirs(month_dir)]
            days_by_ym[f"{y}-{m}"] = days
        months_by_year[y] = months

    return years, months_by_year, days_by_ym


def scan_timeline_counts():
    daily = defaultdict(lambda: {"all": 0, "image": 0, "video": 0})
    monthly = defaultdict(lambda: {"all": 0, "image": 0, "video": 0})
    yearly = defaultdict(lambda: {"all": 0, "image": 0, "video": 0})

    if not RESULT_DIR.exists():
        return daily, monthly, yearly

    for root, _, files in os.walk(RESULT_DIR):
        root_path = Path(root)
        try:
            rel_root = root_path.resolve().relative_to(RESULT_DIR)
        except Exception:
            continue

        parts = rel_root.parts
        if len(parts) < 3:
            continue

        year, month, day = parts[0], parts[1], parts[2]
        day_key = f"{year}-{month}-{day}"
        month_key = f"{year}-{month}"
        year_key = year

        for file_name in files:
            kind = media_type_for_ext(Path(file_name).suffix.lower())
            if kind == "other":
                continue

            daily[day_key]["all"] += 1
            daily[day_key][kind] += 1
            monthly[month_key]["all"] += 1
            monthly[month_key][kind] += 1
            yearly[year_key]["all"] += 1
            yearly[year_key][kind] += 1

    return daily, monthly, yearly


def build_chart_series(granularity, media, year_filter="", month_filter="", limit="all"):
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

    daily, monthly, yearly = scan_timeline_counts()
    source = {
        "daily": daily,
        "monthly": monthly,
        "annually": yearly,
    }[granularity]

    labels = sorted(source.keys())
    if granularity == "daily" and year_filter:
        labels = [label for label in labels if label.startswith(f"{year_filter}-")]
    if granularity == "daily" and month_filter:
        labels = [label for label in labels if label.startswith(f"{year_filter}-{month_filter}-")]
    if granularity == "monthly" and year_filter:
        labels = [label for label in labels if label.startswith(f"{year_filter}-")]

    if limit != "all":
        labels = labels[-int(limit):]

    points = [{"label": label, "count": source[label][media]} for label in labels]
    total = sum(point["count"] for point in points)
    peak = max((point["count"] for point in points), default=0)
    average = round(total / len(points), 2) if points else 0

    return {
        "granularity": granularity,
        "media": media,
        "year_filter": year_filter,
        "month_filter": month_filter,
        "limit": limit,
        "labels": labels,
        "points": points,
        "total": total,
        "peak": peak,
        "average": average,
    }


def selected_base_dir(year, month, day):
    base_dir = calendar_path(year, month, day)
    if base_dir and base_dir.exists() and base_dir.is_dir():
        return base_dir
    return None


def filtered_scope_items(year, month, day, q, media):
    base_dir = selected_base_dir(year, month, day)
    if not base_dir:
        return None, []
    items = scan_resources(base_dir)
    return base_dir, apply_filters(items, q, media)


def paginate(items, page, page_size):
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total, page, pages


def list_inbox_candidates():
    ensure_inbox_dir()
    files = []
    for root, _, names in os.walk(INBOX_DIR):
        for file_name in names:
            p = Path(root) / file_name
            if p.resolve() == INBOX_MARKER.resolve():
                continue
            if media_type_for_ext(p.suffix.lower()) == "other":
                continue
            files.append(p)
    return files
