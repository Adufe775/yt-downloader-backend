# YouTube Downloader Backend

This is a FastAPI backend for downloading YouTube videos (MP4) and audio (MP3) using `yt-dlp`.

## Features
- Download YouTube videos in **MP4** format
- Extract audio in **MP3** format
- Serve files directly from the backend
- Simple REST API you can connect any frontend to

## API Endpoints

### `GET /download`
Download a video or audio file.

**Query Parameters:**
- `url` (string, required) → YouTube video URL
- `format` (string, optional, default=`mp4`) → `mp4` or `mp3`

**Example:**
