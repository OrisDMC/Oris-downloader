"""Local YouTube download service. Run: python tools/youtube-server/server.py."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
sys.path.insert(0, str(RUNTIME))
CACHE = ROOT / ".cache"
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION = 6 * 60 * 60
JOBS: dict[str, dict] = {}
LOCK = threading.RLock()
SLOTS = threading.BoundedSemaphore(2)
ORIGINS = set(os.environ.get("YOUTUBE_ALLOWED_ORIGINS", ",".join(
    f"http://{host}:{port}" for host in ("localhost", "127.0.0.1")
    for port in (8081, 8082, 8083, 19006)
)).split(","))


def youtube_url(value: str) -> str:
    """Canonicalize only YouTube video links; never pass arbitrary URLs to yt-dlp."""
    value = value.strip()
    if re.fullmatch(r"[\w-]{11}", value, flags=re.ASCII):
        return f"https://www.youtube.com/watch?v={value}"
    parsed = urlparse(value if "://" in value else "https://" + value)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("Enter a valid YouTube video link.")
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch" else ""
        match = re.fullmatch(r"/(?:shorts|embed|live)/([\w-]{11})/?", parsed.path, flags=re.ASCII)
        if match:
            video_id = match[1]
    else:
        raise ValueError("Only YouTube video links are supported.")
    if not re.fullmatch(r"[\w-]{11}", video_id, flags=re.ASCII):
        raise ValueError("Enter a valid YouTube video link.")
    return f"https://www.youtube.com/watch?v={video_id}"


def ffmpeg_path() -> str:
    configured = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if configured:
        return configured
    import imageio_ffmpeg
    bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    target = RUNTIME / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(bundled, target)
    return str(target)


def command() -> list[str]:
    args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cache-dir",
            "--no-playlist", "--no-warnings", "--socket-timeout", "12",
            "--retries", "2", "--fragment-retries", "2", "--extractor-retries", "2",
            "--ffmpeg-location", ffmpeg_path()]
    if shutil.which("node"):
        args += ["--js-runtimes", "node"]
    # Never inspect browser profiles automatically. Only use a file explicitly configured by the owner.
    cookie_file = os.environ.get("YOUTUBE_COOKIE_FILE")
    saved_export = RUNTIME / "youtube-cookies.txt"
    if not cookie_file and saved_export.is_file():
        cookie_file = str(saved_export)
    if cookie_file:
        args += ["--cookies", cookie_file]
    return args


def process_env() -> dict:
    return {**os.environ, "PYTHONPATH": str(RUNTIME) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "PYTHONIOENCODING": "utf-8"}


def run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=process_env(), timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def public_error(error: str) -> str:
    lowered = error.lower()
    if "sign in" in lowered or "bot" in lowered:
        return "YouTube requires sign-in for this connection. Configure YOUTUBE_COOKIE_FILE on the download service with an authorized cookies.txt file, or try another connection."
    if "private" in lowered or "unavailable" in lowered or "removed" in lowered:
        return "This video is unavailable or private. Choose a public video."
    if "format" in lowered and "available" in lowered:
        return "This quality is unavailable. Inspect the video again or select a lower quality."
    return "Could not prepare a playable download. Try again later or choose another video."


def inspect_video(url: str) -> dict:
    result = run_process(command() + ["--dump-single-json", "--skip-download", "--", url], 75)
    if result.returncode:
        raise RuntimeError(public_error(result.stderr))
    data = json.loads(result.stdout)
    if data.get("is_live") or not data.get("duration"):
        raise ValueError("Live streams and videos without a known duration cannot be downloaded.")
    if data["duration"] > MAX_DURATION:
        raise ValueError("Choose a video shorter than six hours.")
    formats = data.get("formats", [])
    has_m4a = any(f.get("ext") == "m4a" and f.get("acodec") not in (None, "none") for f in formats)
    heights = sorted({int(f["height"]) for f in formats if f.get("height") and f.get("ext") == "mp4"
                      and f.get("vcodec") not in (None, "none")
                      and (f.get("acodec") not in (None, "none")
                           or (has_m4a and f.get("vcodec", "").startswith("avc1")))})
    qualities = []
    for requested in (360, 480, 720, 1080):
        available = [height for height in heights if height <= requested]
        if available and not any(option["height"] == max(available) for option in qualities):
            qualities.append({"quality": str(requested), "height": max(available)})
    return {"videoId": data["id"], "title": data.get("title", "YouTube video"),
            "author": data.get("uploader", "YouTube"), "thumbnailUrl": data.get("thumbnail", ""),
            "sourceUrl": url, "durationSeconds": data["duration"], "qualities": qualities,
            "audioAvailable": any(f.get("acodec") not in (None, "none") for f in formats)}


def verify_file(path: Path, media_format: str, max_height: int, expected_duration: float | None = None) -> dict:
    """Decode the entire completed file, not just its header, before exposing it to clients."""
    size = path.stat().st_size
    if size < 1024 or size > MAX_BYTES:
        raise ValueError("The downloaded file is empty, too small, or exceeds 2 GB.")
    result = run_process([ffmpeg_path(), "-hide_banner", "-v", "info", "-xerror", "-err_detect", "explode",
                          "-i", str(path), "-map", "0:a:0", *(["-map", "0:v:0"] if media_format == "video" else []),
                          "-f", "null", "-"], 300)
    if result.returncode:
        raise ValueError("The downloaded file is damaged or is missing sound/video.")
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]) if match else 0
    if not duration or (expected_duration and duration < expected_duration - max(2, expected_duration * 0.02)):
        raise ValueError("The downloaded media is incomplete.")
    resolution = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", result.stderr)
    height = int(resolution[2]) if resolution else None
    if media_format == "video" and (height is None or height > max_height):
        raise ValueError("The downloaded video does not match the selected quality.")
    return {"fileSize": size, "durationSeconds": duration, "height": height}


def update_job(job_id: str, **changes) -> None:
    with LOCK:
        JOBS[job_id].update(changes)


def compact_video(path: Path) -> Path:
    """Keep the original video bit-for-bit; use lower bitrate mono audio for speech."""
    compact = path.with_name(path.stem + "-compact.mp4")
    result = run_process([ffmpeg_path(), "-hide_banner", "-v", "error", "-i", str(path),
                          "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac",
                          "-b:a", "32k", "-ac", "1", "-ar", "24000", "-aac_coder", "fast", "-movflags", "+faststart", "-y", str(compact)], 600)
    if result.returncode:
        compact.unlink(missing_ok=True)
        raise ValueError("Could not create the smaller file. Try Original sound instead.")
    if compact.stat().st_size >= path.stat().st_size:
        compact.unlink()
        return path
    return compact


def prepare_job(job_id: str, url: str, media_format: str, quality: int, size_mode: str = "compact") -> None:
    directory = CACHE / job_id
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # A second extraction refreshes expiring URLs on each complete attempt.
        for attempt in range(2):
            update_job(job_id, state="downloading", message=f"Downloading (attempt {attempt + 1}/2)…", progress=0)
            args = command() + ["--newline", "--progress", "--no-simulate", "--max-filesize", str(MAX_BYTES),
                "--match-filter", f"duration <= {MAX_DURATION} & !is_live", "--abort-on-unavailable-fragments",
                "-o", str(directory / "media.%(ext)s"),
                "--progress-template", 'download:PROGRESS {"written":%(progress.downloaded_bytes)j,"total":%(progress.total_bytes)j}',
                "--print", 'after_move:RESULT {"filepath":%(filepath)j,"title":%(title)j,"duration":%(duration)j}']
            if media_format == "audio":
                args += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "128K"]
            else:
                args += ["-f", f"bestvideo[height<={quality}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]",
                         "--merge-output-format", "mp4"]
            process = subprocess.Popen(args + ["--", url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=process_env(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            timer = threading.Timer(600, process.kill)
            timer.start()
            output = None
            download_error = "The download source did not return a complete file."
            try:
                for line in process.stdout:
                    if line.startswith("PROGRESS "):
                        progress = json.loads(line[9:])
                        total, written = progress.get("total") or 0, progress.get("written") or 0
                        update_job(job_id, progress=min(95, written / total * 95) if total else 0)
                    elif line.startswith("RESULT "):
                        output = json.loads(line[7:])
                    elif line.startswith("ERROR:"):
                        download_error = public_error(line)
                process.wait()
            finally:
                timer.cancel()
                process.stdout.close()
            try:
                if process.returncode or not output:
                    raise ValueError(download_error)
                path = Path(output["filepath"]).resolve()
                if path.parent != directory.resolve() or path.suffix not in (".mp4", ".mp3"):
                    raise ValueError("Invalid output file.")
                original_path = path
                if media_format == "video" and size_mode == "compact":
                    update_job(job_id, state="checking", message="Reducing audio size for speech; keeping video unchanged…", progress=95)
                    path = compact_video(path)
                update_job(job_id, state="checking", message="Checking the complete video and sound…", progress=96)
                details = verify_file(path, media_format, quality, output.get("duration"))
                if path != original_path:
                    original_path.unlink(missing_ok=True)
                title = output.get("title") or "YouTube media"
                filename = (re.sub(r'[^\w .-]', '_', title, flags=re.ASCII)[:100].strip() or "YouTube media") + path.suffix
                update_job(job_id, state="ready", message="Ready to save", progress=100, path=str(path),
                           title=title, filename=filename, downloadPath=f"/api/files/{job_id}", **details)
                return
            except (ValueError, OSError):
                if attempt == 1:
                    raise
                for partial in directory.iterdir():
                    if partial.is_file():
                        partial.unlink(missing_ok=True)
        raise ValueError("Download failed.")
    except Exception as error:
        update_job(job_id, state="error", message=str(error) if isinstance(error, ValueError) else public_error(str(error)), progress=0)
        shutil.rmtree(directory, ignore_errors=True)
    finally:
        update_job(job_id, finished=time.time())
        SLOTS.release()


def prune_jobs() -> None:
    with LOCK:
        for job_id, job in list(JOBS.items()):
            if job.get("finished", float("inf")) < time.time() - 3600:
                shutil.rmtree(CACHE / job_id, ignore_errors=True)
                del JOBS[job_id]


def clean_cache_periodically() -> None:
    while True:
        prune_jobs()
        # Also remove expired folders from previous runs. Only touch UUID directories in our cache.
        if CACHE.exists():
            with LOCK:
                active = set(JOBS)
            for directory in CACHE.iterdir():
                if (directory.is_dir() and not directory.is_symlink()
                        and re.fullmatch(r"[a-f0-9]{32}", directory.name)
                        and directory.name not in active and directory.stat().st_mtime < time.time() - 3600):
                    shutil.rmtree(directory, ignore_errors=True)
        time.sleep(300)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def allowed(self) -> bool:
        if "*" in ORIGINS or os.environ.get("YOUTUBE_ALLOWED_ORIGINS") == "*":
            return True
        return not self.headers.get("Origin") or self.headers["Origin"] in ORIGINS

    def end_headers(self):
        origin = self.headers.get("Origin")
        if "*" in ORIGINS or os.environ.get("YOUTUBE_ALLOWED_ORIGINS") == "*":
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def reply(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.reply(200 if self.allowed() else 403, {})

    def do_POST(self):
        if not self.allowed():
            return self.reply(403, {"error": "Origin not allowed."})
        prune_jobs()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get_content_type() != "application/json":
                return self.reply(400, {"error": "A small JSON request is required."})
            body = json.loads(self.rfile.read(length))
            url = youtube_url(body.get("url", ""))
            if self.path == "/api/info":
                if not SLOTS.acquire(blocking=False):
                    return self.reply(429, {"error": "Download service is busy. Try again shortly."})
                try:
                    try:
                        info = inspect_video(url)
                    except ValueError as error:
                        return self.reply(400, {"error": str(error)})
                    return self.reply(200, info)
                finally:
                    SLOTS.release()
            if self.path != "/api/jobs":
                return self.reply(404, {"error": "Not found."})
            media_format = body.get("format", "video")
            quality = str(body.get("quality", "480"))
            size_mode = body.get("sizeMode", "compact")
            if (media_format not in ("video", "audio") or quality not in ("360", "480", "720", "1080")
                    or size_mode not in ("compact", "original")):
                raise ValueError("Choose a supported format and quality.")
            if not SLOTS.acquire(blocking=False):
                return self.reply(429, {"error": "Download service is busy. Try again shortly."})
            job_id = uuid.uuid4().hex
            with LOCK:
                JOBS[job_id] = {"id": job_id, "state": "downloading", "message": "Starting download…", "progress": 0}
            threading.Thread(target=prepare_job, args=(job_id, url, media_format, int(quality), size_mode), daemon=True).start()
            self.reply(202, {"id": job_id})
        except (ValueError, TypeError, AttributeError):
            self.reply(400, {"error": "Enter a valid YouTube video link and download options."})
        except subprocess.TimeoutExpired:
            self.reply(504, {"error": "YouTube did not respond in time. Try again later."})
        except RuntimeError as error:
            self.reply(502, {"error": str(error)})
        except Exception as error:
            self.reply(502, {"error": public_error(str(error))})

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {"error": "Origin not allowed."})
        prune_jobs()
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        match = re.fullmatch(r"/api/(jobs|files)/([a-f0-9]{32})", self.path)
        if not match:
            return self.reply(404, {"error": "Not found."})
        with LOCK:
            job = dict(JOBS.get(match[2], {}))
        if not job:
            return self.reply(404, {"error": "Download expired. Start it again."})
        if match[1] == "jobs":
            return self.reply(200, {key: value for key, value in job.items() if key not in ("path", "finished")})
        if job["state"] != "ready":
            return self.reply(409, {"error": "Download is not ready."})
        try:
            with Path(job["path"]).open("rb") as media:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg" if job["filename"].endswith(".mp3") else "video/mp4")
                self.send_header("Content-Disposition", f'attachment; filename="{job["filename"]}"')
                self.send_header("Content-Length", str(job["fileSize"]))
                self.end_headers()
                shutil.copyfileobj(media, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    import yt_dlp  # Fail at startup with a useful missing-dependency error.
    ffmpeg_path()
    if not shutil.which("node"):
        print("Note: Node.js was not detected in PATH; using yt-dlp built-in runtime.", flush=True)
    host = os.environ.get("YOUTUBE_HOST", "0.0.0.0" if (os.environ.get("PORT") or os.environ.get("SPACE_ID")) else "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("YOUTUBE_PORT", "8787"))
    print(f"YouTube download service running at: http://{host}:{port}", flush=True)
    threading.Thread(target=clean_cache_periodically, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
