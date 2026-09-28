"""Vidqora backend: Flask + yt-dlp.

Serves the website at / and these endpoints (used by index.html):
  GET /api/info?url=...                 -> title, author, duration, thumbnail, formats
  GET /api/download?url=...&format=ID   -> the video/audio file as a download
"""
import glob
import os
import re
import shutil
import tempfile
from typing import Any, cast
from urllib.parse import urlparse

try:
    import yt_dlp  # type: ignore[reportMissingImports]
except ModuleNotFoundError as exc:
    if exc.name != "yt_dlp":
        raise
    yt_dlp = None
from flask import Flask, jsonify, request, send_file, send_from_directory  # type: ignore[import-not-found]
try:
    from flask_cors import CORS  # type: ignore[import-not-found, reportMissingImports]
except ModuleNotFoundError as exc:
    if exc.name != "flask_cors":
        raise

    # Keep the API usable when the optional CORS package is unavailable.
    def CORS(app, origins="*"):
        @app.after_request
        def add_cors_headers(response):
            response.headers["Access-Control-Allow-Origin"] = origins
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            return response
from flask_limiter import Limiter  # type: ignore[import-not-found]
from flask_limiter.util import get_remote_address  # type: ignore[import-not-found]

app = Flask(__name__)
# In production set ALLOWED_ORIGIN to your website, e.g. https://vidqora.com
CORS(app, origins=os.getenv("ALLOWED_ORIGIN", "*"))
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

# Only these sites are accepted (also protects your server from misuse).
ALLOWED = (
    "instagram.com", "instagr.am", "youtube.com", "youtu.be", "facebook.com",
    "fb.watch", "tiktok.com", "twitter.com", "x.com", "pinterest.com", "pin.it",
    "reddit.com", "redd.it", "snapchat.com", "linkedin.com", "vimeo.com",
    "threads.net", "dailymotion.com", "dai.ly",
)
MAX_MB = int(os.getenv("MAX_MB", "500"))      # biggest file allowed
COOKIES = os.getenv("COOKIES_FILE")            # optional cookies.txt path


def check_url(url):
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    ok = any(host == d or host.endswith("." + d) for d in ALLOWED)
    if p.scheme not in ("http", "https") or not ok:
        raise ValueError("This link is not from a supported platform.")
    return url


def base_opts():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 15}
    if COOKIES and os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def require_yt_dlp():
    if yt_dlp is None:
        raise RuntimeError(
            "yt-dlp is not installed. Install it with: python -m pip install yt-dlp"
        )


def fmt_time(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(b):
    return f"~{b / 1048576:.0f} MB" if b else ""


def selector(fid):
    if fid == "mp3":
        return "bestaudio/best"
    m = re.fullmatch(r"v(\d{3,5})", fid or "")
    if not m:
        raise ValueError("Unknown format.")
    h = int(m.group(1))
    return (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/"
            f"b[height<={h}]/b")


def download_error(exc):
    """Return a useful public error without exposing extractor internals."""
    detail = str(exc).lower()
    if "max-filesize" in detail or "file is larger than max-filesize" in detail:
        return "This video is larger than the 500 MB limit.", 413
    if any(term in detail for term in ("private video", "login required", "sign in", "confirm you are human")):
        return "This video is private or requires a login.", 422
    if "ffmpeg" in detail:
        return "FFmpeg is required to combine the selected video and audio formats.", 500
    return "Download failed. Check that the video is public and try again.", 422


@app.errorhandler(429)
def too_many(_):
    return jsonify(error="Too many requests. Please wait a minute and try again."), 429


@app.get("/")
def home():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/health")
def health():
    return jsonify(ok=True, name="Vidqora API")


@app.get("/api/info")
@limiter.limit("15/minute")
def info():
    try:
        require_yt_dlp()
        url = check_url(request.args.get("url"))
        downloader = cast(Any, yt_dlp).YoutubeDL(
            {**base_opts(), "skip_download": True}
        )
        with downloader as y:
            d = y.extract_info(url, download=False)
        if d.get("entries"):
            d = next(iter(d["entries"]))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception:
        return jsonify(error="Could not read this link. Make sure the video is public."), 422

    vids = [f for f in d.get("formats", []) if f.get("height") and f.get("vcodec") != "none"]
    vids.sort(key=lambda f: f["height"], reverse=True)
    preview_formats = [
        f for f in vids
        if f.get("url") and urlparse(f["url"]).scheme in ("http", "https")
    ]
    preview_formats.sort(
        key=lambda f: (f.get("acodec") != "none", f.get("ext") == "mp4", f["height"]),
        reverse=True,
    )
    seen, formats = set(), []
    for f in vids:
        q = min(f["height"], f.get("width") or f["height"])  # 720x1280 reel -> 720p
        if q in seen or len(seen) >= 4:
            continue
        seen.add(q)
        formats.append({"id": f"v{f['height']}", "label": f"MP4 {q}p",
                        "size": fmt_size(f.get("filesize") or f.get("filesize_approx"))})
    if not formats:
        formats.append({"id": "v9999", "label": "MP4 best quality", "size": ""})
    formats.append({"id": "mp3", "label": "MP3 audio", "size": ""})

    return jsonify(
        title=d.get("title") or "Your video",
        author=d.get("uploader") or d.get("channel") or "",
        duration=fmt_time(d.get("duration")),
        thumbnail=d.get("thumbnail") or "",
        preview=preview_formats[0]["url"] if preview_formats else "",
        formats=formats,
    )


@app.get("/api/download")
@limiter.limit("6/minute")
def download():
    tmp = tempfile.mkdtemp(prefix="vq_")
    try:
        require_yt_dlp()
        url = check_url(request.args.get("url"))
        fid = request.args.get("format", "")
        opts = {
            **base_opts(),
            "format": selector(fid),
            "outtmpl": os.path.join(tmp, "video.%(ext)s"),
            "max_filesize": MAX_MB * 1024 * 1024,
            "merge_output_format": "mp4",
        }
        if fid == "mp3":
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3", "preferredquality": "192"}]
        with cast(Any, yt_dlp).YoutubeDL(opts) as y:
            d = y.extract_info(url, download=True)
        files = [f for f in glob.glob(os.path.join(tmp, "video.*")) if not f.endswith(".part")]
        if not files:
            raise RuntimeError("no file (maybe larger than the limit)")
        path = max(files, key=os.path.getsize)
    except ValueError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error=str(e)), 400
    except Exception as exc:
        app.logger.warning("Download failed: %s", exc)
        shutil.rmtree(tmp, ignore_errors=True)
        message, status = download_error(exc)
        return jsonify(error=message), status

    name = re.sub(r"[^\w\- ]+", "", d.get("title") or "vidqora").strip()[:60] or "vidqora"
    resp = send_file(path, as_attachment=True, download_name=name + os.path.splitext(path)[1])
    resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))  # delete after sending
    return resp


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
    )