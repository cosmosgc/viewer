import os
import sys
from pathlib import Path

from flask import Flask

from env_loader import load_env_file
from viewer_support import IMAGE_EXTS, VIDEO_EXTS

load_env_file()

try:
    import pystray
except Exception:
    pystray = None

try:
    from PIL import Image, ExifTags
except Exception:
    Image = None
    ExifTags = None

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

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "resource-viewer-secret")
PAGE_SIZE = max(1, int(os.getenv("RESOURCE_PAGE_SIZE", "120")))
