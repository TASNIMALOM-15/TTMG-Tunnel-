import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}
MEDIA_ATTRS = ("video", "document", "audio", "animation", "voice", "video_note")
_DEFAULT_EXT = {
    "video": ".mp4",
    "audio": ".mp3",
    "voice": ".ogg",
    "animation": ".mp4",
    "video_note": ".mp4",
    "document": "",
}
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

ProgressCb = Callable[[int, int], Awaitable[None]]


class UserError(Exception):
    """Non-retryable error whose message is safe to show to the user."""


def safe_name(name: Optional[str], default: str = "file") -> str:
    name = _BAD_CHARS.sub("_", (name or "").strip()).strip(". ")
    return name[:180] or default


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def progress_text(label: str, done: int, total: int, started: float) -> str:
    elapsed = max(time.time() - started, 0.1)
    speed = done / elapsed
    if total:
        pct = min(done / total, 1.0)
        filled = int(pct * 12)
        eta = (total - done) / speed if speed > 0 else 0
        return (
            f"{label}\n[{'█' * filled}{'░' * (12 - filled)}] {pct * 100:.1f}%\n"
            f"{human(done)} / {human(total)} • {human(speed)}/s • ETA {fmt_time(eta)}"
        )
    return f"{label}\n{human(done)} • {human(speed)}/s"


def media_of(message):
    """Return (attr, media_object) for a Telegram message, or None."""
    for attr in MEDIA_ATTRS:
        obj = getattr(message, attr, None)
        if obj:
            return attr, obj
    return None


def media_name(message, attr: str, obj) -> str:
    name = getattr(obj, "file_name", None)
    if not name:
        name = f"{attr}_{message.id}{_DEFAULT_EXT.get(attr, '')}"
    return safe_name(name)


def ensure_space(path: Path, needed: int) -> None:
    free = shutil.disk_usage(path).free
    if needed and needed + 300 * 1024 * 1024 > free:
        raise UserError(
            f"ডিস্কে জায়গা কম (দরকার {human(needed)}, ফাঁকা {human(free)})। "
            "এই ফাইলটি ফ্রি-টিয়ারে প্রসেস করা যাবে না।"
        )


def _filename_from_response(resp: aiohttp.ClientResponse, url: str) -> str:
    cd = resp.content_disposition
    if cd and cd.filename:
        return safe_name(cd.filename)
    tail = unquote(urlparse(str(resp.url)).path.rsplit("/", 1)[-1])
    return safe_name(tail, "download.bin")


async def download_url(url: str, workdir: Path, cb: Optional[ProgressCb] = None) -> Path:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"লিংক থেকে ডাউনলোড ব্যর্থ (HTTP {resp.status})")
            total = resp.content_length or 0
            workdir.mkdir(parents=True, exist_ok=True)
            ensure_space(workdir, total)
            dest = workdir / _filename_from_response(resp, url)
            done = 0
            with open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if cb:
                        await cb(done, total)
    return dest


async def probe_video(path: Path) -> tuple[int, int, int]:
    """Return (duration_seconds, width, height) using ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        data = json.loads(out or b"{}")
        stream = (data.get("streams") or [{}])[0]
        duration = int(float((data.get("format") or {}).get("duration") or 0))
        return duration, int(stream.get("width") or 0), int(stream.get("height") or 0)
    except Exception:
        return 0, 0, 0


async def make_thumb(path: Path, duration: int) -> Optional[Path]:
    thumb = path.with_name(path.stem + ".thumb.jpg")
    seek = str(max(1, min(duration // 10, 10)))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-ss", seek, "-i", str(path),
            "-frames:v", "1", "-vf", "scale=320:-2", str(thumb),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        return None
    return thumb if thumb.exists() and thumb.stat().st_size > 0 else None
