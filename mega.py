import asyncio
import os
import re
from pathlib import Path
from typing import AsyncIterator, Optional

import config
import rclone_utils as rc

REMOTE = "mega"
MEGARC = "/tmp/.megarc"

_LINK_RE = re.compile(r"https?://mega\.(?:nz|io|co\.nz)/", re.I)


def is_mega_link(url: str) -> bool:
    return bool(_LINK_RE.match(url))


def target(rel: str = "") -> str:
    return rc.join(REMOTE, config.MEGA_FOLDER, rel)


async def upload_local(path: Path, rel: Optional[str] = None) -> str:
    dest = target(rel or path.name)
    await rc.copy_local(path, dest)
    return dest


async def upload_stream(name: str, chunks: AsyncIterator[bytes]) -> str:
    # MEGA backend cannot stream with unknown size, so rclone spools to RCLONE_TEMP_DIR.
    dest = target(name)
    await rc.rcat(dest, chunks)
    return dest


async def download_link(link: str, local_dir: Path) -> list[Path]:
    """Download a public MEGA file/folder link with megatools (megadl)."""
    local_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["megadl", "--no-progress", "--path", str(local_dir)]
    if os.path.exists(MEGARC):
        cmd += ["--config", MEGARC]
    cmd.append(link)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await proc.communicate()
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    if proc.returncode != 0:
        text = (err or out).decode(errors="ignore").strip()
        if "quota" in text.lower() or "bandwidth" in text.lower():
            raise RuntimeError("MEGA ডাউনলোড কোটা শেষ। কিছুক্ষণ পরে আবার চেষ্টা করুন।")
        raise RuntimeError(f"megadl ব্যর্থ: {text[-300:] or 'unknown error'}")
    files = [p for p in local_dir.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError("MEGA লিংক থেকে কোনো ফাইল পাওয়া যায়নি।")
    return files
