import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger("rclone")

# Low-memory flags: one transfer at a time, small buffers.
UPLOAD_FLAGS = [
    "--transfers", "1",
    "--checkers", "2",
    "--buffer-size", "8M",
    "--drive-chunk-size", "32M",
    "--retries", "3",
    "--low-level-retries", "10",
]


class RcloneError(RuntimeError):
    pass


def _tail(data: bytes, limit: int = 500) -> str:
    text = data.decode(errors="ignore").strip()
    return text[-limit:] or "unknown rclone error"


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def join(remote: str, folder: str, rel: str = "") -> str:
    """Build 'remote:folder/rel' safely."""
    if folder:
        return f"{remote}:{folder}/{rel}" if rel else f"{remote}:{folder}"
    return f"{remote}:{rel}"


async def run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "rclone", "--log-level", "ERROR", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await proc.communicate()
    except BaseException:
        _kill(proc)
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RcloneError(_tail(err))
    return out.decode(errors="ignore")


async def copy_local(src: Path, target: str) -> None:
    await run("copyto", str(src), target, *UPLOAD_FLAGS)


async def copyurl(url: str, dest_dir: str) -> None:
    await run("copyurl", url, dest_dir, "--auto-filename", *UPLOAD_FLAGS)


async def rcat(target: str, chunks: AsyncIterator[bytes]) -> None:
    """Pipe an async byte iterator into `rclone rcat` (no full file in RAM)."""
    proc = await asyncio.create_subprocess_exec(
        "rclone", "--log-level", "ERROR", "rcat", target,
        *UPLOAD_FLAGS, "--streaming-upload-cutoff", "8M",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    err_task = asyncio.create_task(proc.stderr.read())
    try:
        async for chunk in chunks:
            proc.stdin.write(chunk)
            await proc.stdin.drain()
        proc.stdin.close()
        code = await proc.wait()
    except (BrokenPipeError, ConnectionResetError):
        code = await proc.wait()
    except BaseException:
        _kill(proc)
        err_task.cancel()
        await proc.wait()
        raise
    finally:
        aclose = getattr(chunks, "aclose", None)
        if aclose:
            try:
                await aclose()
            except Exception:
                pass
    err = await err_task
    if code != 0:
        raise RcloneError(_tail(err))
