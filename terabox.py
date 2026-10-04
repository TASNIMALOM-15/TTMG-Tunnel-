"""TeraBox helper.

TeraBox has no official public API. This module talks to the same web endpoints the
TeraBox website uses, authenticated with your browser cookie (TERABOX_COOKIE).
These endpoints can change without notice, so treat this module as best-effort.
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

import config
import utils
from utils import ProgressCb

APP_ID = "250528"
BASE = "https://www.terabox.com"
UPLOAD_HOST = "https://c-jp.terabox.com/rest/2.0/pcs/superfile2"
CHUNK = 4 * 1024 * 1024

DOMAINS = (
    "terabox.com", "teraboxapp.com", "1024terabox.com", "terabox.app", "4funbox.com",
    "mirrobox.com", "nephobox.com", "freeterabox.com", "momerybox.com",
    "teraboxlink.com", "terafileshare.com", "1024tera.com", "teraboxshare.com",
)


class TeraBoxError(RuntimeError):
    pass


def is_terabox_link(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in DOMAINS):
        return False
    return "/s/" in parsed.path or "surl=" in parsed.query


def _short_url(link: str) -> str:
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    if "surl" in query:
        return "1" + query["surl"][0]
    match = re.search(r"/s/([\w-]+)", parsed.path)
    if match:
        return match.group(1)
    raise utils.UserError("TeraBox লিংকটি বোঝা যায়নি।")


def _cookie() -> str:
    cookie = config.TERABOX_COOKIE
    if not cookie:
        raise utils.UserError("TERABOX_COOKIE সেট করা নেই।")
    return cookie if "=" in cookie else f"ndus={cookie}"


def _session() -> aiohttp.ClientSession:
    headers = {
        "User-Agent": utils.UA,
        "Cookie": _cookie(),
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
    }
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
    return aiohttp.ClientSession(headers=headers, timeout=timeout)


def _base_params() -> dict:
    return {"app_id": APP_ID, "web": "1", "channel": "dubox", "clienttype": "0"}


def _check(data: dict, what: str) -> dict:
    errno = data.get("errno", 0)
    if errno in (-6, -9):
        raise utils.UserError("TeraBox কুকি মেয়াদোত্তীর্ণ/ভুল। নতুন কুকি দিন।")
    if errno != 0:
        raise TeraBoxError(f"{what} ব্যর্থ (errno={errno})")
    return data


async def _get_json(session: aiohttp.ClientSession, url: str, **kwargs) -> dict:
    async with session.get(url, **kwargs) as resp:
        return await resp.json(content_type=None)


async def _post_json(session: aiohttp.ClientSession, url: str, **kwargs) -> dict:
    async with session.post(url, **kwargs) as resp:
        return await resp.json(content_type=None)


async def _tokens(session: aiohttp.ClientSession) -> dict:
    params = {**_base_params(), "fields": '["bdstoken","token"]'}
    data = await _get_json(session, f"{BASE}/api/gettemplatevariable", params=params)
    _check(data, "টোকেন আনা")
    bdstoken = (data.get("result") or {}).get("bdstoken", "")
    if not bdstoken:
        raise utils.UserError("TeraBox কুকি কাজ করছে না (bdstoken পাওয়া যায়নি)।")
    js_token = ""
    try:
        async with session.get(f"{BASE}/main") as resp:
            html = await resp.text()
        match = re.search(r"fn%28%22([0-9A-Fa-f]+)%22%29", html)
        if match:
            js_token = match.group(1)
    except Exception:
        pass
    return {"bdstoken": bdstoken, "jsToken": js_token}


# ------------------------------------------------------------------ download

async def _share_info(session: aiohttp.ClientSession, short: str) -> dict:
    params = {**_base_params(), "shorturl": short, "root": "1"}
    data = await _get_json(session, f"{BASE}/api/shorturlinfo", params=params)
    return _check(data, "শেয়ার লিংকের তথ্য আনা")


async def _list_dir(session: aiohttp.ClientSession, short: str, path: str) -> list[dict]:
    params = {
        **_base_params(), "shorturl": short, "dir": path, "root": "0",
        "page": "1", "num": "1000", "order": "name",
    }
    data = await _get_json(session, f"{BASE}/share/list", params=params)
    _check(data, "ফোল্ডার লিস্ট")
    return data.get("list") or []


async def _walk(session: aiohttp.ClientSession, short: str, entries: list[dict], depth: int = 0) -> list[dict]:
    files: list[dict] = []
    for entry in entries:
        if int(entry.get("isdir", 0)):
            if depth >= 5:
                continue
            children = await _list_dir(session, short, entry["path"])
            files += await _walk(session, short, children, depth + 1)
        else:
            files.append(entry)
    return files


async def _dlink(session: aiohttp.ClientSession, info: dict, entry: dict, js_token: str) -> str:
    if entry.get("dlink"):
        return entry["dlink"]
    params = {
        **_base_params(),
        "sign": info["sign"], "timestamp": info["timestamp"],
        "shareid": info["shareid"], "uk": info["uk"],
        "product": "share", "nozip": "0",
        "fid_list": json.dumps([int(entry["fs_id"])]),
    }
    if js_token:
        params["jsToken"] = js_token
    data = await _get_json(session, f"{BASE}/share/download", params=params)
    _check(data, "ডাউনলোড লিংক আনা")
    link = data.get("dlink")
    if not link:
        raise TeraBoxError("ডাউনলোড লিংক পাওয়া যায়নি।")
    return link


async def _download_file(session: aiohttp.ClientSession, url: str, dest: Path, cb: Optional[ProgressCb]) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with session.get(url, allow_redirects=True) as resp:
        if resp.status >= 400:
            raise TeraBoxError(f"ফাইল ডাউনলোড ব্যর্থ (HTTP {resp.status})")
        total = resp.content_length or 0
        utils.ensure_space(dest.parent, total)
        done = 0
        with open(dest, "wb") as fh:
            async for chunk in resp.content.iter_chunked(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if cb:
                    await cb(done, total)
    return done


async def download(link: str, local_dir: Path, cb: Optional[ProgressCb] = None) -> list[Path]:
    short = _short_url(link)
    local_dir.mkdir(parents=True, exist_ok=True)
    async with _session() as session:
        info = await _share_info(session, short)
        files = await _walk(session, short, info.get("list") or [])
        if not files:
            raise TeraBoxError("লিংকে কোনো ফাইল পাওয়া যায়নি।")
        js_token = ""
        try:
            async with session.get(f"{BASE}/sharing/link", params={"surl": short[1:]}) as resp:
                html = await resp.text()
            match = re.search(r"fn%28%22([0-9A-Fa-f]+)%22%29", html)
            js_token = match.group(1) if match else ""
        except Exception:
            pass

        total = sum(int(f.get("size", 0) or 0) for f in files)
        finished = 0
        out: list[Path] = []
        for entry in files:
            url = await _dlink(session, info, entry, js_token)
            if len(files) > 1:
                parts = [utils.safe_name(p) for p in (entry.get("path") or "").split("/") if p]
                rel = Path(*parts) if parts else Path(utils.safe_name(entry["server_filename"]))
            else:
                rel = Path(utils.safe_name(entry["server_filename"]))
            dest = local_dir / rel
            base_done = finished

            async def inner_cb(done: int, _size: int, _base: int = base_done) -> None:
                if cb:
                    await cb(_base + done, total)

            finished += await _download_file(session, url, dest, inner_cb)
            out.append(dest)
        return out


# ------------------------------------------------------------------- upload

def _md5_blocks(path: Path) -> list[str]:
    blocks: list[str] = []
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            blocks.append(hashlib.md5(chunk).hexdigest())
    return blocks


async def _put_chunk(session: aiohttp.ClientSession, params: dict, remote_path: str,
                     uploadid: str, seq: int, data: bytes) -> None:
    query = {**params, "method": "upload", "type": "tmpfile", "path": remote_path,
             "uploadid": uploadid, "partseq": str(seq)}
    for attempt in range(3):
        try:
            form = aiohttp.FormData()
            form.add_field("file", data, filename="blob", content_type="application/octet-stream")
            async with session.post(UPLOAD_HOST, params=query, data=form) as resp:
                result = await resp.json(content_type=None)
            if result.get("md5"):
                return
            raise TeraBoxError(f"চাঙ্ক {seq} আপলোড ব্যর্থ: {str(result)[:120]}")
        except (aiohttp.ClientError, asyncio.TimeoutError, TeraBoxError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))


async def upload(path: Path, rel: Optional[str] = None, cb: Optional[ProgressCb] = None) -> str:
    size = path.stat().st_size
    if size == 0:
        raise utils.UserError("খালি (0 byte) ফাইল TeraBox-এ আপলোড করা যায় না।")
    remote_path = f"{config.TERABOX_FOLDER}/{rel or path.name}"
    target_dir = remote_path.rsplit("/", 1)[0] + "/"
    mtime = str(int(path.stat().st_mtime))

    async with _session() as session:
        tokens = await _tokens(session)
        params = {**_base_params(), **{k: v for k, v in tokens.items() if v}}
        blocks = await asyncio.to_thread(_md5_blocks, path)
        block_json = json.dumps(blocks)

        pre = await _post_json(
            session, f"{BASE}/api/precreate", params=params,
            data={
                "path": remote_path, "autoinit": "1", "target_path": target_dir,
                "block_list": block_json, "size": str(size), "local_mtime": mtime,
                "file_limit_switch_v34": "true",
            },
        )
        _check(pre, "precreate")
        uploadid = pre.get("uploadid")
        if not uploadid:
            raise TeraBoxError("uploadid পাওয়া যায়নি।")

        sent = 0
        with open(path, "rb") as fh:
            for seq in range(len(blocks)):
                chunk = fh.read(CHUNK)
                await _put_chunk(session, _base_params(), remote_path, uploadid, seq, chunk)
                sent += len(chunk)
                if cb:
                    await cb(sent, size)

        created = await _post_json(
            session, f"{BASE}/api/create", params=params,
            data={
                "path": remote_path, "size": str(size), "uploadid": uploadid,
                "target_path": target_dir, "block_list": block_json,
                "local_mtime": mtime, "isdir": "0", "rtype": "1",
            },
        )
        _check(created, "create")
        return "terabox:" + str(created.get("path") or remote_path)
