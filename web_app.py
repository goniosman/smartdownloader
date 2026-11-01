"""
=============================================================================
Smart Video Downloader - Modern Web & Desktop Server
=============================================================================
Backend: FastAPI + Uvicorn
Supports:
- Scanning channels / profiles / URLs
- Interactive list with checkboxes, thumbnails, metadata
- Real-time download progress with speed, ETA, percent
- Mobile device support over Wi-Fi
- Direct file download to mobile / PC browsers
=============================================================================
"""

import os
import re
import sys
import json
import uuid
import socket
import threading
import time
import urllib.parse
import urllib.request
import mimetypes
import shutil
import subprocess
from typing import List, Dict, Any, Optional

# Ensure APK MIME type is registered properly
mimetypes.add_type("application/vnd.android.package-archive", ".apk")

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import downloader_engine

app = FastAPI(title="Smart Video Downloader Pro")

# Enable CORS for Mobile Apps and Web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR") or os.path.join(os.path.expanduser("~"), "Downloads", "VideoDownloader")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Mount static and templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Global Download Tasks & Completed Files Tracker
tasks_lock = threading.Lock()
download_tasks: Dict[str, Dict[str, Any]] = {}

files_lock = threading.Lock()
completed_files_registry: Dict[str, Dict[str, Any]] = {}

HISTORY_FILE = os.path.join(BASE_DIR, "download_history.json")
history_lock = threading.Lock()

def load_download_history() -> Dict[str, Any]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_download_history(url: str, title: str, video_id: Optional[str] = None):
    with history_lock:
        hist = load_download_history()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {"title": title, "url": url, "downloaded_at": now_str}
        hist[url] = entry
        if video_id:
            hist[str(video_id)] = entry
        clean_match = re.search(r'(?:reel|videos|v=|watch\?v=|\bv/|/v/|youtu\.be/)/?([a-zA-Z0-9_-]+)', url)
        if clean_match:
            hist[clean_match.group(1)] = entry
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠ Failed to save history: {e}")

PUBLIC_TUNNEL_URL: Optional[str] = None
tunnel_process: Optional[subprocess.Popen] = None

DISCOVERY_API_URL = "https://api.restful-api.dev/objects/ff808181a09d98f701a1975e3ab87fd5"

def publish_live_server_info(public_url: Optional[str] = None):
    target_url = public_url or PUBLIC_TUNNEL_URL or ""
    if not target_url:
        return
    try:
        data = {
            "name": "smart_downloader_server",
            "data": {
                "url": target_url,
                "local_ip": get_local_ip(),
                "port": 5000,
                "updated_at": int(time.time())
            }
        }
        req = urllib.request.Request(
            DISCOVERY_API_URL,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "SmartDownloader/1.0"},
            method="PUT"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                print("📡 Published live server endpoint to Auto-Connect Discovery Hub!")
    except Exception as e:
        print(f"⚠ Could not publish to discovery hub: {e}")

def discovery_publisher_loop():
    time.sleep(5)
    while True:
        try:
            publish_live_server_info()
        except Exception:
            pass
        time.sleep(180)

def start_cloudflared_tunnel():
    global PUBLIC_TUNNEL_URL, tunnel_process
    cloud_url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not cloud_url and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        cloud_url = "https://" + os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if cloud_url:
        PUBLIC_TUNNEL_URL = cloud_url.rstrip("/")
        print(f"🌐 Cloud Hosting URL Active: {PUBLIC_TUNNEL_URL}")
        publish_live_server_info(PUBLIC_TUNNEL_URL)
        return

    cf_exe = os.path.join(BASE_DIR, "cloudflared.exe")
    if not os.path.exists(cf_exe):
        cf_exe = shutil.which("cloudflared")
    if not cf_exe or not os.path.exists(cf_exe):
        return

    try:
        tunnel_process = subprocess.Popen(
            [cf_exe, "tunnel", "--url", "http://127.0.0.1:5000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace"
        )
        for line in tunnel_process.stdout:
            m = re.search(r'https://[a-zA-Z0-9\-]+\.trycloudflare\.com', line)
            if m:
                PUBLIC_TUNNEL_URL = m.group(0)
                print("=" * 60)
                print(f"🌐 Cloudflare Online Tunnel Active!")
                print(f"📱 Connect from Mobile Data (Anywhere): {PUBLIC_TUNNEL_URL}")
                print("=" * 60)
                publish_live_server_info(PUBLIC_TUNNEL_URL)
                break
    except Exception as e:
        print(f"⚠ Could not launch cloudflared tunnel: {e}")

@app.on_event("startup")
async def app_startup():
    threading.Thread(target=start_cloudflared_tunnel, daemon=True).start()
    threading.Thread(target=discovery_publisher_loop, daemon=True).start()

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# Request Models
class ScanRequest(BaseModel):
    url: str
    kind: str = "both"  # both, short, long
    limit: Optional[int] = 50
    cookies: Optional[str] = None

class DownloadItem(BaseModel):
    url: str
    title: str
    id: Optional[str] = None

class DownloadBatchRequest(BaseModel):
    items: List[DownloadItem]
    quality: str = "best"
    cookies: Optional[str] = None
    output_dir: Optional[str] = None


APP_VERSION_INFO = {
    "version_code": 7,
    "version_name": "1.6.0",
    "release_date": "2026-11-01",
    "changelog": "Brand new custom App Icon + Clean native mobile experience without any browser/tunnel links.",
    "download_url": "/api/download_apk"
}

@app.get("/static/SmartDownloader.apk")
@app.get("/api/download_apk")
@app.get("/SmartDownloader.apk")
async def serve_apk_file():
    apk_path = os.path.join(BASE_DIR, "SmartDownloader.apk")
    if not os.path.exists(apk_path):
        apk_path = os.path.join(BASE_DIR, "static", "SmartDownloader.apk")
    if not os.path.exists(apk_path):
        raise HTTPException(status_code=404, detail="APK file not found.")

    headers = {
        "Content-Disposition": 'attachment; filename="SmartDownloader.apk"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-cache"
    }
    return FileResponse(
        apk_path,
        media_type="application/vnd.android.package-archive",
        filename="SmartDownloader.apk",
        headers=headers
    )

@app.get("/api/app_version")
async def api_get_app_version():
    return APP_VERSION_INFO

@app.get("/api/settings")
async def api_get_settings():
    return {
        "download_folder": DOWNLOADS_DIR,
        "local_ip": get_local_ip(),
        "version": APP_VERSION_INFO
    }

class SettingsUpdateRequest(BaseModel):
    download_folder: Optional[str] = None

@app.post("/api/settings")
async def api_update_settings(req: SettingsUpdateRequest):
    global DOWNLOADS_DIR
    if req.download_folder and req.download_folder.strip():
        DOWNLOADS_DIR = os.path.abspath(req.download_folder.strip())
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    return {"status": "updated", "download_folder": DOWNLOADS_DIR}


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    local_ip = get_local_ip()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "local_ip": local_ip,
            "public_tunnel_url": PUBLIC_TUNNEL_URL,
            "default_folder": DOWNLOADS_DIR,
            "ffmpeg_active": downloader_engine.FFMPEG_PATH is not None,
            "app_version": APP_VERSION_INFO
        }
    )


@app.get("/api/system_info")
async def get_system_info():
    return {
        "local_ip": get_local_ip(),
        "port": 5000,
        "default_folder": DOWNLOADS_DIR,
        "ffmpeg_active": downloader_engine.FFMPEG_PATH is not None,
        "ffmpeg_path": downloader_engine.FFMPEG_PATH,
        "public_tunnel_url": PUBLIC_TUNNEL_URL
    }


@app.post("/api/scan")
async def api_scan(req: ScanRequest):
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="Please provide a valid URL.")

    scan_logs = []
    def log_cb(msg: str):
        scan_logs.append(msg)

    u_lower = req.url.lower()
    kind = req.kind
    if kind == "both":
        if "/shorts" in u_lower:
            kind = "short"
        elif "/videos" in u_lower:
            kind = "long"
        elif "/reels" in u_lower:
            kind = "short"

    result = downloader_engine.scan_channel_or_url(
        raw_input=req.url.strip(),
        kind=kind,
        limit=req.limit,
        cookies_browser=req.cookies,
        log_cb=log_cb
    )
    result["logs"] = scan_logs

    # Annotate videos that have already been downloaded
    hist = load_download_history()
    for v in result.get("videos", []):
        v_url = v.get("url", "")
        v_id = str(v.get("id", ""))
        clean_match = re.search(r'(?:reel|videos|v=|watch\?v=|\bv/|/v/|youtu\.be/)/?([a-zA-Z0-9_-]+)', v_url)
        c_id = clean_match.group(1) if clean_match else ""
        if v_url in hist or v_id in hist or (c_id and c_id in hist):
            v["already_downloaded"] = True
            hist_entry = hist.get(v_url) or hist.get(v_id) or hist.get(c_id) or {}
            v["downloaded_at"] = hist_entry.get("downloaded_at", "")

    return result


def background_downloader(task_id: str, items: List[Dict[str, str]], quality: str, folder: str, cookies: Optional[str]):
    with tasks_lock:
        task = download_tasks.get(task_id)
        if not task:
            return
        cancel_event = task["cancel_event"]

    total = len(items)
    for idx, item in enumerate(items, 1):
        if cancel_event.is_set():
            with tasks_lock:
                task["status"] = "canceled"
                task["message"] = "Download canceled by user."
            break

        vurl = item.get("url") if isinstance(item, dict) else item.url
        vtitle = (item.get("title") if isinstance(item, dict) else item.title) or "Video"
        vid = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)

        with tasks_lock:
            task["current_index"] = idx
            task["current_title"] = vtitle
            task["current_url"] = vurl
            task["current_id"] = vid
            task["current_percent"] = 0
            task["speed_mb"] = 0
            task["status"] = "downloading"

        def progress_cb(info: Dict[str, Any]):
            with tasks_lock:
                task["current_percent"] = info.get("percent", 0)
                task["speed_mb"] = info.get("speed_mb", 0)
                task["downloaded_mb"] = info.get("downloaded_mb", 0)
                task["total_mb"] = info.get("total_mb", 0)
                task["eta_sec"] = info.get("eta_sec", 0)

        res = downloader_engine.download_single_video(
            url=vurl,
            output_dir=folder,
            quality=quality,
            cookies_browser=cookies,
            progress_callback=progress_cb,
            cancel_event=cancel_event
        )

        with tasks_lock:
            if res.get("success"):
                task["completed_count"] += 1
                actual_path = res.get("filename")
                fname = os.path.basename(actual_path or "")
                file_id = str(uuid.uuid4())[:8]
                if actual_path:
                    with files_lock:
                        completed_files_registry[file_id] = {
                            "path": actual_path,
                            "filename": fname,
                            "title": vtitle
                        }
                # Record to persistent download history
                save_download_history(vurl, vtitle, vid)
                local_ip = get_local_ip()
                encoded_name = urllib.parse.quote(fname)
                dl_url = f"/api/file/{file_id}/{encoded_name}"
                full_dl_url = f"http://{local_ip}:5000/api/file/{file_id}/{encoded_name}"
                task["completed_files"].append({
                    "id": file_id,
                    "video_id": vid,
                    "url": vurl,
                    "title": vtitle,
                    "filename": fname,
                    "download_url": dl_url,
                    "full_download_url": full_dl_url
                })
            else:
                task["failed_count"] += 1
                task["errors"].append({
                    "id": vid,
                    "url": vurl,
                    "title": vtitle,
                    "error": res.get("error", "Error")
                })

    with tasks_lock:
        if task["status"] != "canceled":
            task["status"] = "completed"
            task["message"] = f"Download finished! Completed: {task['completed_count']}, Failed: {task['failed_count']}"


@app.post("/api/download")
async def api_start_download(req: DownloadBatchRequest):
    if not req.items:
        raise HTTPException(status_code=400, detail="Please select at least one video to download.")

    task_id = str(uuid.uuid4())
    folder = req.output_dir or DOWNLOADS_DIR
    os.makedirs(folder, exist_ok=True)

    cancel_event = threading.Event()
    items_data = [{"url": it.url, "title": it.title, "id": it.id} for it in req.items]

    task_info = {
        "task_id": task_id,
        "status": "starting",
        "total": len(items_data),
        "current_index": 0,
        "current_title": "",
        "current_url": "",
        "current_id": "",
        "current_percent": 0,
        "speed_mb": 0,
        "downloaded_mb": 0,
        "total_mb": 0,
        "eta_sec": 0,
        "completed_count": 0,
        "failed_count": 0,
        "completed_files": [],
        "errors": [],
        "folder": folder,
        "cancel_event": cancel_event,
        "message": "Starting download..."
    }

    with tasks_lock:
        download_tasks[task_id] = task_info

    t = threading.Thread(
        target=background_downloader,
        args=(task_id, items_data, req.quality, folder, req.cookies),
        daemon=True
    )
    t.start()

    return {"status": "started", "task_id": task_id, "total": len(items_data)}


@app.get("/api/progress/{task_id}")
async def api_get_progress(task_id: str):
    with tasks_lock:
        task = download_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        data = {k: v for k, v in task.items() if k != "cancel_event"}
    return data


@app.post("/api/cancel/{task_id}")
async def api_cancel_task(task_id: str):
    with tasks_lock:
        task = download_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        task["cancel_event"].set()
        task["status"] = "canceling"
        task["message"] = "Canceling download..."
    return {"status": "canceled", "task_id": task_id}


def create_range_file_response(
    file_path: str,
    request: Request,
    filename: Optional[str] = None,
    media_type: Optional[str] = None,
    as_attachment: bool = True
):
    """Serve file with HTTP 206 Partial Content (Range) support for Android/mobile players."""
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(file_path)
    clean_name = filename or os.path.basename(file_path)
    ascii_name = re.sub(r'[^a-zA-Z0-9\.\-\_ ]', '_', clean_name)
    quoted_name = urllib.parse.quote(clean_name)

    if not media_type:
        media_type = "audio/mpeg" if file_path.lower().endswith(".mp3") else "video/mp4"

    disposition_type = "attachment" if as_attachment else "inline"
    content_disposition = f'{disposition_type}; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted_name}'

    range_header = request.headers.get("range")
    if not range_header or "=" not in range_header:
        headers = {
            "Content-Disposition": content_disposition,
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "X-Content-Type-Options": "nosniff"
        }
        return FileResponse(file_path, headers=headers, media_type=media_type)

    try:
        units, range_val = range_header.strip().split("=", 1)
        if units.strip().lower() != "bytes":
            raise ValueError()

        ranges = range_val.split("-")
        if not ranges[0].strip() and ranges[1].strip():
            # Suffix range: bytes=-500
            suffix = int(ranges[1])
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(ranges[0]) if ranges[0].strip() else 0
            end = int(ranges[1]) if len(ranges) > 1 and ranges[1].strip() else file_size - 1

        if start >= file_size or end >= file_size or start > end:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                }
            )

        content_length = end - start + 1

        def iter_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                bytes_left = content_length
                chunk_size = 128 * 1024
                while bytes_left > 0:
                    read_len = min(chunk_size, bytes_left)
                    data = f.read(read_len)
                    if not data:
                        break
                    bytes_left -= len(data)
                    yield data

        headers = {
            "Content-Disposition": content_disposition,
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Content-Type": media_type,
            "X-Content-Type-Options": "nosniff"
        }
        return StreamingResponse(
            iter_range(),
            status_code=206,
            headers=headers,
            media_type=media_type
        )
    except Exception:
        headers = {
            "Content-Disposition": content_disposition,
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "X-Content-Type-Options": "nosniff"
        }
        return FileResponse(file_path, headers=headers, media_type=media_type)


@app.get("/api/file/{file_id}")
@app.get("/api/file/{file_id}/{filename:path}")
async def api_serve_file_by_id(file_id: str, request: Request, filename: Optional[str] = None):
    with files_lock:
        info = completed_files_registry.get(file_id)
    if not info:
        raise HTTPException(status_code=404, detail="File expired or not found.")
    
    filepath = info["path"]
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found on server disk.")
    
    final_name = filename or info.get("filename") or os.path.basename(filepath)
    return create_range_file_response(filepath, request, filename=final_name)


@app.get("/api/download_file/{filename:path}")
async def api_download_file(filename: str, request: Request):
    unquoted = urllib.parse.unquote(filename)
    file_path = os.path.join(DOWNLOADS_DIR, unquoted)
    if not os.path.exists(file_path):
        for f in os.listdir(DOWNLOADS_DIR):
            if f == unquoted or f.startswith(unquoted):
                file_path = os.path.join(DOWNLOADS_DIR, f)
                break
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    
    clean_name = os.path.basename(file_path)
    return create_range_file_response(file_path, request, filename=clean_name)


@app.post("/api/open_folder")
async def api_open_folder(req: Optional[Dict[str, str]] = None):
    try:
        folder = (req and req.get("folder")) or DOWNLOADS_DIR
        os.makedirs(folder, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)
        return {"status": "opened", "folder": folder}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/browse_folder")
async def api_browse_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected = filedialog.askdirectory(initialdir=DOWNLOADS_DIR)
        root.destroy()
        if selected:
            return {"folder": selected}
        return {"folder": None}
    except Exception:
        return {"folder": None}


@app.get("/api/history")
async def api_get_download_history():
    hist = load_download_history()
    return {"history": hist, "count": len(hist)}


@app.post("/api/history/clear")
async def api_clear_download_history():
    with history_lock:
        try:
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
            return {"status": "cleared"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print("🚀 Smart Video Downloader Server Starting...")
    print(f"💻 Port: {port}")
    print(f"📱 Local IP: http://{local_ip}:{port}")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port)
