import os
import sys

from viewer_context import (
    APP_DIR,
    DEFAULT_RESULT_DIR,
    ExifTags,
    IMAGE_EXTS,
    INBOX_DIR,
    INBOX_MARKER,
    PAGE_SIZE,
    PINNED_JSON,
    RESULT_DIR,
    TEMPLATE_DIR,
    VIDEO_EXTS,
    Image,
    app,
    pystray,
)
from viewer_routes import register_routes
from viewer_status import detect_local_ip, detect_public_ip, run_status_window
from viewer_store import (
    apply_filters,
    apply_sort,
    build_chart_series,
    calendar_path,
    collect_calendar,
    ensure_inbox_dir,
    filtered_scope_items,
    list_inbox_candidates,
    load_pins,
    lookup_service,
    paginate,
    safe_rel_path,
    save_pins,
    scan_resources,
    scan_timeline_counts,
    selected_base_dir,
)
from viewer_support import (
    FILENAME_DT_RE,
    exif_datetime_for_image,
    list_subdirs,
    media_type_for_ext,
    metadata_datetime_for_file,
    normalized_upload_name,
    parse_dt_from_name,
    path_sort_key,
    unique_path,
)

register_routes(app)


if __name__ == "__main__":
    ensure_inbox_dir()
    host = os.getenv("RESOURCE_HOST", "0.0.0.0")
    port = int(os.getenv("RESOURCE_PORT", "5000"))
    debug = os.getenv("RESOURCE_DEBUG", "1").lower() in {"1", "true", "yes", "on"}
    gui_mode = os.getenv("RESOURCE_GUI", "1").lower() in {"1", "true", "yes", "on"}
    if getattr(sys, "frozen", False) and gui_mode:
        run_status_window(app, host=host, port=port, debug=debug)
    else:
        app.run(host=host, port=port, debug=debug)
