import datetime as dt
import shutil
from pathlib import Path

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for

from viewer_context import INBOX_DIR, INBOX_MARKER, PAGE_SIZE, RESULT_DIR, Image
from viewer_store import (
    apply_sort,
    build_chart_series,
    collect_calendar,
    filtered_scope_items,
    list_inbox_candidates,
    load_pins,
    lookup_service,
    paginate,
    safe_rel_path,
    save_pins,
)
from viewer_support import media_type_for_ext, metadata_datetime_for_file, normalized_upload_name, parse_dt_from_name, unique_path


def register_routes(flask_app):
    @flask_app.route("/")
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
        base_dir, filtered = filtered_scope_items(year, month, day, q, media)
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
            reverse_ui_config=lookup_service.ui_config,
        )

    @flask_app.route("/stats")
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

    @flask_app.route("/month/<year>/<month>")
    def month_view(year, month):
        return redirect(url_for("index", year=year, month=month))

    @flask_app.route("/day/<year>/<month>/<day>")
    def day_view(year, month, day):
        return redirect(url_for("index", year=year, month=month, day=day))

    @flask_app.route("/upload", methods=["GET", "POST"])
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

    @flask_app.route("/ingest", methods=["GET", "POST"])
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
                file_dt = metadata_datetime_for_file(src, Image)
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

    @flask_app.route("/pinned")
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

    @flask_app.post("/pin")
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

    @flask_app.post("/delete")
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

    @flask_app.post("/reverse-search")
    def reverse_search_route():
        data = request.get_json(silent=True) or {}
        rel_path = data.get("rel_path") or request.form.get("rel_path", "")
        force = str(data.get("force") or request.form.get("force") or "").lower() in {"1", "true", "yes", "on"}
        payload, status = lookup_service.get_or_update_lookup_data(rel_path, force=force)
        return jsonify(payload), status

    @flask_app.post("/reverse-search/batch")
    def reverse_search_batch_route():
        q = request.form.get("q", "")
        year = request.form.get("year", "")
        month = request.form.get("month", "")
        day = request.form.get("day", "")
        media = request.form.get("media", "all")
        sort_order = request.form.get("sort", "date_desc")
        page = request.form.get("page", "1")

        base_dir, filtered = filtered_scope_items(year, month, day, q, media)
        if base_dir is None:
            flash("Pick a year or month before fetching missing reverse-search data.")
            return redirect(url_for("index", q=q, year=year, month=month, day=day, media=media, sort=sort_order, page=page))

        image_items = [item for item in filtered if item["kind"] == "image"]
        fetched = 0
        skipped = 0
        failed = 0
        aborted_short_limit = False

        for index, item in enumerate(image_items):
            cached_lookup = lookup_service.cached_resource_data(Path(item["abs_path"]))
            should_force = (
                isinstance(cached_lookup, dict)
                and isinstance(cached_lookup.get("result"), dict)
                and cached_lookup["result"].get("error") == "ShortLimitReached"
            )
            payload, status = lookup_service.get_or_update_lookup_data(item["rel_path"], force=should_force)
            if status != 200 or not payload.get("ok"):
                failed += 1
                continue
            result_data = payload.get("data", {}).get("result")
            if isinstance(result_data, dict) and result_data.get("error") == "ShortLimitReached":
                failed += len(image_items) - index
                aborted_short_limit = True
                break
            if payload.get("cached"):
                skipped += 1
            else:
                fetched += 1

        if aborted_short_limit:
            flash(
                f"Reverse-search batch stopped on ShortLimitReached. Matching images: {len(image_items)}. "
                f"Fetched missing: {fetched}. Already cached: {skipped}. Failed: {failed}."
            )
        else:
            flash(
                f"Reverse-search batch complete. Matching images: {len(image_items)}. "
                f"Fetched missing: {fetched}. Already cached: {skipped}. Failed: {failed}."
            )
        return redirect(url_for("index", q=q, year=year, month=month, day=day, media=media, sort=sort_order, page=page))

    @flask_app.route("/media/<path:rel_path>")
    def media(rel_path):
        abs_path = lookup_service.resolve_media_path(rel_path)
        if abs_path is None:
            return "Invalid path", 400
        if not abs_path.exists():
            return "Not found", 404
        return send_file(abs_path, as_attachment=False)

    @flask_app.route("/download/<path:rel_path>")
    def download(rel_path):
        abs_path = lookup_service.resolve_media_path(rel_path)
        if abs_path is None:
            return "Invalid path", 400
        if not abs_path.exists():
            return "Not found", 404
        return send_file(abs_path, as_attachment=True, download_name=abs_path.name)
