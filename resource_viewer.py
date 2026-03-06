import datetime as dt
import json
import os
import re
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for


APP_DIR = Path(__file__).resolve().parent
RESULT_DIR = Path(os.getenv("RESOURCE_RESULT_DIR", APP_DIR.parent / "result")).resolve()
PINNED_JSON = Path(os.getenv("RESOURCE_PINNED_JSON", APP_DIR / "pinned_files.json")).resolve()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

FILENAME_DT_RE = re.compile(r"@(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "resource-viewer-secret")
PAGE_SIZE = max(1, int(os.getenv("RESOURCE_PAGE_SIZE", "120")))


def parse_dt_from_name(file_name):
    m = FILENAME_DT_RE.search(file_name)
    if not m:
        return None
    day, month, year, hh, mm, ss = map(int, m.groups())
    try:
        return dt.datetime(year, month, day, hh, mm, ss)
    except ValueError:
        return None


def media_type_for_ext(ext):
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


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


def scan_resources(base_dir):
    if not base_dir.exists():
        return []

    items = []
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


def collect_calendar():
    if not RESULT_DIR.exists():
        return [], {}, {}

    years = []
    months_by_year = {}
    days_by_ym = {}

    for year_dir in sorted((x for x in RESULT_DIR.iterdir() if x.is_dir()), key=lambda p: p.name, reverse=True):
        y = year_dir.name
        if not y.isdigit():
            continue
        years.append(y)
        months = []
        for month_dir in sorted((x for x in year_dir.iterdir() if x.is_dir()), key=lambda p: p.name):
            m = month_dir.name
            if not m.isdigit():
                continue
            months.append(m)
            days = sorted(
                [x.name for x in month_dir.iterdir() if x.is_dir() and x.name.isdigit()]
            )
            days_by_ym[f"{y}-{m}"] = days
        months_by_year[y] = months

    return years, months_by_year, days_by_ym


def selected_base_dir(year, month, day):
    if day and month and year:
        return RESULT_DIR / year / month / day
    if month and year:
        return RESULT_DIR / year / month
    if year:
        return RESULT_DIR / year
    return None


def paginate(items, page, page_size):
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total, page, pages


@app.route("/")
def index():
    q = request.args.get("q", "")
    year = request.args.get("year", "")
    month = request.args.get("month", "")
    day = request.args.get("day", "")
    media = request.args.get("media", "all")
    sort_order = request.args.get("sort", "desc")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    years, months_by_year, days_by_ym = collect_calendar()

    base_dir = selected_base_dir(year, month, day)
    items = scan_resources(base_dir) if base_dir else []
    filtered = apply_filters(items, q, media)

    reverse = sort_order != "asc"
    filtered.sort(key=lambda x: x["sort_ts"], reverse=reverse)
    page_items, total, page, pages = paginate(filtered, page, PAGE_SIZE)

    pins = load_pins()
    for item in page_items:
        item["is_pinned"] = item["abs_path"] in pins

    return render_template(
        "resource_index.html",
        items=page_items,
        total=total,
        q=q,
        year=year,
        month=month,
        day=day,
        media=media,
        sort_order=sort_order,
        years=years,
        months_by_year=months_by_year,
        days_by_ym=days_by_ym,
        pinned_count=len(pins),
        result_dir=str(RESULT_DIR),
        has_scope=base_dir is not None,
        page=page,
        pages=pages,
        page_size=PAGE_SIZE,
    )


@app.route("/month/<year>/<month>")
def month_view(year, month):
    return redirect(url_for("index", year=year, month=month))


@app.route("/day/<year>/<month>/<day>")
def day_view(year, month, day):
    return redirect(url_for("index", year=year, month=month, day=day))


@app.route("/pinned")
def pinned():
    pins = load_pins()
    pinned_items = []
    for abs_path in pins:
        p = Path(abs_path)
        if not p.exists():
            continue
        rel_path = safe_rel_path(p)
        if not rel_path:
            continue
        kind = media_type_for_ext(p.suffix.lower())
        if kind == "other":
            continue
        parsed_dt = parse_dt_from_name(p.name)
        stat = p.stat()
        sort_ts = parsed_dt.timestamp() if parsed_dt else stat.st_mtime
        display_dt = (
            parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
            if parsed_dt
            else dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        )
        parts = Path(rel_path).parts
        pinned_items.append(
            {
                "name": p.name,
                "abs_path": str(p.resolve()),
                "rel_path": rel_path,
                "kind": kind,
                "year": parts[0] if len(parts) > 0 else "",
                "month": parts[1] if len(parts) > 1 else "",
                "day": parts[2] if len(parts) > 2 else "",
                "display_dt": display_dt,
                "sort_ts": sort_ts,
            }
        )
    pinned_items.sort(key=lambda x: x["sort_ts"], reverse=True)
    for item in pinned_items:
        item["is_pinned"] = True
    return render_template(
        "resource_pinned.html",
        items=pinned_items,
        total=len(pinned_items),
        pinned_count=len(pins),
    )


@app.post("/pin")
def pin_toggle():
    abs_path = request.form.get("abs_path", "")
    back = request.form.get("back", url_for("index"))
    if not abs_path:
        return redirect(back)

    pins = load_pins()
    if abs_path in pins:
        pins.remove(abs_path)
        flash("Unpinned file.")
    else:
        pins.add(abs_path)
        flash("Pinned file.")
    save_pins(pins)
    return redirect(back)


@app.post("/delete")
def delete_file():
    data = request.get_json(silent=True) or {}
    rel_path = data.get("rel_path") or request.form.get("rel_path", "")
    if not rel_path:
        return jsonify({"ok": False, "message": "Missing rel_path"}), 400

    safe_path = Path(rel_path)
    abs_path = (RESULT_DIR / safe_path).resolve()
    try:
        abs_path.relative_to(RESULT_DIR)
    except ValueError:
        return jsonify({"ok": False, "message": "Invalid path"}), 400

    if not abs_path.exists():
        return jsonify({"ok": False, "message": "File not found"}), 404
    if not abs_path.is_file():
        return jsonify({"ok": False, "message": "Not a file"}), 400

    try:
        abs_path.unlink()
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Delete failed: {exc}"}), 500

    pins = load_pins()
    abs_key = str(abs_path)
    if abs_key in pins:
        pins.remove(abs_key)
        save_pins(pins)

    return jsonify({"ok": True, "message": "File deleted", "rel_path": str(safe_path).replace("\\", "/")})


@app.route("/media/<path:rel_path>")
def media(rel_path):
    safe_path = Path(rel_path)
    abs_path = (RESULT_DIR / safe_path).resolve()
    try:
        abs_path.relative_to(RESULT_DIR)
    except ValueError:
        return "Invalid path", 400
    if not abs_path.exists():
        return "Not found", 404
    return send_file(abs_path, as_attachment=False)


@app.route("/download/<path:rel_path>")
def download(rel_path):
    safe_path = Path(rel_path)
    abs_path = (RESULT_DIR / safe_path).resolve()
    try:
        abs_path.relative_to(RESULT_DIR)
    except ValueError:
        return "Invalid path", 400
    if not abs_path.exists():
        return "Not found", 404
    return send_file(abs_path, as_attachment=True, download_name=abs_path.name)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
