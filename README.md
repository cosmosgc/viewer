# Cosmos's Galery Manager

A local Flask web app for organizing and browsing image/video files stored in a date-based folder structure.

The app is designed for large media collections and supports:
- fast scoped browsing by year/month/day
- search and filtering (image/video)
- rich modal preview with keyboard shortcuts
- pin/unpin favorites
- upload through browser
- ingest/import from a local drop folder
- delete and download actions

## Project Structure

- `resource_viewer.py`: Flask backend and all routes
- `templates/`: HTML templates for index, upload, pinned, and ingest pages
- `resource_inbox/`: local drop folder for file ingestion
- `pinned_files.json`: persisted list of pinned absolute file paths

## How Files Are Organized

Managed media is stored under:

`result/YYYY/MM/DD`

Files are renamed to:

`name@DD-MM-YYYY_HH-MM-SS.ext`

This keeps filenames consistent and sortable by capture/import date.

## Main Features

1. Calendar navigation
- Browse by year/month/day.
- Scope loading to keep large collections responsive.

2. Search and filtering
- Search by filename or relative path.
- Filter by media type: all, image, or video.

3. Sorting
- Date newest/oldest
- Size largest/smallest
- Name A-Z / Z-A
- Path A-Z / Z-A
- Type A-Z / Z-A

4. Modal viewer
- Previous/next browsing
- Video autoplay toggle (saved in browser localStorage)
- Fullscreen, play/pause, mute, skip controls
- Metadata tags (name, path, size, date, type, resolution, duration)
- Delete current file from modal

5. Pinned files
- Pin/unpin media from index cards
- Dedicated pinned view page

6. Upload page
- Upload multiple images/videos from browser
- Files are copied into the date-based result tree

7. Ingest folder
- Put files into `resource_inbox/`
- Open **Ingest Folder** page and click **Process Inbox**
- Files are moved into `result/YYYY/MM/DD` and renamed
- Date source priority:
  - filename timestamp (`@DD-MM-YYYY_HH-MM-SS`)
  - EXIF date for images (if Pillow is available)
  - filesystem modified time fallback

## Routes

- `GET /`: main browser page
- `GET /upload`, `POST /upload`: upload UI and upload handler
- `GET /ingest`, `POST /ingest`: inbox processing UI and handler
- `GET /pinned`: pinned files view
- `POST /pin`: toggle pin
- `POST /delete`: delete a file by relative path
- `GET /media/<path>`: inline media serving
- `GET /download/<path>`: download media as attachment

## Run Locally

1. Install dependencies:

```bash
pip install flask pillow
```

2. Set optional environment variables (defaults shown):
- `RESOURCE_RESULT_DIR=../result`
- `RESOURCE_PINNED_JSON=./pinned_files.json`
- `RESOURCE_INBOX_DIR=./resource_inbox`
- `RESOURCE_PAGE_SIZE=120`
- `RESOURCE_HOST=0.0.0.0`
- `RESOURCE_PORT=5000`
- `RESOURCE_DEBUG=1`

3. Start:

```bash
python resource_viewer.py
```

Or use:

```bash
run_resource_viewer.bat
```

4. Open:

`http://localhost:5000`

## Notes

- The app only manages known media extensions defined in `resource_viewer.py`.
- `pinned_files.json` stores absolute paths; if files are moved outside the app, stale pin entries are ignored.
- If Pillow is not installed, EXIF-based ingest dating is skipped automatically.
