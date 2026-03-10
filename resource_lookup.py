import datetime as dt
import json
import mimetypes
import os
import urllib.request
import uuid
from base64 import b64encode
from pathlib import Path
from urllib.error import HTTPError, URLError

from env_loader import load_env_file

load_env_file()


class ReverseSearchService:
    def __init__(self, result_dir, media_type_for_ext, safe_rel_path):
        self.result_dir = Path(result_dir).resolve()
        self.media_type_for_ext = media_type_for_ext
        self.safe_rel_path = safe_rel_path
        self.config_path = Path(__file__).resolve().parent / "reverse_search_config.json"
        self.iqdb_url = os.getenv("RESOURCE_E621_IQDB_URL", "https://e621.net/iqdb_queries.json")
        self.login = os.getenv("E621_LOGIN", "").strip()
        self.api_key = os.getenv("E621_API_KEY", "").strip()
        self.user_agent = os.getenv(
            "RESOURCE_E621_USER_AGENT",
            "CosmosGalleryManager/1.0 (by Codex integration)",
        ).strip()
        self.timeout = max(5, int(os.getenv("RESOURCE_E621_TIMEOUT", "25")))
        self.ui_config = self.load_ui_config()

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

    def image_lookup_cache_path(self, image_path):
        return image_path.parent / "data.json"

    def load_dir_lookup_cache(self, image_path):
        cache_path = self.image_lookup_cache_path(image_path)
        if not cache_path.exists():
            return {"resources": {}}
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return {"resources": {}}
        if not isinstance(payload, dict):
            return {"resources": {}}
        resources = payload.get("resources")
        if not isinstance(resources, dict):
            payload["resources"] = {}
        return payload

    def save_dir_lookup_cache(self, image_path, payload):
        cache_path = self.image_lookup_cache_path(image_path)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

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
        cache_payload = self.load_dir_lookup_cache(image_path)
        resources = cache_payload.get("resources", {})
        if not isinstance(resources, dict):
            return None
        resource = resources.get(image_path.name)
        if not isinstance(resource, dict):
            return None
        summary = resource.get("summary")
        if not isinstance(summary, dict):
            summary = self.summarize_resource(resource)
            resource["summary"] = summary
            resources[image_path.name] = resource
            self.save_dir_lookup_cache(image_path, cache_payload)
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

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = None

        if http_status == 429:
            return {"error": "ShortLimitReached", "http_status": http_status}
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

        cache_payload = self.load_dir_lookup_cache(abs_path)
        resource_key = abs_path.name
        resources = cache_payload.setdefault("resources", {})

        if not force and resource_key in resources:
            return {
                "ok": True,
                "cached": True,
                "resource_key": resource_key,
                "data": resources[resource_key],
                "cache_path": str(self.image_lookup_cache_path(abs_path)),
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
        resources[resource_key] = resource_payload
        cache_payload["updated_at"] = now
        cache_payload["directory"] = str(abs_path.parent)
        self.save_dir_lookup_cache(abs_path, cache_payload)

        return {
            "ok": True,
            "cached": False,
            "resource_key": resource_key,
            "data": resource_payload,
            "cache_path": str(self.image_lookup_cache_path(abs_path)),
        }, 200
