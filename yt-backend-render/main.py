"""
Backend API v1 — Video + Channel Flow
Stack: FastAPI + yt-dlp + SQLite + (optional) YouTube Data API v3

Endpoints
- GET  /video_info?url=... [&save=1]     → video + channel metadata (optionally save channel)
- GET  /download?url=...                  → stream video file, auto-delete after
- GET  /channels                          → list saved channels (from SQLite)
- GET  /channels/{channel_id}/videos      → list recent videos from channel (needs YT_API_KEY)

Run
  uvicorn main:app --reload --port 8000

Env
  setx YT_API_KEY "YOUR_KEY"   # Windows (then open new terminal)
  export YT_API_KEY=YOUR_KEY    # macOS/Linux
"""

import os
import re
import uuid
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

import yt_dlp

# ---------------------------- Config ----------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")
DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
YOUTUBE_API_KEY = os.environ.get("YT_API_KEY")  # optional but required for /channels/{id}/videos

# ---------------------------- App ----------------------------
app = FastAPI(title="VideoHub API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # during development; tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------- DB ----------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            title TEXT,
            thumbnail TEXT,
            saved_at TEXT,
            last_used_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_db()

# ---------------------------- Helpers ----------------------------

def clean_youtube_url(url: str) -> str:
    # keep only base watch/shorts/youtu.be with the video id
    url = url.strip()
    # If shorts URL, convert to watch
    m = re.search(r"https?://(?:www\.)?youtube\.com/shorts/([\w-]{6,})", url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    # Normal watch or youtu.be
    m = re.search(r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]{6,})", url)
    return m.group(1) if m else url


def extract_info(url: str) -> Dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    # Map response
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "upload_date": info.get("upload_date"),
        "webpage_url": info.get("webpage_url"),
    }


def upsert_channel(ch_id: str, title: Optional[str], thumb: Optional[str]):
    if not ch_id:
        return
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM channels WHERE id=?", (ch_id,))
    if cur.fetchone():
        cur.execute(
            "UPDATE channels SET title=COALESCE(?, title), thumbnail=COALESCE(?, thumbnail), last_used_at=? WHERE id=?",
            (title, thumb, now, ch_id),
        )
    else:
        cur.execute(
            "INSERT INTO channels (id, title, thumbnail, saved_at, last_used_at) VALUES (?, ?, ?, ?, ?)",
            (ch_id, title, thumb, now, now),
        )
    conn.commit()
    conn.close()


# ---------------------------- Routes ----------------------------

@app.get("/")
async def root():
    return {"ok": True, "hasYTApiKey": bool(YOUTUBE_API_KEY)}


@app.get("/video_info")
async def video_info(url: str = Query(...), save: int = Query(0, description="1 to save channel")):
    """Return video + channel metadata. Optionally save the channel."""
    try:
        url = clean_youtube_url(url)
        data = extract_info(url)
        if save == 1 and data.get("channel_id"):
            upsert_channel(data["channel_id"], data.get("uploader"), data.get("thumbnail"))
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to extract: {e}")


@app.get("/download")
async def download(url: str = Query(...)):
    """Stream video to client and auto-delete the temporary file."""
    try:
        url = clean_youtube_url(url)
        # also capture channel for saving in DB
        try:
            info = extract_info(url)
            upsert_channel(info.get("channel_id"), info.get("uploader"), info.get("thumbnail"))
            title_for_name = (info.get("title") or "video").strip().replace("\n", " ")[:100]
        except Exception:
            info = {}
            title_for_name = "video"

        unique_id = uuid.uuid4().hex
        # Build an output path; yt-dlp will set actual extension
        out_base = os.path.join(DOWNLOADS_DIR, f"{unique_id}")
        ydl_opts = {
            "outtmpl": out_base + ".%(ext)s",
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=True)
            final_path = ydl.prepare_filename(result)
            # handle merged case → enforce .mp4
            if ydl.params.get("merge_output_format") and not final_path.endswith(".mp4"):
                base, _ = os.path.splitext(final_path)
                merged = base + ".mp4"
                if os.path.exists(merged):
                    final_path = merged

        def stream_and_delete():
            with open(final_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            try:
                os.remove(final_path)
                # remove empty downloads dir leftovers
                # (ignore if there are other files)
            except Exception:
                pass

        filename_safe = re.sub(r"[^\w\- .]", "_", title_for_name) + ".mp4"
        headers = {"Content-Disposition": f"attachment; filename={filename_safe}"}
        return StreamingResponse(stream_and_delete(), media_type="video/mp4", headers=headers)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/channels")
async def list_channels():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id as channel_id, title as channel_title, thumbnail, saved_at, last_used_at FROM channels ORDER BY last_used_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"channels": rows}


@app.get("/channels/{channel_id}/videos")
async def channel_videos(channel_id: str, page_token: Optional[str] = None, max_results: int = 20):
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=400, detail="Server missing YT_API_KEY env var for channel listing")
    import requests

    # 1) get uploads playlist
    ch = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "contentDetails,snippet", "id": channel_id, "key": YOUTUBE_API_KEY},
        timeout=20,
    )
    if ch.status_code != 200:
        raise HTTPException(status_code=ch.status_code, detail=ch.text)
    items = ch.json().get("items", [])
    if not items:
        raise HTTPException(status_code=404, detail="Channel not found")
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ch_title = items[0]["snippet"]["title"]
    ch_thumb = items[0]["snippet"]["thumbnails"]["default"]["url"]

    # 2) list uploads
    pl = requests.get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        params={
            "part": "snippet,contentDetails",
            "playlistId": uploads,
            "maxResults": max_results,
            "pageToken": page_token or "",
            "key": YOUTUBE_API_KEY,
        },
        timeout=20,
    )
    if pl.status_code != 200:
        raise HTTPException(status_code=pl.status_code, detail=pl.text)
    data = pl.json()

    videos = []
    for it in data.get("items", []):
        sn = it.get("snippet", {})
        cd = it.get("contentDetails", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
        videos.append(
            {
                "videoId": cd.get("videoId"),
                "title": sn.get("title"),
                "thumbnail": thumb,
                "publishedAt": sn.get("publishedAt"),
                "channelTitle": sn.get("channelTitle"),
            }
        )

    # ensure channel exists in our DB
    upsert_channel(channel_id, ch_title, ch_thumb)

    return {"channel": {"id": channel_id, "title": ch_title, "thumbnail": ch_thumb}, "videos": videos, "nextPageToken": data.get("nextPageToken")}
