import datetime as dt
import re
from pathlib import Path

from werkzeug.utils import secure_filename

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
FILENAME_DT_RE = re.compile(r"@(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})")


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


def path_sort_key(path):
    return (0 if path.name.isdigit() else 1, path.name.lower())


def list_subdirs(path, reverse=False):
    if not path.exists() or not path.is_dir():
        return []
    return sorted((child for child in path.iterdir() if child.is_dir()), key=path_sort_key, reverse=reverse)


def exif_datetime_for_image(path, image_module):
    if image_module is None:
        return None
    try:
        with image_module.open(path) as img:
            exif = getattr(img, "getexif", lambda: None)()
            if not exif:
                return None
            raw = exif.get(36867) or exif.get(306)
            if not raw:
                return None
            return dt.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def metadata_datetime_for_file(path, image_module=None):
    parsed = parse_dt_from_name(path.name)
    if parsed:
        return parsed
    if media_type_for_ext(path.suffix.lower()) == "image":
        exif_dt = exif_datetime_for_image(path, image_module)
        if exif_dt:
            return exif_dt
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return dt.datetime.now()


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
