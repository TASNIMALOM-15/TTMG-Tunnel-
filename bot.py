import asyncio
import logging
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from aiohttp import ClientSession, ClientTimeout, web
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import gdrive
import mega
import rclone_utils as rc
import terabox
import utils
from utils import UserError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hub")

URL_RE = re.compile(r"https?://[^\s<>\"']+")
DESTS = {
    "gdrive": "☁️ Send to Drive",
    "mega": "Ⓜ️ Send to MEGA",
    "terabox": "📦 Send to TeraBox",
    "tg": "📥 Download to Telegram",
}
NAMES = {"gdrive": "Google Drive", "mega": "MEGA", "terabox": "TeraBox", "tg": "Telegram", "url": "Link"}
COMMANDS = ["start", "help", "bulk", "done", "queue", "cancel", "clear"]


@dataclass
class Item:
    kind: str  # tg | gdrive | mega | terabox | url
    ref: str
    name: str
    size: int = 0


# ----------------------------------------------------------------- job store

class JobStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, chat_id INTEGER, status_msg_id INTEGER,
                kind TEXT, ref TEXT, name TEXT, size INTEGER, dest TEXT,
                status TEXT, error TEXT, created REAL)"""
        )
        self.db.execute(
            "DELETE FROM jobs WHERE status IN ('done','failed','cancelled') AND created < ?",
            (time.time() - 7 * 86400,),
        )
        self.db.commit()

    def add(self, **kw) -> int:
        cur = self.db.execute(
            """INSERT INTO jobs(user_id, chat_id, status_msg_id, kind, ref, name, size, dest, status, created)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (kw["user_id"], kw["chat_id"], kw["status_msg_id"], kw["kind"], kw["ref"],
             kw["name"], kw["size"], kw["dest"], "queued", time.time()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def get(self, job_id: int) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def set_status(self, job_id: int, status: str, error: Optional[str] = None) -> None:
        self.db.execute("UPDATE jobs SET status=?, error=? WHERE id=?", (status, error, job_id))
        self.db.commit()

    def unfinished(self) -> list[int]:
        rows = self.db.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY id"
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def cancel_queued(self, user_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status='queued' AND user_id=?", (user_id,)
        ).fetchall()
        self.db.execute(
            "UPDATE jobs SET status='cancelled' WHERE status='queued' AND user_id=?", (user_id,)
        )
        self.db.commit()
        return [dict(r) for r in rows]


STORE = JobStore(config.DB_PATH)
QUEUE: asyncio.Queue = asyncio.Queue()
RUNNING: dict[int, asyncio.Task] = {}
PENDING: dict[str, list[Item]] = {}
BULK: dict[int, list[Item]] = {}
SHUTTING_DOWN = False


# -------------------------------------------------------------------- status

class Status:
    """Throttled editor for a job's status message."""

    def __init__(self, client: Client, chat_id: int, msg_id: int, title: str):
        self.client, self.chat_id, self.msg_id, self.title = client, chat_id, msg_id, title
        self.last = 0.0

    async def set(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < 4:
            return
        self.last = now
        try:
            await self.client.edit_message_text(self.chat_id, self.msg_id, f"{self.title}\n{text}")
        except MessageNotModified:
            pass
        except FloodWait as exc:
            await asyncio.sleep(min(exc.value, 10))
        except Exception as exc:
            log.debug("status edit failed: %s", exc)

    def progress_cb(self, label: str):
        started = time.time()

        async def cb(done: int, total: int) -> None:
            await self.set(utils.progress_text(label, done, total, started))

        return cb


# ------------------------------------------------------------- item parsing

def classify(url: str) -> Optional[str]:
    if gdrive.is_gdrive_link(url):
        return "gdrive"
    if mega.is_mega_link(url):
        return "mega"
    if terabox.is_terabox_link(url):
        return "terabox"
    if re.match(r"https?://(?:t|telegram)\.me/", url, re.I):
        return None
    return "url"


def link_name(kind: str, url: str) -> str:
    if kind == "url":
        parsed = urlparse(url)
        tail = unquote(parsed.path.rsplit("/", 1)[-1])
        return utils.safe_name(tail or parsed.netloc, "link")
    return f"{NAMES[kind]} link"


def extract_items(message: Message) -> list[Item]:
    items: list[Item] = []
    found = utils.media_of(message)
    if found:
        attr, obj = found
        items.append(
            Item("tg", f"{message.chat.id}:{message.id}",
                 utils.media_name(message, attr, obj), getattr(obj, "file_size", 0) or 0)
        )
    text = message.text or message.caption or ""
    seen = set()
    for raw in URL_RE.findall(text):
        url = raw.rstrip(").,]>")
        if url in seen:
            continue
        seen.add(url)
        kind = classify(url)
        if kind:
            items.append(Item(kind, url, link_name(kind, url), 0))
    return items


def valid(kind: str, dest: str) -> bool:
    return kind != dest


def keyboard(token: str, items: list[Item]) -> InlineKeyboardMarkup:
    kinds = {i.kind for i in items}
    buttons = [
        InlineKeyboardButton(label, callback_data=f"go|{token}|{dest}")
        for dest, label in DESTS.items()
        if any(valid(k, dest) for k in kinds)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"x|{token}")])
    return InlineKeyboardMarkup(rows)


def summary(items: list[Item]) -> str:
    lines = []
    for it in items[:6]:
        size = f" ({utils.human(it.size)})" if it.size else ""
        lines.append(f"• {it.name[:60]}{size} — {NAMES[it.kind]}")
    if len(items) > 6:
        lines.append(f"… আরও {len(items) - 6}টি")
    return "\n".join(lines)


async def offer(message: Message, items: list[Item]) -> None:
    token = uuid.uuid4().hex[:8]
    PENDING[token] = items
    while len(PENDING) > 100:
        PENDING.pop(next(iter(PENDING)))
    await message.reply_text(
        f"📥 {len(items)}টি আইটেম পাওয়া গেছে:\n{summary(items)}\n\nকোথায় পাঠাবেন?",
        reply_markup=keyboard(token, items),
        quote=True,
    )


# ------------------------------------------------------------------ enqueue

async def enqueue(client: Client, chat_id: int, user_id: int, items: list[Item], dest: str) -> tuple[int, int]:
    added = skipped = 0
    for it in items:
        if not valid(it.kind, dest):
            skipped += 1
            continue
        msg = await client.send_message(chat_id, f"⏳ কিউতে আছে: {it.name[:80]}")
        job_id = STORE.add(
            user_id=user_id, chat_id=chat_id, status_msg_id=msg.id,
            kind=it.kind, ref=it.ref, name=it.name, size=it.size, dest=dest,
        )
        await QUEUE.put(job_id)
        added += 1
    return added, skipped


# ----------------------------------------------------------------- transfer

async def get_tg_media(app: Client, ref: str):
    chat_id, msg_id = (int(x) for x in ref.split(":"))
    msg = await app.get_messages(chat_id, msg_id)
    found = utils.media_of(msg) if msg and not msg.empty else None
    if not found:
        raise UserError("টেলিগ্রাম মেসেজটি আর পাওয়া যাচ্ছে না।")
    return msg, found[1]


async def send_to_telegram(app: Client, chat_id: int, path: Path, st: Status, tag: str) -> None:
    cb = st.progress_cb(f"{tag}⬆️ টেলিগ্রামে আপলোড হচ্ছে")
    if path.suffix.lower() in utils.VIDEO_EXT:
        duration, width, height = await utils.probe_video(path)
        thumb = await utils.make_thumb(path, duration)
        try:
            await app.send_video(
                chat_id, str(path), caption=path.name, duration=duration, width=width,
                height=height, thumb=str(thumb) if thumb else None,
                supports_streaming=True, progress=cb,
            )
        finally:
            if thumb:
                thumb.unlink(missing_ok=True)
    else:
        await app.send_document(chat_id, str(path), caption=path.name, force_document=True, progress=cb)


async def fetch_local(app: Client, job: dict, st: Status, workdir: Path) -> list[Path]:
    kind, ref = job["kind"], job["ref"]
    if kind == "tg":
        msg, media = await get_tg_media(app, ref)
        utils.ensure_space(workdir, getattr(media, "file_size", 0) or 0)
        target = workdir / utils.safe_name(job["name"])
        path = await app.download_media(
            msg, file_name=str(target), progress=st.progress_cb("⬇️ টেলিগ্রাম থেকে নামছে")
        )
        if not path:
            raise RuntimeError("টেলিগ্রাম থেকে ডাউনলোড ব্যর্থ।")
        return [Path(path)]
    if kind == "gdrive":
        await st.set("⬇️ Drive থেকে নামছে…", force=True)
        return await gdrive.download_to_dir(gdrive.extract_id(ref) or "", workdir)
    if kind == "mega":
        await st.set("⬇️ MEGA থেকে নামছে…", force=True)
        return await mega.download_link(ref, workdir)
    if kind == "terabox":
        return await terabox.download(ref, workdir, st.progress_cb("⬇️ TeraBox থেকে নামছে"))
    if kind == "url":
        return [await utils.download_url(ref, workdir, st.progress_cb("⬇️ লিংক থেকে নামছে"))]
    raise UserError("অজানা সোর্স।")


async def upload_local(app: Client, job: dict, path: Path, rel: str, st: Status, tag: str) -> str:
    dest = job["dest"]
    if dest == "gdrive":
        await st.set(f"{tag}⬆️ Drive-এ আপলোড হচ্ছে…", force=True)
        return await gdrive.upload_local(path, rel)
    if dest == "mega":
        await st.set(f"{tag}⬆️ MEGA-তে আপলোড হচ্ছে…", force=True)
        return await mega.upload_local(path, rel)
    if dest == "terabox":
        return await terabox.upload(path, rel, st.progress_cb(f"{tag}⬆️ TeraBox-এ আপলোড হচ্ছে"))
    if dest == "tg":
        if path.stat().st_size > config.TG_MAX_BYTES:
            raise UserError("ফাইল ২GB-এর বেশি, টেলিগ্রামে পাঠানো সম্ভব নয়।")
        await send_to_telegram(app, job["chat_id"], path, st, tag)
        return "Telegram"
    raise UserError("অজানা গন্তব্য।")


async def transfer(app: Client, job: dict, st: Status, workdir: Path) -> str:
    kind, dest, ref = job["kind"], job["dest"], job["ref"]
    name = utils.safe_name(job["name"])

    # Fast path 1: Telegram -> Drive/MEGA, streamed through rclone (no full file in RAM).
    if kind == "tg" and dest in ("gdrive", "mega"):
        msg, media = await get_tg_media(app, ref)
        total = getattr(media, "file_size", 0) or 0
        cb = st.progress_cb(f"⬆️ {NAMES[dest]}-এ স্ট্রিম হচ্ছে")
        done = 0

        async def chunks():
            nonlocal done
            async for part in app.stream_media(msg):
                done += len(part)
                await cb(done, total)
                yield part

        module = gdrive if dest == "gdrive" else mega
        return await module.upload_stream(name, chunks())

    # Fast path 2: Drive -> MEGA, rclone copies directly (fallback: staging below).
    if kind == "gdrive" and dest == "mega":
        try:
            await st.set("🔄 Drive → MEGA সরাসরি কপি হচ্ছে…", force=True)
            await gdrive.copy_id_to(gdrive.extract_id(ref) or "", mega.target())
            return mega.target()
        except rc.RcloneError as exc:
            log.warning("direct copyid failed, falling back to staging: %s", exc)

    # Fast path 3: plain URL -> Drive/MEGA via rclone copyurl.
    if kind == "url" and dest in ("gdrive", "mega"):
        folder = gdrive.target() if dest == "gdrive" else mega.target()
        try:
            await st.set(f"🔄 লিংক থেকে {NAMES[dest]}-এ কপি হচ্ছে…", force=True)
            await rc.copyurl(ref, folder)
            return folder
        except rc.RcloneError as exc:
            log.warning("copyurl failed, falling back to staging: %s", exc)

    # Generic path: stage on disk, upload, cleanup.
    workdir.mkdir(parents=True, exist_ok=True)
    files = await fetch_local(app, job, st, workdir)
    results = []
    for index, path in enumerate(files, 1):
        rel = path.relative_to(workdir).as_posix()
        tag = f"[{index}/{len(files)}] " if len(files) > 1 else ""
        results.append(await upload_local(app, job, path, rel, st, tag))
        if dest != "tg":
            path.unlink(missing_ok=True)
    return results[0] if len(results) == 1 else f"{len(results)}টি ফাইল → {NAMES[dest]}"


async def process(app: Client, job: dict) -> None:
    job_id = job["id"]
    st = Status(app, job["chat_id"], job["status_msg_id"], f"#{job_id} • {job['name'][:60]}")
    workdir = Path(config.TMP_DIR) / f"job{job_id}"
    STORE.set_status(job_id, "running")
    try:
        await st.set("🚀 শুরু হচ্ছে…", force=True)
        result = ""
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                result = await transfer(app, job, st, workdir)
                break
            except (asyncio.CancelledError, UserError):
                raise
            except Exception as exc:
                log.warning("job %s attempt %s failed: %r", job_id, attempt + 1, exc)
                shutil.rmtree(workdir, ignore_errors=True)
                if attempt >= config.MAX_RETRIES:
                    raise
                await st.set(
                    f"⚠️ সমস্যা: {str(exc)[:150]}\n🔁 আবার চেষ্টা ({attempt + 1}/{config.MAX_RETRIES})",
                    force=True,
                )
                await asyncio.sleep(5 * (attempt + 1))
        STORE.set_status(job_id, "done")
        await st.set(f"✅ সম্পন্ন → {NAMES[job['dest']]}\n📁 {result[:300]}", force=True)
    except asyncio.CancelledError:
        if SHUTTING_DOWN:
            STORE.set_status(job_id, "queued")
        else:
            STORE.set_status(job_id, "cancelled")
            await st.set("🛑 বাতিল করা হয়েছে", force=True)
        raise
    except Exception as exc:
        log.exception("job %s failed", job_id)
        STORE.set_status(job_id, "failed", str(exc))
        await st.set(f"❌ ব্যর্থ: {str(exc)[:300]}", force=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def worker(app: Client, wid: int) -> None:
    log.info("worker %s started", wid)
    while True:
        job_id = await QUEUE.get()
        try:
            job = STORE.get(job_id)
            if not job or job["status"] != "queued":
                continue
            task = asyncio.create_task(process(app, job))
            RUNNING[job_id] = task
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                RUNNING.pop(job_id, None)
        finally:
            QUEUE.task_done()


# ----------------------------------------------------------------- handlers

OWNER = filters.create(
    lambda _, __, update: bool(
        getattr(update, "from_user", None) and update.from_user.id in config.OWNER_IDS
    )
)

HELP_TEXT = (
    "👋 আমি Cloud Hub Bot।\n\n"
    "ফাইল (ভিডিও/ডকুমেন্ট) ফরোয়ার্ড করুন অথবা Google Drive / MEGA / TeraBox / সরাসরি লিংক পাঠান। "
    "তারপর বাটন চেপে গন্তব্য বেছে নিন।\n\n"
    "কমান্ড:\n"
    "/bulk — একসাথে অনেক আইটেম জমা করার মোড\n"
    "/done — জমা শেষ, গন্তব্য বাছাই\n"
    "/queue — কিউ দেখুন\n"
    "/cancel — চলমান কাজ বাতিল\n"
    "/clear — কিউয়ের অপেক্ষমাণ কাজ মুছুন"
)


async def cmd_start(client: Client, message: Message) -> None:
    await message.reply_text(HELP_TEXT)


async def cmd_bulk(client: Client, message: Message) -> None:
    BULK[message.from_user.id] = []
    await message.reply_text("📦 বাল্ক মোড চালু। ফাইল/লিংক পাঠাতে থাকুন, শেষ হলে /done লিখুন।")


async def cmd_done(client: Client, message: Message) -> None:
    items = BULK.pop(message.from_user.id, None)
    if not items:
        await message.reply_text("কিছু জমা নেই। আগে /bulk দিয়ে আইটেম পাঠান।")
        return
    await offer(message, items)


async def cmd_queue(client: Client, message: Message) -> None:
    running = [STORE.get(j) for j in list(RUNNING)]
    lines = [f"▶️ চলছে: {j['name'][:50]}" for j in running if j]
    lines.append(f"⏳ অপেক্ষমাণ: {QUEUE.qsize()}টি")
    await message.reply_text("\n".join(lines))


async def cmd_cancel(client: Client, message: Message) -> None:
    uid = message.from_user.id
    BULK.pop(uid, None)
    count = 0
    for job_id, task in list(RUNNING.items()):
        job = STORE.get(job_id)
        if job and job["user_id"] == uid:
            task.cancel()
            count += 1
    await message.reply_text(f"🛑 {count}টি চলমান কাজ বাতিল করা হয়েছে।")


async def cmd_clear(client: Client, message: Message) -> None:
    rows = STORE.cancel_queued(message.from_user.id)
    for row in rows:
        try:
            await client.edit_message_text(row["chat_id"], row["status_msg_id"], "🗑 কিউ থেকে মুছে ফেলা হয়েছে")
        except Exception:
            pass
    await message.reply_text(f"🗑 {len(rows)}টি অপেক্ষমাণ কাজ মুছে ফেলা হয়েছে।")


async def on_incoming(client: Client, message: Message) -> None:
    items = extract_items(message)
    if not items:
        if message.text:
            await message.reply_text("❓ সাপোর্টেড লিংক বা ফাইল পাইনি। /help দেখুন।")
        return
    uid = message.from_user.id
    if uid in BULK:
        BULK[uid].extend(items)
        await message.reply_text(f"➕ জমা হলো (মোট {len(BULK[uid])}টি)। শেষ হলে /done", quote=True)
        return
    await offer(message, items)


async def on_callback(client: Client, cq: CallbackQuery) -> None:
    parts = (cq.data or "").split("|")
    if not cq.message:
        await cq.answer()
        return
    if parts[0] == "x" and len(parts) >= 2:
        PENDING.pop(parts[1], None)
        await cq.message.edit_text("❌ বাতিল করা হয়েছে।")
        await cq.answer()
        return
    if parts[0] == "go" and len(parts) == 3 and parts[2] in DESTS:
        items = PENDING.pop(parts[1], None)
        if items is None:
            await cq.answer("অনুরোধের মেয়াদ শেষ। আবার পাঠান।", show_alert=True)
            return
        await cq.answer("কিউতে যোগ হচ্ছে…")
        added, skipped = await enqueue(client, cq.message.chat.id, cq.from_user.id, items, parts[2])
        text = f"✅ {added}টি কাজ কিউতে যোগ হয়েছে → {NAMES[parts[2]]}"
        if skipped:
            text += f"\n⚠️ {skipped}টি বাদ (সোর্স ও গন্তব্য একই)"
        await cq.message.edit_text(text)
        return
    await cq.answer()


# --------------------------------------------------------------- web server

async def start_web() -> web.AppRunner:
    async def index(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "queued": QUEUE.qsize(), "running": len(RUNNING)})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("web server listening on :%s", config.PORT)
    return runner


async def keep_alive() -> None:
    if not config.APP_URL:
        log.info("APP_URL/RENDER_EXTERNAL_URL not set; self-ping disabled")
        return
    async with ClientSession(timeout=ClientTimeout(total=20)) as session:
        while True:
            await asyncio.sleep(config.PING_INTERVAL)
            try:
                async with session.get(f"{config.APP_URL}/health") as resp:
                    log.debug("keep-alive ping -> %s", resp.status)
            except Exception as exc:
                log.warning("keep-alive ping failed: %s", exc)


# --------------------------------------------------------------------- main

async def main() -> None:
    global SHUTTING_DOWN
    Path(config.TMP_DIR).mkdir(parents=True, exist_ok=True)
    for old in Path(config.TMP_DIR).glob("job*"):
        shutil.rmtree(old, ignore_errors=True)

    runner = await start_web()

    app = Client(
        "cloud-hub",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        in_memory=True,
        workers=4,
        sleep_threshold=30,
    )
    private_owner = filters.private & OWNER
    app.add_handler(MessageHandler(cmd_start, filters.command(["start", "help"]) & private_owner))
    app.add_handler(MessageHandler(cmd_bulk, filters.command("bulk") & private_owner))
    app.add_handler(MessageHandler(cmd_done, filters.command("done") & private_owner))
    app.add_handler(MessageHandler(cmd_queue, filters.command("queue") & private_owner))
    app.add_handler(MessageHandler(cmd_cancel, filters.command("cancel") & private_owner))
    app.add_handler(MessageHandler(cmd_clear, filters.command("clear") & private_owner))
    content = (
        filters.text | filters.video | filters.document | filters.audio
        | filters.animation | filters.voice | filters.video_note
    )
    app.add_handler(MessageHandler(on_incoming, private_owner & content & ~filters.command(COMMANDS)))
    app.add_handler(CallbackQueryHandler(on_callback, OWNER))

    await app.start()
    me = await app.get_me()
    log.info("bot started as @%s", me.username)

    for job_id in STORE.unfinished():
        STORE.set_status(job_id, "queued")
        QUEUE.put_nowait(job_id)
    if QUEUE.qsize():
        log.info("restored %s unfinished jobs", QUEUE.qsize())

    tasks = [asyncio.create_task(worker(app, i + 1)) for i in range(config.WORKERS)]
    tasks.append(asyncio.create_task(keep_alive()))

    await idle()

    SHUTTING_DOWN = True
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
