"""
=============================================================================
Smart Video Downloader - Core Engine
=============================================================================
Supports:
- YouTube (Channels, Shorts, Long, Playlists, Single Videos)
- TikTok (Watermark-free Videos, User Profiles)
- Bilibili (China & Global bilibili.tv / bilibili.com/en, User Spaces)
- Facebook (Reels, Videos, Multiple links)
- Format merging via imageio-ffmpeg
=============================================================================
"""

import os
import re
import sys
import json
import shutil
import time
import threading
import subprocess
from typing import List, Dict, Any, Optional, Callable

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    import yt_dlp
except ImportError:
    raise SystemExit("Please run: pip install yt-dlp")

def get_ffmpeg_path() -> Optional[str]:
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None

FFMPEG_PATH = get_ffmpeg_path()
SHORT_MAX_SECONDS = 90

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\[\d+;\d+m|\[0m')

def clean_text(text: str) -> str:
    return ANSI_ESCAPE.sub('', str(text or '')).strip()

def format_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds <= 0:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE))

def is_facebook(url: str) -> bool:
    return bool(re.search(r"(facebook\.com|fb\.watch|fb\.com|messenger\.com)", url, re.IGNORECASE))

def is_tiktok(url: str) -> bool:
    return bool(re.search(r"(tiktok\.com)", url, re.IGNORECASE))

def is_bilibili(url: str) -> bool:
    return bool(re.search(r"(bilibili\.com|bilibili\.tv|b23\.tv)", url, re.IGNORECASE))

def detect_platform(url: str) -> str:
    if is_youtube(url):
        return "youtube"
    elif is_tiktok(url):
        return "tiktok"
    elif is_facebook(url):
        return "facebook"
    elif is_bilibili(url):
        return "bilibili"
    return "general"

def is_single_video(url: str) -> bool:
    u = url.strip().lower()
    if re.search(r"(youtube\.com/watch\?|youtu\.be/|youtube\.com/shorts/|youtube\.com/live/)", u):
        return True
    if any(k in u for k in ["/watch", "/reel/", "/videos/", "fb.watch/", "/share/r/", "/share/v/"]):
        clean = u.rstrip("/")
        if clean.endswith("/videos") or clean.endswith("/reels"):
            return False
        return True
    if re.search(r"(tiktok\.com/.+/video/|vt\.tiktok\.com|vm\.tiktok\.com|tiktok\.com/t/)", u):
        return True
    if re.search(r"(bilibili\.com/video/|bilibili\.tv/.+/video/|bilibili\.tv/.+/play/|b23\.tv/|bangumi/play/)", u):
        return True
    return False

def clean_channel_url(url: str) -> str:
    u = url.strip()
    if "profile.php" in u and "id=" in u:
        m = re.search(r'id=(\d+)', u)
        if m:
            return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    # Strip query params and subtabs
    u = u.split("?")[0].rstrip("/")
    return re.sub(r"/(videos|shorts|streams|featured|reels|live)$", "", u, flags=re.IGNORECASE)

def extract_all_urls(text: str) -> List[str]:
    return re.findall(r'https?://[^\s"\'<>]+', text)


class ScanLogger:
    def __init__(self, log_cb: Optional[Callable[[str], None]] = None):
        self.log_cb = log_cb
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        if self.log_cb:
            self.log_cb(clean_text(msg))


def get_ydl_base_opts(cookies_browser: Optional[str] = None, log_cb: Optional[Callable] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
        "logger": ScanLogger(log_cb),
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    if cookies_browser and cookies_browser.lower() not in ("none", "no", "", "null"):
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def resolve_video_info(e: Dict[str, Any], default_platform: str = "general") -> Dict[str, Any]:
    u = e.get("url") or e.get("webpage_url")
    vid = str(e.get("id") or "")
    if u and not u.startswith("http") and vid:
        if default_platform == "youtube":
            u = f"https://www.youtube.com/watch?v={vid}"

    thumb = e.get("thumbnail")
    if not thumb and e.get("thumbnails"):
        try:
            thumb = e["thumbnails"][-1].get("url")
        except Exception:
            pass

    title = e.get("title") or vid or "Video"
    dur = e.get("duration")
    dur_str = format_duration(dur)

    vtype = "long"
    if u and ("/shorts/" in u.lower() or "/reel" in u.lower()):
        vtype = "short"
    elif dur and dur <= SHORT_MAX_SECONDS:
        vtype = "short"

    return {
        "id": vid or u,
        "url": u,
        "title": title,
        "thumbnail": thumb or "/static/default_thumb.svg",
        "duration": dur or 0,
        "duration_str": dur_str or ("Short" if vtype == "short" else ""),
        "type": vtype,
        "platform": default_platform,
        "uploader": e.get("uploader") or e.get("channel") or ""
    }


def extract_tiktok_profile_videos(
    url: str,
    limit: Optional[int] = 50,
    log_cb: Optional[Callable[[str], None]] = None
) -> List[Dict[str, Any]]:
    """Scan videos from a TikTok user profile using embed/frontity parser."""
    m = re.search(r'@([a-zA-Z0-9_\.\-]+)', url)
    username = m.group(1) if m else url.strip().split('/')[-1].lstrip('@').split('?')[0]
    if not username:
        return []

    if log_cb:
        log_cb(f"🔍 Fetching TikTok videos for @{username}...")

    videos = []
    embed_url = f"https://www.tiktok.com/embed/@{username}"
    html_text = ""

    try:
        import curl_cffi.requests
        resp = curl_cffi.requests.get(embed_url, impersonate="chrome", timeout=12)
        if resp.status_code == 200:
            html_text = resp.text
    except Exception as ex:
        if log_cb:
            log_cb(f"⚠ curl_cffi notice: {ex}")

    if not html_text:
        try:
            import urllib.request
            req = urllib.request.Request(
                embed_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.tiktok.com/"
                }
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                html_text = r.read().decode("utf-8", "ignore")
        except Exception:
            pass

    if html_text:
        m_state = re.search(r'<script[^>]+\bid=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>(.*?)</script>', html_text)
        if m_state:
            try:
                state_data = json.loads(m_state.group(1))
                source_data = state_data.get("source", {}).get("data", {})
                for k, v in source_data.items():
                    if isinstance(v, dict) and "videoList" in v:
                        vlist = v["videoList"]
                        for it in vlist:
                            vid = it.get("id")
                            if not vid:
                                continue
                            author = it.get("authorUniqueId") or username
                            video_url = f"https://www.tiktok.com/@{author}/video/{vid}"
                            title = it.get("desc") or f"TikTok Video by @{author}"
                            thumb = it.get("dynamicCoverUrl") or it.get("coverUrl") or it.get("originCoverUrl") or "/static/default_thumb.svg"
                            videos.append({
                                "id": str(vid),
                                "url": video_url,
                                "title": title,
                                "thumbnail": thumb,
                                "duration": 0,
                                "duration_str": "Short",
                                "type": "short",
                                "platform": "tiktok",
                                "uploader": f"@{author}"
                            })
                            if limit and len(videos) >= limit:
                                break
                        if videos:
                            break
            except Exception:
                pass

    return videos


def extract_facebook_page_videos(
    url: str,
    limit: Optional[int] = 50,
    log_cb: Optional[Callable[[str], None]] = None
) -> List[Dict[str, Any]]:
    """Scan and list videos and reels from a Facebook Page or Profile using multi-target and mobile SSR parser."""
    clean_url = clean_channel_url(url)
    targets = []
    
    if "profile.php?id=" in clean_url:
        m = re.search(r'id=(\d+)', clean_url)
        pid = m.group(1) if m else ""
        targets = [
            f"https://m.facebook.com/profile.php?id={pid}&v=videos",
            f"https://www.facebook.com/profile.php?id={pid}&sk=reels_tab",
            f"https://www.facebook.com/profile.php?id={pid}&sk=videos",
            f"https://m.facebook.com/profile.php?id={pid}",
            f"https://www.facebook.com/profile.php?id={pid}"
        ]
    else:
        m_slug = re.sub(r'^https?://(?:www\.|m\.|web\.)?facebook\.com/', '', clean_url).strip('/')
        targets = [
            f"https://m.facebook.com/{m_slug}/videos",
            f"https://www.facebook.com/{m_slug}/reels",
            f"https://www.facebook.com/{m_slug}/videos",
            f"https://m.facebook.com/{m_slug}/reels",
            f"https://m.facebook.com/{m_slug}",
            f"https://www.facebook.com/{m_slug}"
        ]
    
    found_urls = []
    seen_urls = set()
    
    try:
        import curl_cffi.requests
        for target in targets:
            if log_cb:
                log_cb(f"🔍 Inspecting Facebook: {target}...")
            try:
                resp = curl_cffi.requests.get(target, impersonate="chrome", timeout=12)
                if resp.status_code != 200:
                    continue
                # Unescape escaped slashes in Facebook JSON payloads
                text = resp.text.replace(r'\/', '/')

                # 1. Match video IDs in GraphQL definitions
                for m in re.finditer(r'"__typename":"(?:Video|Reel)"[^}]+"id":"(\d+)"', text):
                    vid = m.group(1)
                    v_link = f"https://www.facebook.com/watch/?v={vid}"
                    if v_link not in seen_urls:
                        seen_urls.add(v_link)
                        found_urls.append(v_link)

                # 2. Match permalink / url patterns
                for m in re.finditer(r'"(?:permalink_url|url|share_url)":\s*"([^"]+)"', text):
                    val = m.group(1).split('?')[0]
                    if any(x in val for x in ['/reel/', '/videos/', '/watch/', 'fb.watch']):
                        clean_v = val
                        if not clean_v.startswith("http"):
                            clean_v = "https://www.facebook.com" + ("" if clean_v.startswith("/") else "/") + clean_v
                        if clean_v not in seen_urls and not clean_v.endswith('/videos') and not clean_v.endswith('/reels'):
                            seen_urls.add(clean_v)
                            found_urls.append(clean_v)

                # 3. Direct Regex on reel and watch patterns
                for m in re.finditer(r'(?:https?://(?:www\.|m\.)?facebook\.com)?/reel/(\d+)', text):
                    v_link = f"https://www.facebook.com/reel/{m.group(1)}"
                    if v_link not in seen_urls:
                        seen_urls.add(v_link)
                        found_urls.append(v_link)

                for m in re.finditer(r'(?:https?://(?:www\.|m\.)?facebook\.com)?/watch/\?v=(\d+)', text):
                    v_link = f"https://www.facebook.com/watch/?v={m.group(1)}"
                    if v_link not in seen_urls:
                        seen_urls.add(v_link)
                        found_urls.append(v_link)

                # 4. JSON video_id and playback_video_id
                for m in re.finditer(r'"(?:video_id|playback_video_id)":\s*"(\d+)"', text):
                    v_link = f"https://www.facebook.com/watch/?v={m.group(1)}"
                    if v_link not in seen_urls:
                        seen_urls.add(v_link)
                        found_urls.append(v_link)
                            
                if limit and limit > 0 and len(found_urls) >= limit:
                    break
            except Exception:
                continue
    except Exception as ex:
        if log_cb:
            log_cb(f"⚠ Facebook page scanner notice: {ex}")

    videos = []
    for idx, f_url in enumerate(found_urls, 1):
        is_reel = "/reel/" in f_url
        vid_match = re.search(r'(?:reel|videos|v=|watch\?v=)/?(\d+)', f_url)
        clean_id = vid_match.group(1) if vid_match else f"fb_{idx}_{int(time.time())}"
        videos.append({
            "id": clean_id,
            "url": f_url,
            "title": f"Facebook {'Reel' if is_reel else 'Video'} #{idx}",
            "thumbnail": "/static/default_thumb.svg",
            "duration": 0,
            "duration_str": "Reel" if is_reel else "Video",
            "type": "short" if is_reel else "long",
            "platform": "facebook",
            "uploader": "Facebook Creator"
        })
        if limit and limit > 0 and len(videos) >= limit:
            break
            
    return videos


def scan_channel_or_url(
    raw_input: str,
    kind: str = "both",
    limit: Optional[int] = 50,
    cookies_browser: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None
) -> Dict[str, Any]:
    urls = extract_all_urls(raw_input)
    if not urls:
        return {"status": "error", "message": "No valid URL provided.", "videos": []}

    videos: List[Dict[str, Any]] = []
    seen = set()
    overall_title = ""
    primary_platform = "general"

    for raw_url in urls:
        platform = detect_platform(raw_url)
        if primary_platform == "general":
            primary_platform = platform

        base_opts = get_ydl_base_opts(cookies_browser, log_cb)

        # 1. Direct Single Video
        if is_single_video(raw_url):
            if log_cb:
                log_cb(f"🔍 Checking single video: {raw_url}")
            opts = dict(base_opts)
            opts["extract_flat"] = True
            opts["skip_download"] = True
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(raw_url, download=False)
                if info:
                    item = resolve_video_info(info, platform)
                    if item["url"] not in seen:
                        seen.add(item["url"])
                        videos.append(item)
                        if not overall_title:
                            overall_title = item["title"]
            except Exception as ex:
                if log_cb:
                    log_cb(f"⚠ Single video fetch error: {ex}")
                if raw_url not in seen:
                    seen.add(raw_url)
                    videos.append({
                        "id": raw_url,
                        "url": raw_url,
                        "title": "Video Link",
                        "thumbnail": "/static/default_thumb.svg",
                        "duration": 0,
                        "duration_str": "",
                        "type": "video",
                        "platform": platform,
                        "uploader": ""
                    })

        # 2. YouTube Channel or Playlist
        elif platform == "youtube":
            if "playlist?list=" in raw_url:
                if log_cb:
                    log_cb(f"🔍 Scanning YouTube playlist...")
                opts = dict(base_opts)
                opts["extract_flat"] = "in_playlist"
                opts["skip_download"] = True
                if limit and limit > 0:
                    opts["playlistend"] = limit
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        pinfo = ydl.extract_info(raw_url, download=False)
                    if pinfo:
                        if not overall_title:
                            overall_title = pinfo.get("title") or "YouTube Playlist"
                        for e in (pinfo.get("entries") or []):
                            if e:
                                item = resolve_video_info(e, "youtube")
                                if item["url"] and item["url"] not in seen:
                                    seen.add(item["url"])
                                    videos.append(item)
                                    if limit and limit > 0 and len(videos) >= limit:
                                        break
                except Exception as ex:
                    if log_cb:
                        log_cb(f"⚠ Playlist error: {ex}")
            else:
                base = clean_channel_url(raw_url)
                tabs = []
                if kind in ("long", "both"):
                    tabs.append(("Long Videos", base + "/videos"))
                if kind in ("short", "both"):
                    tabs.append(("Shorts", base + "/shorts"))

                for tname, turl in tabs:
                    if log_cb:
                        log_cb(f"🔍 Fetching YouTube {tname} list...")
                    opts = dict(base_opts)
                    opts["extract_flat"] = "in_playlist"
                    opts["skip_download"] = True
                    if limit and limit > 0:
                        opts["playlistend"] = limit * 2
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            cinfo = ydl.extract_info(turl, download=False)
                        if cinfo:
                            if not overall_title:
                                overall_title = cinfo.get("channel") or cinfo.get("uploader") or cinfo.get("title") or "YouTube Channel"
                            for e in (cinfo.get("entries") or []):
                                if e:
                                    item = resolve_video_info(e, "youtube")
                                    if item["url"] and item["url"] not in seen:
                                        seen.add(item["url"])
                                        videos.append(item)
                                        if limit and limit > 0 and len(videos) >= limit:
                                            break
                    except Exception as ex:
                        if log_cb:
                            log_cb(f"⚠ {tname} fetch error: {ex}")
                    if limit and limit > 0 and len(videos) >= limit:
                        break

        # 3. TikTok Profile
        elif platform == "tiktok":
            if log_cb:
                log_cb(f"🔍 Scanning TikTok profile...")
            # Try embed parser bypass first
            tiktok_vids = extract_tiktok_profile_videos(raw_url, limit=limit, log_cb=log_cb)
            if tiktok_vids:
                if not overall_title:
                    overall_title = tiktok_vids[0]["uploader"] or "TikTok Videos"
                for item in tiktok_vids:
                    if item["url"] not in seen:
                        seen.add(item["url"])
                        videos.append(item)
            else:
                opts = dict(base_opts)
                opts["extract_flat"] = True
                opts["skip_download"] = True
                if limit and limit > 0:
                    opts["playlistend"] = limit
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        tinfo = ydl.extract_info(raw_url, download=False)
                    if tinfo:
                        if not overall_title:
                            overall_title = tinfo.get("title") or tinfo.get("uploader") or "TikTok Profile"
                        for e in (tinfo.get("entries") or []):
                            if e:
                                item = resolve_video_info(e, "tiktok")
                                if item["url"] and item["url"] not in seen:
                                    seen.add(item["url"])
                                    videos.append(item)
                                    if limit and limit > 0 and len(videos) >= limit:
                                        break
                except Exception as ex:
                    if log_cb:
                        log_cb(f"⚠ TikTok profile error: {ex}")

        # 4. Bilibili Creator Space
        elif platform == "bilibili":
            if log_cb:
                log_cb(f"🔍 Scanning Bilibili creator space...")
            target_url = raw_url
            if "space.bilibili.com" in target_url and not target_url.rstrip("/").endswith("/video"):
                target_url = target_url.rstrip("/") + "/video"
            opts = dict(base_opts)
            opts["extract_flat"] = True
            opts["skip_download"] = True
            if limit and limit > 0:
                opts["playlistend"] = limit
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    binfo = ydl.extract_info(target_url, download=False)
                if binfo:
                    if not overall_title:
                        overall_title = binfo.get("title") or binfo.get("uploader") or "Bilibili Channel"
                    for e in (binfo.get("entries") or []):
                        if e:
                            item = resolve_video_info(e, "bilibili")
                            if item["url"] and item["url"] not in seen:
                                seen.add(item["url"])
                                videos.append(item)
                                if limit and limit > 0 and len(videos) >= limit:
                                    break
            except Exception as ex:
                if log_cb:
                    log_cb(f"⚠ Bilibili error: {ex}")

        # 5. Facebook Page or Links
        elif platform == "facebook":
            if log_cb:
                log_cb(f"🔍 Scanning Facebook page / reels...")
            fb_vids = extract_facebook_page_videos(raw_url, limit=limit, log_cb=log_cb)
            if fb_vids:
                if not overall_title:
                    overall_title = "Facebook Page Videos"
                for item in fb_vids:
                    if item["url"] not in seen:
                        seen.add(item["url"])
                        videos.append(item)
            else:
                base = clean_channel_url(raw_url)
                tabs = []
                if kind in ("long", "both"):
                    tabs.append(("Videos", base + "/videos"))
                if kind in ("short", "both"):
                    tabs.append(("Reels", base + "/reels"))

                for tname, turl in tabs:
                    if log_cb:
                        log_cb(f"🔍 Scanning Facebook {tname}...")
                    opts = dict(base_opts)
                    opts["extract_flat"] = True
                    opts["skip_download"] = True
                    if limit:
                        opts["playlistend"] = limit
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            finfo = ydl.extract_info(turl, download=False)
                        if finfo:
                            for e in (finfo.get("entries") or []):
                                if e:
                                    item = resolve_video_info(e, "facebook")
                                    if item["url"] and item["url"] not in seen:
                                        seen.add(item["url"])
                                        videos.append(item)
                    except Exception:
                        pass

            if not videos:
                if log_cb:
                    log_cb("💡 Facebook Page Tip: Facebook may require login or block automatic page scraping. You can paste multiple direct video/reel links together separated by spaces or newlines!")

        else:
            opts = dict(base_opts)
            opts["extract_flat"] = True
            opts["skip_download"] = True
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(raw_url, download=False)
                if info:
                    if info.get("entries"):
                        for e in info["entries"]:
                            if e:
                                item = resolve_video_info(e, platform)
                                if item["url"] and item["url"] not in seen:
                                    seen.add(item["url"])
                                    videos.append(item)
                    else:
                        item = resolve_video_info(info, platform)
                        if item["url"] not in seen:
                            seen.add(item["url"])
                            videos.append(item)
            except Exception as ex:
                if log_cb:
                    log_cb(f"⚠ Scan error: {ex}")

        if limit and len(videos) >= limit:
            videos = videos[:limit]
            break

    if limit and limit > 0:
        videos = videos[:limit]

    return {
        "status": "success" if videos else "warning",
        "message": f"Found total {len(videos)} videos." if videos else "No videos found. Tip: For restricted Facebook pages or TikTok accounts, paste direct video/reel links (multiple links supported!).",
        "platform": primary_platform,
        "title": overall_title or "Video List",
        "total": len(videos),
        "videos": videos
    }


def build_download_format(quality: str = "best") -> tuple[str, bool]:
    has_ffmpeg = FFMPEG_PATH is not None
    if quality == "audio_mp3":
        return "bestaudio/best", True
    if not has_ffmpeg:
        return "best[ext=mp4]/best", False
    if quality == "best":
        return "bv*[vcodec^=avc][format_id!*=watermarked]+ba[acodec^=mp4a]/bv*[vcodec^=avc]+ba/bv*[format_id!*=watermarked]+ba/b[ext=mp4]/best", False
    elif quality == "1080p":
        return "bv*[height<=1080][vcodec^=avc]+ba[acodec^=mp4a]/bv*[height<=1080][vcodec^=avc]+ba/bv*[height<=1080]+ba/b[ext=mp4]/best", False
    elif quality == "720p":
        return "bv*[height<=720][vcodec^=avc]+ba[acodec^=mp4a]/bv*[height<=720][vcodec^=avc]+ba/bv*[height<=720]+ba/b[ext=mp4]/best", False
    elif quality == "480p":
        return "bv*[height<=480][vcodec^=avc]+ba[acodec^=mp4a]/bv*[height<=480][vcodec^=avc]+ba/bv*[height<=480]+ba/b[ext=mp4]/best", False
    return "bv*[vcodec^=avc]+ba[acodec^=mp4a]/bv*+ba/b", False


def ensure_mobile_compatibility(filepath: str) -> str:
    """Ensure MP4 video has standard H.264 video, AAC audio, and moov atom at the front (+faststart) for Android."""
    if not filepath or not os.path.exists(filepath):
        return filepath
    if not FFMPEG_PATH or not filepath.lower().endswith(".mp4"):
        return filepath
    try:
        probe = subprocess.run([FFMPEG_PATH, "-i", filepath], capture_output=True, text=True, errors="ignore")
        stderr = probe.stderr

        # Detect video codec
        v_match = re.search(r'Stream #\d+:\d+.*Video:\s*([a-zA-Z0-9_-]+)', stderr)
        vcodec = v_match.group(1).lower() if v_match else ''

        # Detect audio codec
        a_match = re.search(r'Stream #\d+:\d+.*Audio:\s*([a-zA-Z0-9_-]+)', stderr)
        acodec = a_match.group(1).lower() if a_match else ''

        # Android Gallery/Video Player compatibility:
        # Standard phone hardware decoders require H.264 (AVC) and AAC.
        # AV1 (av01), HEVC (hvc1), and VP9 often fail on mobile galleries with "Failed to play video".
        needs_video_transcode = vcodec not in ('h264', 'avc1', 'avc')
        needs_audio_transcode = acodec not in ('aac', 'mp4a')

        with open(filepath, "rb") as f:
            head = f.read(1024 * 1024)
            has_faststart = b"moov" in head

        if not needs_video_transcode and not needs_audio_transcode and has_faststart:
            return filepath

        temp_dir = os.path.dirname(filepath)
        temp_out = os.path.join(temp_dir, f"__fast_{os.path.basename(filepath)}")
        cmd = [FFMPEG_PATH, "-nostdin", "-y", "-i", filepath]

        if needs_video_transcode:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"])
        else:
            cmd.extend(["-c:v", "copy"])

        if needs_audio_transcode:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            cmd.extend(["-c:a", "copy"])

        cmd.extend(["-movflags", "+faststart", temp_out])

        res = subprocess.run(cmd, capture_output=True, timeout=60)
        if res.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 0:
            os.replace(temp_out, filepath)
        elif os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠ ensure_mobile_compatibility notice: {e}")
    return filepath


def download_single_video(
    url: str,
    output_dir: str,
    quality: str = "best",
    cookies_browser: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_event: Optional[threading.Event] = None
) -> Dict[str, Any]:
    os.makedirs(output_dir, exist_ok=True)
    fmt, is_audio = build_download_format(quality)
    out_template = os.path.join(output_dir, "%(title).80B [%(id).16B].%(ext)s")

    last_filename = None

    def progress_hook(d):
        nonlocal last_filename
        if cancel_event and cancel_event.is_set():
            raise Exception("Download canceled by user.")

        if d.get("filename"):
            last_filename = d.get("filename")

        if d["status"] == "downloading" and progress_callback:
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            dl = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            pct = (dl / tot * 100) if tot > 0 else 0

            progress_callback({
                "status": "downloading",
                "percent": round(pct, 1),
                "speed_mb": round(speed / (1024 * 1024), 2) if speed else 0,
                "downloaded_mb": round(dl / (1024 * 1024), 1),
                "total_mb": round(tot / (1024 * 1024), 1),
                "eta_sec": int(eta) if eta else 0,
                "filename": os.path.basename(last_filename or "")
            })
        elif d["status"] == "finished" and progress_callback:
            progress_callback({
                "status": "merging",
                "percent": 100,
                "speed_mb": 0,
                "filename": os.path.basename(last_filename or "")
            })

    ydl_opts: Dict[str, Any] = {
        "format": fmt,
        "format_sort": ["res", "vcodec:h264", "acodec:m4a", "fps", "+size"],
        "outtmpl": out_template,
        "windowsfilenames": True,
        "trim_file_name": 100,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "ignoreerrors": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
        "postprocessor_args": {
            "Merger": ["-c", "copy", "-movflags", "+faststart"],
            "Fixup": ["-movflags", "+faststart"],
        },
        "progress_hooks": [progress_hook],
    }

    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
        if not is_audio:
            ydl_opts["merge_output_format"] = "mp4"

    if is_audio:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    if cookies_browser and cookies_browser.lower() not in ("none", "no", "", "null"):
        ydl_opts["cookiesfrombrowser"] = (cookies_browser,)

    def resolve_final_path(info_dict, ydl_instance):
        final_path = None
        if info_dict:
            reqs = info_dict.get("requested_downloads")
            if reqs and isinstance(reqs, list) and reqs[0].get("filepath"):
                final_path = reqs[0].get("filepath")
            if not final_path:
                final_path = info_dict.get("_filename")
            if not final_path:
                try:
                    final_path = ydl_instance.prepare_filename(info_dict)
                except Exception:
                    pass
        if final_path:
            base, ext = os.path.splitext(final_path)
            target_ext = ".mp3" if is_audio else ".mp4"
            if os.path.exists(base + target_ext):
                return base + target_ext
            if os.path.exists(final_path):
                return final_path
        if last_filename and os.path.exists(last_filename):
            return last_filename
        return final_path or last_filename

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            saved_file = resolve_final_path(info, ydl)
            if saved_file and not is_audio:
                saved_file = ensure_mobile_compatibility(saved_file)
        return {"success": True, "filename": saved_file}
    except Exception as ex:
        err = str(ex).lower()
        if cookies_browser and ("cookie" in err or "permission" in err):
            ydl_opts.pop("cookiesfrombrowser", None)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    saved_file = resolve_final_path(info, ydl)
                    if saved_file and not is_audio:
                        saved_file = ensure_mobile_compatibility(saved_file)
                return {"success": True, "filename": saved_file}
            except Exception as ex2:
                return {"success": False, "error": str(ex2)}
        return {"success": False, "error": str(ex)}
