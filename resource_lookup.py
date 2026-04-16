import datetime as dt
import json
import mimetypes
import os
import sqlite3
import threading
import time
import urllib.request
import uuid
from base64 import b64encode
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.parse import urlparse

from env_loader import load_env_file

load_env_file()


class ReverseSearchService:
    def __init__(self, result_dir, media_type_for_ext, safe_rel_path, db_path):
        self.result_dir = Path(result_dir).resolve()
        self.media_type_for_ext = media_type_for_ext
        self.safe_rel_path = safe_rel_path
        self.db_path = Path(db_path).resolve()
        self.config_path = Path(__file__).resolve().parent / "reverse_search_config.json"
        self.api_base_url = os.getenv("RESOURCE_E621_API_BASE_URL", "https://e621.net").rstrip("/")
        self.iqdb_url = os.getenv("RESOURCE_E621_IQDB_URL", "https://e621.net/iqdb_queries.json")
        self.login = os.getenv("E621_LOGIN", "").strip()
        self.api_key = os.getenv("E621_API_KEY", "").strip()
        self.user_agent = os.getenv(
            "RESOURCE_E621_USER_AGENT",
            "CosmosGalleryManager/1.0 (by Codex integration)",
        ).strip()
        self.timeout = max(5, int(os.getenv("RESOURCE_E621_TIMEOUT", "25")))
        self.min_interval_seconds = max(0.0, float(os.getenv("RESOURCE_E621_MIN_INTERVAL_SECONDS", "1")))
        self._request_spacing_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._last_request_completed_at = None
        self.ui_config = self.load_ui_config()
        self.ensure_lookup_db()

    def build_auth_headers(self, include_content_type=None):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if include_content_type:
            headers["Content-Type"] = include_content_type
        if self.login and self.api_key:
            headers["Authorization"] = "Basic " + b64encode(f"{self.login}:{self.api_key}".encode("utf-8")).decode("ascii")
        return headers

    def perform_json_request(self, url, method="GET", data=None, content_type=None):
        request_data = None
        if data is not None:
            request_data = json.dumps(data).encode("utf-8")
            content_type = content_type or "application/json"
        headers = self.build_auth_headers(include_content_type=content_type)
        req = urllib.request.Request(url, data=request_data, headers=headers, method=method)

        self.acquire_request_slot()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_output = resp.read().decode("utf-8", errors="replace")
                http_status = getattr(resp, "status", 200)
        except HTTPError as exc:
            raw_output = exc.read().decode("utf-8", errors="replace")
            http_status = exc.code
        except URLError as exc:
            return {"ok": False, "message": f"Request failed: {exc.reason}", "http_status": None}, 502
        except Exception as exc:
            return {"ok": False, "message": f"Request failed: {exc}", "http_status": None}, 500
        finally:
            self.release_request_slot()

        try:
            parsed = json.loads(raw_output) if raw_output else None
        except json.JSONDecodeError:
            parsed = None

        if 200 <= http_status < 300:
            return {
                "ok": True,
                "http_status": http_status,
                "data": parsed if parsed is not None else raw_output,
            }, http_status

        message = None
        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("reason")
        if not message:
            message = f"API request failed with status {http_status}"
        return {
            "ok": False,
            "message": message,
            "http_status": http_status,
            "data": parsed if parsed is not None else raw_output,
        }, http_status

    def fetch_api_listing(self, endpoint_key, tags="", page="", limit="", pool_id="", search_query=""):
        endpoint_key = str(endpoint_key or "posts").strip().lower()
        allowed = {"posts", "favorites", "pools", "pool"}
        if endpoint_key not in allowed:
            return {"ok": False, "message": "Unsupported endpoint"}, 400

        params = {}
        tags = str(tags or "").strip()
        page = str(page or "").strip()
        limit = str(limit or "").strip()
        pool_id = str(pool_id or "").strip()
        search_query = str(search_query or "").strip()

        if endpoint_key == "posts":
            path = "/posts.json"
            if tags:
                params["tags"] = tags
        elif endpoint_key == "favorites":
            path = "/favorites.json"
            if tags:
                params["tags"] = tags
        elif endpoint_key == "pools":
            path = "/pools.json"
            if search_query:
                params["search[name_matches]"] = search_query
            if tags:
                params["search[description_matches]"] = tags
        else:
            if not pool_id.isdigit():
                return {"ok": False, "message": "Pool ID is required for pool detail"}, 400
            path = f"/pools/{pool_id}.json"

        if page:
            params["page"] = page
        if limit:
            params["limit"] = limit

        query = urlencode(params, doseq=True)
        url = f"{self.api_base_url}{path}"
        if query:
            url = f"{url}?{query}"

        payload, status = self.perform_json_request(url)
        payload["request"] = {
            "endpoint": endpoint_key,
            "path": path,
            "query": params,
            "url": url,
        }
        return payload, status

    def parse_post_created_at(self, created_at):
        raw = str(created_at or "").strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed

    def download_external_media(self, source_url):
        if not source_url:
            return {"ok": False, "message": "Missing source URL"}, 400
        parsed = urlparse(str(source_url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "message": "Invalid source URL"}, 400

        req = urllib.request.Request(str(source_url), headers=self.build_auth_headers(), method="GET")
        self.acquire_request_slot()
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as resp:
                file_bytes = resp.read()
                http_status = getattr(resp, "status", 200)
                content_type = resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "message": f"Download failed with status {exc.code}: {message[:200]}"}, exc.code
        except URLError as exc:
            return {"ok": False, "message": f"Download failed: {exc.reason}"}, 502
        except Exception as exc:
            return {"ok": False, "message": f"Download failed: {exc}"}, 500
        finally:
            self.release_request_slot()

        if http_status < 200 or http_status >= 300:
            return {"ok": False, "message": f"Download failed with status {http_status}"}, http_status
        return {"ok": True, "bytes": file_bytes, "content_type": content_type}, 200

    def load_ui_config(self):
        default_config = {
            "sidebar_open_by_default": True,
            "show_raw_data_by_default": False,
            "show_raw_toggle": True,
            "field_styles": {},
            "tag_styles": {},
        }
        if not self.config_path.exists():
            return default_config
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            return default_config
        if not isinstance(loaded, dict):
            return default_config
        return {**default_config, **loaded}

    def resolve_media_path(self, rel_path):
        safe_path = Path(rel_path)
        abs_path = (self.result_dir / safe_path).resolve()
        try:
            abs_path.relative_to(self.result_dir)
        except ValueError:
            return None
        return abs_path

    def ensure_lookup_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reverse_lookup_cache (
                    rel_path TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    dir_path TEXT NOT NULL,
                    fetched_at TEXT,
                    source TEXT,
                    result_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    legacy_cache_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reverse_lookup_cache_dir_path ON reverse_lookup_cache(dir_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reverse_lookup_cache_file_name ON reverse_lookup_cache(file_name)"
            )
            conn.commit()

    def db_connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def db_row_to_resource(self, row):
        if row is None:
            return None
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
        except Exception:
            result = {}
        try:
            summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        except Exception:
            summary = {}
        return {
            "file_name": row["file_name"],
            "rel_path": row["rel_path"],
            "fetched_at": row["fetched_at"] or "",
            "source": row["source"] or "",
            "result": result,
            "summary": summary,
        }

    def get_cached_resource_by_rel_path(self, rel_path):
        rel_path = str(rel_path or "").replace("\\", "/").strip()
        if not rel_path:
            return None
        with self._db_lock, self.db_connect() as conn:
            row = conn.execute(
                """
                SELECT rel_path, file_name, dir_path, fetched_at, source, result_json, summary_json
                FROM reverse_lookup_cache
                WHERE rel_path = ?
                """,
                (rel_path,),
            ).fetchone()
        return self.db_row_to_resource(row)

    def upsert_cached_resource(self, resource_payload, legacy_cache_path=""):
        if not isinstance(resource_payload, dict):
            return False
        rel_path = str(resource_payload.get("rel_path") or "").replace("\\", "/").strip()
        file_name = str(resource_payload.get("file_name") or "").strip()
        if not rel_path or not file_name:
            return False
        summary = resource_payload.get("summary")
        if not isinstance(summary, dict):
            summary = self.summarize_resource(resource_payload)
        result = resource_payload.get("result")
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._db_lock, self.db_connect() as conn:
            conn.execute(
                """
                INSERT INTO reverse_lookup_cache (
                    rel_path, file_name, dir_path, fetched_at, source, result_json, summary_json, legacy_cache_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    file_name = excluded.file_name,
                    dir_path = excluded.dir_path,
                    fetched_at = excluded.fetched_at,
                    source = excluded.source,
                    result_json = excluded.result_json,
                    summary_json = excluded.summary_json,
                    legacy_cache_path = excluded.legacy_cache_path,
                    updated_at = excluded.updated_at
                """,
                (
                    rel_path,
                    file_name,
                    str(Path(rel_path).parent).replace("\\", "/"),
                    str(resource_payload.get("fetched_at") or ""),
                    str(resource_payload.get("source") or ""),
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    str(legacy_cache_path or ""),
                    now,
                    now,
                ),
            )
            conn.commit()
        return True

    def count_cached_resources(self):
        with self._db_lock, self.db_connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM reverse_lookup_cache").fetchone()
        return int(row["count"]) if row else 0

    def image_lookup_cache_path(self, image_path):
        return image_path.parent / "data.json"

    def import_legacy_cache_file(self, cache_path):
        cache_path = Path(cache_path)
        if not cache_path.exists() or not cache_path.is_file():
            return {"ok": False, "message": "Legacy cache file not found", "imported": 0, "skipped": 0}
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            return {"ok": False, "message": f"Failed to read {cache_path}: {exc}", "imported": 0, "skipped": 0}
        resources = payload.get("resources", {}) if isinstance(payload, dict) else {}
        if not isinstance(resources, dict):
            return {"ok": False, "message": "Legacy cache file has no resources map", "imported": 0, "skipped": 0}

        imported = 0
        skipped = 0
        rel_dir = self.safe_rel_path(cache_path.parent)
        if not rel_dir:
            return {"ok": False, "message": "Legacy cache directory is outside result dir", "imported": 0, "skipped": 0}

        for file_name, resource in resources.items():
            if not isinstance(resource, dict):
                skipped += 1
                continue
            rel_path = str(resource.get("rel_path") or f"{rel_dir}/{file_name}").replace("\\", "/").strip("/")
            normalized = {
                **resource,
                "file_name": str(resource.get("file_name") or file_name),
                "rel_path": rel_path,
            }
            if not isinstance(normalized.get("summary"), dict):
                normalized["summary"] = self.summarize_resource(normalized)
            if self.upsert_cached_resource(normalized, legacy_cache_path=str(cache_path)):
                imported += 1
            else:
                skipped += 1

        return {"ok": True, "imported": imported, "skipped": skipped, "path": str(cache_path)}

    def import_legacy_cache_tree(self):
        imported_files = 0
        imported_rows = 0
        skipped_rows = 0
        failures = []
        for cache_path in self.result_dir.rglob("data.json"):
            result = self.import_legacy_cache_file(cache_path)
            if result.get("ok"):
                imported_files += 1
                imported_rows += int(result.get("imported") or 0)
                skipped_rows += int(result.get("skipped") or 0)
            else:
                failures.append(result.get("message") or str(cache_path))
        return {
            "ok": True,
            "files_found": imported_files + len(failures),
            "files_imported": imported_files,
            "rows_imported": imported_rows,
            "rows_skipped": skipped_rows,
            "failures": failures,
            "db_path": str(self.db_path),
        }

    def first_raw_hit(self, result):
        raw = result.get("raw") if isinstance(result, dict) else None
        if isinstance(raw, list) and raw:
            return raw[0]
        if isinstance(raw, dict):
            return raw
        return {}

    def extract_post_payload(self, result):
        first_hit = self.first_raw_hit(result)
        post_wrapper = first_hit.get("post") if isinstance(first_hit, dict) else None
        if isinstance(post_wrapper, dict):
            nested = post_wrapper.get("posts")
            if isinstance(nested, dict):
                return nested
        return {}

    def split_tag_string(self, value):
        if not value:
            return []
        return [tag for tag in str(value).split() if tag]

    def build_tag_groups(self, post):
        groups = []
        known_groups = [
            ("artist", "tag_string_artist"),
            ("character", "tag_string_character"),
            ("copyright", "tag_string_copyright"),
            ("species", "tag_string_species"),
            ("general", "tag_string_general"),
            ("meta", "tag_string_meta"),
            ("lore", "tag_string_lore"),
        ]
        for label, field_name in known_groups:
            tags = self.split_tag_string(post.get(field_name))
            if tags:
                groups.append({"label": label, "tags": tags})
        if groups:
            return groups

        general_tags = self.split_tag_string(post.get("tag_string"))
        return [{"label": "general", "tags": general_tags[:80]}] if general_tags else []

    def summarize_resource(self, resource_payload):
        result = resource_payload.get("result") if isinstance(resource_payload, dict) else {}
        first_hit = self.first_raw_hit(result)
        post = self.extract_post_payload(result)
        summary = {
            "post_id": post.get("id") or first_hit.get("post_id"),
            "post_url": None,
            "match_score": round(float(first_hit.get("score", 0)), 2) if first_hit.get("score") is not None else None,
            "up_score": post.get("up_score"),
            "down_score": post.get("down_score"),
            "score": post.get("score"),
            "fav_count": post.get("fav_count"),
            "rating": post.get("rating"),
            "source_url": post.get("source"),
            "file_url": post.get("file_url"),
            "sample_url": post.get("sample_url"),
            "preview_file_url": post.get("preview_file_url"),
            "created_at": post.get("created_at"),
            "updated_at": post.get("updated_at"),
            "md5": post.get("md5"),
            "width": post.get("image_width"),
            "height": post.get("image_height"),
            "file_ext": post.get("file_ext"),
            "file_size": post.get("file_size"),
            "description": post.get("description"),
            "tag_groups": self.build_tag_groups(post),
            "tag_count": post.get("tag_count"),
        }
        if summary["post_id"]:
            summary["post_url"] = f"https://e621.net/posts/{summary['post_id']}"
        return summary

    def cached_resource_data(self, image_path):
        rel_path = self.safe_rel_path(image_path)
        if not rel_path:
            return None
        resource = self.get_cached_resource_by_rel_path(rel_path)
        if resource is None:
            legacy_path = self.image_lookup_cache_path(image_path)
            if legacy_path.exists():
                self.import_legacy_cache_file(legacy_path)
                resource = self.get_cached_resource_by_rel_path(rel_path)
        if not isinstance(resource, dict):
            return None
        summary = resource.get("summary")
        if not isinstance(summary, dict):
            resource["summary"] = self.summarize_resource(resource)
            self.upsert_cached_resource(resource)
        return resource

    def build_multipart_body(self, post_data, file_name, file_bytes, content_type):
        boundary = f"----ResourceViewer{uuid.uuid4().hex}"
        body = bytearray()

        for key, value in post_data.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        return boundary, bytes(body)

    def acquire_request_slot(self):
        if self.min_interval_seconds <= 0:
            return
        self._request_spacing_lock.acquire()
        now = time.monotonic()
        if self._last_request_completed_at is not None:
            elapsed = now - self._last_request_completed_at
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def release_request_slot(self):
        if self.min_interval_seconds <= 0:
            return
        try:
            self._last_request_completed_at = time.monotonic()
        finally:
            self._request_spacing_lock.release()

    def reverse_search_image(self, image_path):
        if not self.login or not self.api_key:
            return {"error": "Authentication required. Set E621_LOGIN and E621_API_KEY."}

        with open(image_path, "rb") as f:
            file_bytes = f.read()

        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        boundary, body = self.build_multipart_body({}, image_path.name, file_bytes, content_type)
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": "Basic "
            + b64encode(f"{self.login}:{self.api_key}".encode("utf-8")).decode("ascii"),
        }
        req = urllib.request.Request(self.iqdb_url, data=body, headers=headers, method="POST")

        self.acquire_request_slot()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_output = resp.read().decode("utf-8", errors="replace")
                http_status = getattr(resp, "status", 200)
        except HTTPError as exc:
            raw_output = exc.read().decode("utf-8", errors="replace")
            http_status = exc.code
        except URLError as exc:
            return {"error": f"Request failed: {exc.reason}"}
        except Exception as exc:
            return {"error": f"Request failed: {exc}"}
        finally:
            self.release_request_slot()

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = None

        if http_status == 429:
            return {
                "error": "ShortLimitReached",
                "message": "Reverse search short-period rate limit reached.",
                "retry_after_seconds": self.min_interval_seconds,
                "http_status": http_status,
            }
        if not raw_output:
            return {"error": "EmptyResult", "http_status": http_status}
        if isinstance(parsed, dict) and parsed.get("message"):
            return {"error": parsed["message"], "http_status": http_status, "raw": parsed}

        posts = parsed.get("posts") if isinstance(parsed, dict) else parsed
        if isinstance(posts, list):
            if not posts:
                return {"error": "NoResults", "http_status": http_status, "raw": parsed}
            urls = []
            for result in posts:
                post_id = result.get("post_id") if isinstance(result, dict) else None
                if post_id:
                    urls.append(f"https://e621.net/posts/{post_id}")
            return {
                "http_status": http_status,
                "match_count": len(posts),
                "result_urls": urls,
                "raw": parsed,
            }

        return {"http_status": http_status, "raw": parsed if parsed is not None else raw_output}

    def get_or_update_lookup_data(self, rel_path, force=False):
        abs_path = self.resolve_media_path(rel_path)
        if abs_path is None:
            return {"ok": False, "message": "Invalid path"}, 400
        if not abs_path.exists() or not abs_path.is_file():
            return {"ok": False, "message": "File not found"}, 404
        if self.media_type_for_ext(abs_path.suffix.lower()) != "image":
            return {"ok": False, "message": "Reverse search is only available for images"}, 400

        resource_key = abs_path.name
        cached_resource = self.cached_resource_data(abs_path)
        if not force and isinstance(cached_resource, dict):
            return {
                "ok": True,
                "cached": True,
                "resource_key": resource_key,
                "data": cached_resource,
                "storage_path": str(self.db_path),
            }, 200

        result = self.reverse_search_image(abs_path)
        now = dt.datetime.now().isoformat(timespec="seconds")
        resource_payload = {
            "file_name": abs_path.name,
            "rel_path": self.safe_rel_path(abs_path),
            "fetched_at": now,
            "source": "e621_iqdb",
            "result": result,
        }
        resource_payload["summary"] = self.summarize_resource(resource_payload)
        self.upsert_cached_resource(resource_payload)

        return {
            "ok": True,
            "cached": False,
            "resource_key": resource_key,
            "data": resource_payload,
            "storage_path": str(self.db_path),
        }, 200
