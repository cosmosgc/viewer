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
    load_watches,
    lookup_service,
    paginate,
    safe_rel_path,
    save_watches,
    save_pins,
)
from viewer_support import media_type_for_ext, metadata_datetime_for_file, normalized_upload_name, parse_dt_from_name, unique_path


def register_routes(flask_app):
    def watch_target_dir_for_post(post):
        created_at = lookup_service.parse_post_created_at((post or {}).get("created_at"))
        if created_at is None:
            return None
        return RESULT_DIR / "imported" / created_at.strftime("%Y") / created_at.strftime("%m")

    def watch_import_state(post):
        post_id = str((post or {}).get("id") or "").strip()
        target_dir = watch_target_dir_for_post(post)
        if not post_id or target_dir is None:
            return {
                "imported": False,
                "target_dir": "",
                "matching_files": [],
            }
        matching_files = []
        if target_dir.exists():
            pattern = f"{normalized_upload_name(f'e621_post_{post_id}')}@*"
            matching_files = sorted(path.name for path in target_dir.glob(pattern) if path.is_file())
        return {
            "imported": bool(matching_files),
            "target_dir": str(target_dir.relative_to(RESULT_DIR)),
            "matching_files": matching_files,
        }

    @flask_app.route("/")
    def index():
        q = request.args.get("q", "")
        year = request.args.get("year", "")
        month = request.args.get("month", "")
        day = request.args.get("day", "")
        media = request.args.get("media", "all")
        sort_order = request.args.get("sort", "date_desc")
        try:
            page_size = int(request.args.get("page_size", str(PAGE_SIZE)))
        except ValueError:
            page_size = PAGE_SIZE
        page_size = max(1, min(page_size, 500))
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1

        years, months_by_year, days_by_ym = collect_calendar()
        base_dir, filtered = filtered_scope_items(year, month, day, q, media)
        sort_order = apply_sort(filtered, sort_order)
        page_items, total, page, pages = paginate(filtered, page, page_size)

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
            page_size=page_size,
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

    @flask_app.route("/lookup")
    def lookup_view():
        return render_template(
            "resource_lookup.html",
            pinned_count=len(load_pins()),
            result_dir=str(RESULT_DIR),
            inbox_count=len(list_inbox_candidates()),
            reverse_ui_config=lookup_service.ui_config,
            lookup_db_path=str(lookup_service.db_path),
            lookup_cache_count=lookup_service.count_cached_resources(),
        )

    @flask_app.route("/watch")
    def watch_view():
        watches = load_watches()
        return render_template(
            "resource_watch.html",
            watches=watches,
            pinned_count=len(load_pins()),
            result_dir=str(RESULT_DIR),
            inbox_count=len(list_inbox_candidates()),
        )

    @flask_app.post("/watch/add")
    def watch_add_route():
        data = request.get_json(silent=True) or request.form
        tags = str(data.get("tags") or "").strip()
        last_seen_at = str(data.get("last_seen_at") or "").strip()
        if not tags:
            return jsonify({"ok": False, "message": "Tags are required"}), 400
        now = dt.datetime.now().isoformat(timespec="seconds")
        watches = load_watches()
        watch = {
            "id": dt.datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "tags": tags,
            "last_seen_at": last_seen_at or now,
            "created_at": now,
            "updated_at": now,
        }
        watches.append(watch)
        save_watches(watches)
        return jsonify({"ok": True, "message": "Watch added", "watch": watch}), 200

    @flask_app.post("/watch/update")
    def watch_update_route():
        data = request.get_json(silent=True) or request.form
        watch_id = str(data.get("id") or "").strip()
        if not watch_id:
            return jsonify({"ok": False, "message": "Missing watch id"}), 400
        watches = load_watches()
        for watch in watches:
            if watch["id"] != watch_id:
                continue
            if "tags" in data:
                tags = str(data.get("tags") or "").strip()
                if not tags:
                    return jsonify({"ok": False, "message": "Tags are required"}), 400
                watch["tags"] = tags
            if "last_seen_at" in data:
                watch["last_seen_at"] = str(data.get("last_seen_at") or "").strip()
            watch["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            save_watches(watches)
            return jsonify({"ok": True, "message": "Watch updated", "watch": watch}), 200
        return jsonify({"ok": False, "message": "Watch not found"}), 404

    @flask_app.post("/watch/delete")
    def watch_delete_route():
        data = request.get_json(silent=True) or request.form
        watch_id = str(data.get("id") or "").strip()
        if not watch_id:
            return jsonify({"ok": False, "message": "Missing watch id"}), 400
        watches = load_watches()
        kept = [watch for watch in watches if watch["id"] != watch_id]
        if len(kept) == len(watches):
            return jsonify({"ok": False, "message": "Watch not found"}), 404
        save_watches(kept)
        return jsonify({"ok": True, "message": "Watch deleted", "id": watch_id}), 200

    @flask_app.post("/watch/feed")
    def watch_feed_route():
        data = request.get_json(silent=True) or {}
        tags = str(data.get("tags") or "").strip()
        last_seen_at = str(data.get("last_seen_at") or "").strip()
        page = str(data.get("page") or "1").strip()
        limit = str(data.get("limit") or "40").strip()
        if not tags:
            return jsonify({"ok": False, "message": "Tags are required"}), 400

        try:
            page_num = max(1, int(page))
        except (TypeError, ValueError):
            page_num = 1
        try:
            limit_num = max(1, int(limit))
        except (TypeError, ValueError):
            limit_num = 40

        payload, status = lookup_service.fetch_api_listing(
            endpoint_key="posts",
            tags=tags,
            page=str(page_num),
            limit=str(limit_num),
        )
        if status != 200 or not payload.get("ok"):
            return jsonify(payload), status

        posts = payload.get("data", {}).get("posts", [])
        last_seen_dt = lookup_service.parse_post_created_at(last_seen_at) if last_seen_at else None
        filtered_posts = []
        for post in posts if isinstance(posts, list) else []:
            created_dt = lookup_service.parse_post_created_at(post.get("created_at"))
            if last_seen_dt is not None and created_dt is not None and created_dt <= last_seen_dt:
                continue
            post["library_state"] = watch_import_state(post)
            filtered_posts.append(post)

        hit_last_seen_boundary = len(filtered_posts) < len(posts if isinstance(posts, list) else [])
        filtered_posts.sort(
            key=lambda post: lookup_service.parse_post_created_at(post.get("created_at")) or dt.datetime.min
        )
        payload["data"] = {"posts": filtered_posts}
        payload["watch"] = {
            "tags": tags,
            "last_seen_at": last_seen_at,
            "page": page_num,
            "limit": limit_num,
            "has_prev_page": page_num > 1,
            "has_next_page": len(posts if isinstance(posts, list) else []) >= limit_num and not hit_last_seen_boundary,
            "source_count": len(posts if isinstance(posts, list) else []),
            "returned_count": len(filtered_posts),
        }
        return jsonify(payload), 200

    @flask_app.post("/lookup/api")
    def lookup_api_route():
        data = request.get_json(silent=True) or {}
        payload, status = lookup_service.fetch_api_listing(
            endpoint_key=data.get("endpoint", ""),
            tags=data.get("tags", ""),
            page=data.get("page", ""),
            limit=data.get("limit", ""),
            pool_id=data.get("pool_id", ""),
            search_query=data.get("search_query", ""),
        )
        return jsonify(payload), status

    @flask_app.post("/lookup/import")
    def lookup_import_route():
        data = request.get_json(silent=True) or {}
        post = data.get("post")
        if not isinstance(post, dict):
            return jsonify({"ok": False, "message": "Missing post payload"}), 400

        post_id = post.get("id")
        created_at = lookup_service.parse_post_created_at(post.get("created_at"))
        if created_at is None:
            return jsonify({"ok": False, "message": "Invalid or missing post created_at"}), 400

        file_info = post.get("file") if isinstance(post.get("file"), dict) else {}
        source_url = file_info.get("url")
        ext = str(file_info.get("ext") or "").strip().lower()
        if not source_url:
            return jsonify({"ok": False, "message": "Post has no downloadable file URL"}), 400
        if not ext:
            ext = Path(str(source_url)).suffix.lower().lstrip(".")
        if not ext:
            return jsonify({"ok": False, "message": "Could not determine file extension"}), 400
        ext = "." + ext.lstrip(".")
        if media_type_for_ext(ext) == "other":
            return jsonify({"ok": False, "message": f"Unsupported media type: {ext}"}), 400

        download_payload, status = lookup_service.download_external_media(source_url)
        if status != 200 or not download_payload.get("ok"):
            return jsonify(download_payload), status

        target_dir = RESULT_DIR / "imported" / created_at.strftime("%Y") / created_at.strftime("%m")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = created_at.strftime("%d-%m-%Y_%H-%M-%S")
        base_name = normalized_upload_name(f"e621_post_{post_id or 'resource'}")
        target_path = unique_path(target_dir, f"{base_name}@{stamp}{ext}")

        try:
            target_path.write_bytes(download_payload["bytes"])
        except Exception as exc:
            return jsonify({"ok": False, "message": f"Saving file failed: {exc}"}), 500

        rel_path = safe_rel_path(target_path)
        raw_hit = {
            "post_id": post_id,
            "score": 100.0,
            "post": {
                "posts": {
                    "id": post.get("id"),
                    "created_at": post.get("created_at"),
                    "updated_at": post.get("updated_at"),
                    "uploader_id": post.get("uploader_id"),
                    "score": (post.get("score") or {}).get("total") if isinstance(post.get("score"), dict) else post.get("score"),
                    "up_score": (post.get("score") or {}).get("up") if isinstance(post.get("score"), dict) else None,
                    "down_score": (post.get("score") or {}).get("down") if isinstance(post.get("score"), dict) else None,
                    "source": "\n".join(post.get("sources") or []) if isinstance(post.get("sources"), list) else post.get("sources"),
                    "md5": file_info.get("md5"),
                    "rating": post.get("rating"),
                    "image_width": file_info.get("width"),
                    "image_height": file_info.get("height"),
                    "tag_string": " ".join((post.get("tags") or {}).get("general") or []) if isinstance(post.get("tags"), dict) else "",
                    "tag_string_artist": " ".join((post.get("tags") or {}).get("artist") or []) if isinstance(post.get("tags"), dict) else "",
                    "tag_string_character": " ".join((post.get("tags") or {}).get("character") or []) if isinstance(post.get("tags"), dict) else "",
                    "tag_string_copyright": " ".join((post.get("tags") or {}).get("copyright") or []) if isinstance(post.get("tags"), dict) else "",
                    "tag_string_species": " ".join((post.get("tags") or {}).get("species") or []) if isinstance(post.get("tags"), dict) else "",
                    "tag_string_meta": " ".join((post.get("tags") or {}).get("meta") or []) if isinstance(post.get("tags"), dict) else "",
                    "tag_string_lore": " ".join((post.get("tags") or {}).get("lore") or []) if isinstance(post.get("tags"), dict) else "",
                    "fav_count": post.get("fav_count"),
                    "file_ext": file_info.get("ext"),
                    "parent_id": (post.get("relationships") or {}).get("parent_id") if isinstance(post.get("relationships"), dict) else None,
                    "has_active_children": (post.get("relationships") or {}).get("has_active_children") if isinstance(post.get("relationships"), dict) else False,
                    "change_seq": post.get("change_seq"),
                    "approver_id": post.get("approver_id"),
                    "tag_count_general": len(((post.get("tags") or {}).get("general") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "tag_count_artist": len(((post.get("tags") or {}).get("artist") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "tag_count_character": len(((post.get("tags") or {}).get("character") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "tag_count_copyright": len(((post.get("tags") or {}).get("copyright") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "file_size": file_info.get("size"),
                    "tag_count_species": len(((post.get("tags") or {}).get("species") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "comment_count": post.get("comment_count"),
                    "description": post.get("description"),
                    "duration": post.get("duration"),
                    "updated_at": post.get("updated_at"),
                    "is_deleted": (post.get("flags") or {}).get("deleted") if isinstance(post.get("flags"), dict) else False,
                    "is_pending": (post.get("flags") or {}).get("pending") if isinstance(post.get("flags"), dict) else False,
                    "is_flagged": (post.get("flags") or {}).get("flagged") if isinstance(post.get("flags"), dict) else False,
                    "tag_count": sum(len(v or []) for v in (post.get("tags") or {}).values()) if isinstance(post.get("tags"), dict) else 0,
                    "tag_count_meta": len(((post.get("tags") or {}).get("meta") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "tag_count_invalid": len(((post.get("tags") or {}).get("invalid") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "tag_count_lore": len(((post.get("tags") or {}).get("lore") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "bit_flags": 0,
                    "has_children": (post.get("relationships") or {}).get("has_children") if isinstance(post.get("relationships"), dict) else False,
                    "tag_count_contributor": len(((post.get("tags") or {}).get("contributor") or [])) if isinstance(post.get("tags"), dict) else 0,
                    "video_samples": (((post.get("sample") or {}).get("alternates") or {}).get("samples")) if isinstance(post.get("sample"), dict) else {},
                    "has_sample": (post.get("sample") or {}).get("has") if isinstance(post.get("sample"), dict) else False,
                    "has_visible_children": (post.get("relationships") or {}).get("has_active_children") if isinstance(post.get("relationships"), dict) else False,
                    "children_ids": (post.get("relationships") or {}).get("children") if isinstance(post.get("relationships"), dict) else [],
                    "pool_ids": post.get("pools") or [],
                    "is_favorited": post.get("is_favorited"),
                    "file_url": file_info.get("url"),
                    "sample_url": (post.get("sample") or {}).get("url") if isinstance(post.get("sample"), dict) else None,
                    "preview_file_url": (post.get("preview") or {}).get("url") if isinstance(post.get("preview"), dict) else None,
                }
            }
        }
        resource_payload = {
            "file_name": target_path.name,
            "rel_path": rel_path,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": "e621_api",
            "result": {
                "http_status": 200,
                "match_count": 1,
                "result_urls": [f"https://e621.net/posts/{post_id}"] if post_id else [],
                "raw": [raw_hit],
            },
        }
        resource_payload["summary"] = lookup_service.summarize_resource(resource_payload)
        lookup_service.upsert_cached_resource(resource_payload)

        return jsonify({
            "ok": True,
            "message": "Imported into library",
            "rel_path": rel_path,
            "abs_path": str(target_path),
            "post_id": post_id,
            "created_at": created_at.isoformat(sep=" "),
        }), 200

    @flask_app.post("/lookup/import-legacy")
    def lookup_import_legacy_route():
        back = request.form.get("back", url_for("lookup_view"))
        summary = lookup_service.import_legacy_cache_tree()
        if not summary.get("ok"):
            flash(summary.get("message") or "Legacy lookup import failed.")
            return redirect(back)

        message = (
            f"Legacy lookup import complete. Files scanned: {summary.get('files_found', 0)}. "
            f"Files imported: {summary.get('files_imported', 0)}. "
            f"Rows imported: {summary.get('rows_imported', 0)}. "
            f"Rows skipped: {summary.get('rows_skipped', 0)}."
        )
        failures = summary.get("failures") or []
        if failures:
            message += f" Failures: {len(failures)}."
        flash(message)
        return redirect(back)

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
        page_size = request.form.get("page_size", str(PAGE_SIZE))

        base_dir, filtered = filtered_scope_items(year, month, day, q, media)
        if base_dir is None:
            flash("Pick a year or month before fetching missing reverse-search data.")
            return redirect(url_for("index", q=q, year=year, month=month, day=day, media=media, sort=sort_order, page=page, page_size=page_size))

        image_items = [item for item in filtered if item["kind"] == "image"]
        fetched = 0
        skipped = 0
        failed = 0
        rate_limited = 0

        for item in image_items:
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
                rate_limited += 1
                failed += 1
                continue
            if payload.get("cached"):
                skipped += 1
            else:
                fetched += 1

        message = (
            f"Reverse-search batch complete. Matching images: {len(image_items)}. "
            f"Fetched missing: {fetched}. Already cached: {skipped}. Failed: {failed}."
        )
        if rate_limited:
            message += f" ShortLimitReached responses: {rate_limited}."
        flash(message)
        return redirect(url_for("index", q=q, year=year, month=month, day=day, media=media, sort=sort_order, page=page, page_size=page_size))

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
