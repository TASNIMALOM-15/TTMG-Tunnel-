import re
from pathlib import Path
from typing import AsyncIterator, Optional

import config
import rclone_utils as rc

REMOTE = "gdrive"

_HOST_RE = re.compile(r"https?://(?:drive|docs)\.google\.com/", re.I)
_ID_PATTERNS = (
    r"/file/d/([A-Za-z0-9_-]{10,})",
    r"/folders/([A-Za-z0-9_-]{10,})",
    r"[?&]id=([A-Za-z0-9_-]{10,})",
    r"/d/([A-Za-z0-9_-]{10,})",
)


def is_gdrive_link(url: str) -> bool:
    return bool(_HOST_RE.match(url)) and extract_id(url) is not None


def extract_id(url: str) -> Optional[str]:
    for pattern in _ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def target(rel: str = "") -> str:
    return rc.join(REMOTE, config.GDRIVE_FOLDER, rel)


async def upload_local(path: Path, rel: Optional[str] = None) -> str:
    dest = target(rel or path.name)
    await rc.copy_local(path, dest)
    return dest


async def upload_stream(name: str, chunks: AsyncIterator[bytes]) -> str:
    dest = target(name)
    await rc.rcat(dest, chunks)
    return dest


async def download_to_dir(file_id: str, local_dir: Path) -> list[Path]:
    """Copy a Drive file/folder (by ID) to a local directory via rclone."""
    local_dir.mkdir(parents=True, exist_ok=True)
    await rc.run("backend", "copyid", f"{REMOTE}:", file_id, f"{local_dir}/")
    files = [p for p in local_dir.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError(
            "Drive থেকে ফাইল পাওয়া যায়নি। লিংকটি 'Anyone with the link' কিনা চেক করুন।"
        )
    return files


async def copy_id_to(file_id: str, remote_dir: str) -> None:
    """Server-side stream: Drive file/folder ID -> any other rclone remote dir."""
    await rc.run("backend", "copyid", f"{REMOTE}:", file_id, f"{remote_dir}/")
