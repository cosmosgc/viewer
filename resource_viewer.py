import datetime as dt
import json
import os
import re
import shutil
import socket
import sys
import threading
import urllib.request
import webbrowser
from collections import defaultdict
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

try:
    import pystray
except Exception:
    pystray = None

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    TEMPLATE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "templates"
    DEFAULT_RESULT_DIR = APP_DIR / "result"
else:
    APP_DIR = Path(__file__).resolve().parent
    TEMPLATE_DIR = APP_DIR / "templates"
    DEFAULT_RESULT_DIR = APP_DIR.parent / "result"

RESULT_DIR = Path(os.getenv("RESOURCE_RESULT_DIR", DEFAULT_RESULT_DIR)).resolve()
PINNED_JSON = Path(os.getenv("RESOURCE_PINNED_JSON", APP_DIR / "pinned_files.json")).resolve()
INBOX_DIR = Path(os.getenv("RESOURCE_INBOX_DIR", APP_DIR / "resource_inbox")).resolve()
INBOX_MARKER = INBOX_DIR / "drop_files_here.txt"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

FILENAME_DT_RE = re.compile(r"@(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})")

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "resource-viewer-secret")
PAGE_SIZE = max(1, int(os.getenv("RESOURCE_PAGE_SIZE", "120")))

try:
    from PIL import Image, ExifTags
except Exception:
    Image = None
    ExifTags = None


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


def ensure_inbox_dir():
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_MARKER.touch(exist_ok=True)


def exif_datetime_for_image(path):
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = getattr(img, "getexif", lambda: None)()
            if not exif:
                return None
            raw = exif.get(36867) or exif.get(306)
            if not raw:
                return None
            return dt.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def metadata_datetime_for_file(path):
    parsed = parse_dt_from_name(path.name)
    if parsed:
        return parsed
    if media_type_for_ext(path.suffix.lower()) == "image":
        exif_dt = exif_datetime_for_image(path)
        if exif_dt:
            return exif_dt
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return dt.datetime.now()


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
                    "size_bytes": stat.st_size,
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
        if not (year.isdigit() and month.isdigit() and day.isdigit()):
            continue

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


def normalized_upload_name(name):
    p = Path(name)
    stem = secure_filename(p.stem) or "resource"
    stem = re.sub(r"@\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2}$", "", stem)
    return stem


def unique_path(target_dir, file_name):
    candidate = target_dir / file_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    ext = candidate.suffix
    i = 1
    while True:
        alt = target_dir / f"{stem}_{i}{ext}"
        if not alt.exists():
            return alt
        i += 1


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


def detect_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def detect_public_ip():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return "Unavailable"


def run_status_window(host, port, debug):
    import tkinter as tk
    from tkinter import ttk

    local_ip = detect_local_ip()
    public_ip = detect_public_ip()
    localhost_url = f"http://127.0.0.1:{port}"
    lan_url = f"http://{local_ip}:{port}"
    bind_label = "0.0.0.0 (all interfaces)" if host == "0.0.0.0" else host

    server = make_server(host, port, app)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    root = tk.Tk()
    root.title("Cosmos's Galery Manager - Server Status")
    root.geometry("560x310")
    root.resizable(False, False)
    root_mounted = True
    tray_icon = None

    wrap = ttk.Frame(root, padding=14)
    wrap.pack(fill="both", expand=True)

    ttk.Label(wrap, text="Cosmos's Galery Manager", font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(wrap, text="Server is running").pack(anchor="w", pady=(2, 10))

    grid = ttk.Frame(wrap)
    grid.pack(fill="x", pady=(0, 12))

    def row(label, value):
        r = ttk.Frame(grid)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=14).pack(side="left")
        ttk.Label(r, text=value).pack(side="left")

    row("Bind Host:", bind_label)
    row("Port:", str(port))
    row("Local IP:", local_ip)
    row("Public IP:", public_ip)
    row("Local URL:", localhost_url)
    row("LAN URL:", lan_url)
    row("Debug:", str(debug))

    hint = (
        "Public access needs router port-forward + firewall allow rule.\n"
        "Use LAN URL for devices on the same network."
    )
    ttk.Label(wrap, text=hint).pack(anchor="w", pady=(0, 12))

    actions = ttk.Frame(wrap)
    actions.pack(fill="x")

    def open_local():
        webbrowser.open(localhost_url)

    def open_lan():
        webbrowser.open(lan_url)

    def show_window():
        if not root_mounted:
            return
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def hide_window():
        if not root_mounted:
            return
        root.after(0, root.withdraw)

    def stop_from_tray(icon=None, item=None):
        stop_server()

    def setup_tray():
        nonlocal tray_icon
        if pystray is None:
            return
        try:
            from PIL import Image as PILImage, ImageDraw
        except Exception:
            return

        icon_img = PILImage.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(icon_img)
        d.ellipse((6, 6, 58, 58), fill=(11, 103, 255, 255), outline=(230, 240, 255, 255), width=2)
        d.rectangle((20, 18, 44, 46), fill=(240, 246, 255, 255))
        d.rectangle((24, 22, 40, 28), fill=(11, 103, 255, 255))
        d.rectangle((24, 32, 40, 42), fill=(11, 103, 255, 255))

        tray_icon = pystray.Icon(
            "cosmos_gallery_manager",
            icon_img,
            "Cosmos's Galery Manager",
            menu=pystray.Menu(
                pystray.MenuItem("Show Status", lambda icon, item: show_window()),
                pystray.MenuItem("Hide Status", lambda icon, item: hide_window()),
                pystray.MenuItem("Open Local", lambda icon, item: open_local()),
                pystray.MenuItem("Open LAN", lambda icon, item: open_lan()),
                pystray.MenuItem("Stop Server", stop_from_tray),
            ),
        )
        tray_icon.run_detached()

    def stop_server():
        nonlocal root_mounted
        if not root_mounted:
            return
        root_mounted = False
        try:
            if tray_icon is not None:
                try:
                    tray_icon.stop()
                except Exception:
                    pass
            server.shutdown()
        finally:
            root.destroy()

    ttk.Button(actions, text="Open Local", command=open_local).pack(side="left", padx=(0, 8))
    ttk.Button(actions, text="Open LAN", command=open_lan).pack(side="left", padx=(0, 8))
    ttk.Button(actions, text="Hide", command=hide_window).pack(side="left", padx=(0, 8))
    ttk.Button(actions, text="Stop Server", command=stop_server).pack(side="right")

    def on_close():
        if tray_icon is not None:
            hide_window()
        else:
            stop_server()

    root.protocol("WM_DELETE_WINDOW", on_close)
    setup_tray()
    root.mainloop()


@app.route("/")
def index():
    q = request.args.get("q", "")
    year = request.args.get("year", "")
    month = request.args.get("month", "")
    day = request.args.get("day", "")
    media = request.args.get("media", "all")
    sort_order = request.args.get("sort", "date_desc")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    years, months_by_year, days_by_ym = collect_calendar()

    base_dir = selected_base_dir(year, month, day)
    items = scan_resources(base_dir) if base_dir else []
    filtered = apply_filters(items, q, media)

    sort_order = apply_sort(filtered, sort_order)
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
        inbox_count=len(list_inbox_candidates()),
    )


@app.route("/stats")
def stats_view():
    granularity = request.args.get("granularity", "daily")
    media = request.args.get("media", "all")
    year = request.args.get("year", "")
    month = request.args.get("month", "")
    limit = request.args.get("limit", "all")

    years, months_by_year, _ = collect_calendar()
    if year and year not in years:
        year = ""
    if month and (not year or month not in months_by_year.get(year, [])):
        month = ""

    chart_data = build_chart_series(
        granularity=granularity,
        media=media,
        year_filter=year,
        month_filter=month,
        limit=limit,
    )

    return render_template(
        "resource_stats.html",
        chart_data=chart_data,
        years=years,
        months_by_year=months_by_year,
        year=year,
        month=month,
        granularity=chart_data["granularity"],
        media=chart_data["media"],
        limit=chart_data["limit"],
        pinned_count=len(load_pins()),
        inbox_count=len(list_inbox_candidates()),
        result_dir=str(RESULT_DIR),
    )


@app.route("/month/<year>/<month>")
def month_view(year, month):
    return redirect(url_for("index", year=year, month=month))


@app.route("/day/<year>/<month>/<day>")
def day_view(year, month, day):
    return redirect(url_for("index", year=year, month=month, day=day))


@app.route("/upload", methods=["GET", "POST"])
def upload_resources():
    if request.method == "GET":
        return render_template("resource_upload.html")

    files = request.files.getlist("files")
    if not files:
        flash("No files selected.")
        return redirect(url_for("upload_resources"))

    now = dt.datetime.now()
    stamp = now.strftime("%d-%m-%Y_%H-%M-%S")
    target_dir = RESULT_DIR / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    target_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    skipped = 0

    for f in files:
        if not f or not f.filename:
            skipped += 1
            continue
        ext = Path(f.filename).suffix.lower()
        if media_type_for_ext(ext) == "other":
            skipped += 1
            continue
        base = normalized_upload_name(f.filename)
        new_name = f"{base}@{stamp}{ext}"
        dst = unique_path(target_dir, new_name)
        try:
            f.save(dst)
            saved += 1
        except Exception:
            skipped += 1

    flash(f"Upload complete. Saved: {saved}. Skipped: {skipped}.")
    return redirect(url_for("upload_resources"))


@app.route("/ingest", methods=["GET", "POST"])
def ingest_resources():
    candidates = list_inbox_candidates()
    if request.method == "GET":
        return render_template(
            "resource_ingest.html",
            inbox_dir=str(INBOX_DIR),
            inbox_marker=INBOX_MARKER.name,
            pending_count=len(candidates),
            pending_preview=[p.name for p in sorted(candidates)[:30]],
        )

    moved = 0
    skipped = 0
    failed = 0

    for src in candidates:
        ext = src.suffix.lower()
        if media_type_for_ext(ext) == "other":
            skipped += 1
            continue
        try:
            file_dt = metadata_datetime_for_file(src)
            stamp = file_dt.strftime("%d-%m-%Y_%H-%M-%S")
            target_dir = RESULT_DIR / file_dt.strftime("%Y") / file_dt.strftime("%m") / file_dt.strftime("%d")
            target_dir.mkdir(parents=True, exist_ok=True)

            base = normalized_upload_name(src.name)
            new_name = f"{base}@{stamp}{ext}"
            dst = unique_path(target_dir, new_name)
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception:
            failed += 1

    flash(f"Ingest complete. Moved: {moved}. Failed: {failed}. Skipped: {skipped}.")
    return redirect(url_for("ingest_resources"))


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
                "size_bytes": stat.st_size,
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
        inbox_count=len(list_inbox_candidates()),
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
    ensure_inbox_dir()
    host = os.getenv("RESOURCE_HOST", "0.0.0.0")
    port = int(os.getenv("RESOURCE_PORT", "5000"))
    debug = os.getenv("RESOURCE_DEBUG", "1").lower() in {"1", "true", "yes", "on"}
    gui_mode = os.getenv("RESOURCE_GUI", "1").lower() in {"1", "true", "yes", "on"}
    if getattr(sys, "frozen", False) and gui_mode:
        run_status_window(host=host, port=port, debug=debug)
    else:
        app.run(host=host, port=port, debug=debug)
