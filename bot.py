#!/usr/bin/env python3
"""
Telegram File Stream Bot – Personal Use
────────────────────────────────────────
• No database / no per-request storage
• All file info lives in the HMAC-signed URL token
• HTTP Range request support → seek, resume, stream in browser/VLC
• Works with any file up to 2 GB via Telegram MTProto
"""
import asyncio, base64, hashlib, hmac, json, logging, os, re
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgstream")

# ─── Configuration ─────────────────────────────────────────────────────────────
API_ID     = int(os.environ["API_ID"])       # https://my.telegram.org
API_HASH   =     os.environ["API_HASH"]      # https://my.telegram.org
BOT_TOKEN  =     os.environ["BOT_TOKEN"]     # from @BotFather
SECRET_KEY =     os.environ["SECRET_KEY"]    # openssl rand -hex 32
PUBLIC_URL =     os.environ["PUBLIC_URL"].rstrip("/")  # your public HTTPS URL

PORT = int(os.environ.get("PORT", "8080"))

# Your Telegram numeric user ID (from @userinfobot).
# Leave empty ("") to allow anyone — NOT recommended for a public bot.
ALLOWED_USERS: set[int] = {
    int(x)
    for x in os.environ.get("ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
}

CHUNK = 1 << 20  # 1 MiB per Telegram API request

# ─── Telegram client ───────────────────────────────────────────────────────────
client = TelegramClient("session", API_ID, API_HASH)

# ─── Signed token helpers (no DB needed) ──────────────────────────────────────
_KEY = SECRET_KEY.encode()


def _sign(data: str) -> str:
    return hmac.new(_KEY, data.encode(), hashlib.sha256).hexdigest()[:24]


def encode_token(
    chat_id: int, msg_id: int, size: int, name: str, mime: str
) -> str:
    payload = json.dumps(
        {"c": chat_id, "m": msg_id, "s": size, "n": name, "mt": mime},
        separators=(",", ":"),
    )
    d = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{_sign(d)}.{d}"


def decode_token(token: str) -> dict | None:
    try:
        sig, d = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(d)):
            return None
        raw = base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)).decode()
        r = json.loads(raw)
        return {
            "chat_id": r["c"],
            "msg_id":  r["m"],
            "size":    r["s"],
            "name":    r["n"],
            "mime":    r["mt"],
        }
    except Exception:
        return None

# ─── Utilities ─────────────────────────────────────────────────────────────────
def _guess_name(msg) -> str:
    if msg.file:
        if msg.file.name:
            return msg.file.name
        mt  = msg.file.mime_type or "application/octet-stream"
        ext = mt.split("/")[-1].split(";")[0].strip()
        return f"file.{ext}"
    return "file.bin"


def _fmt_size(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"

# ─── Bot handler ───────────────────────────────────────────────────────────────
@client.on(events.NewMessage(incoming=True))
async def on_file(event):
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return

    msg = event.message

    if not msg.file:
        await event.reply(
            "📁 Send or forward me **any file** and I'll generate a direct "
            "download / stream link.\n\n"
            "_Supports video, audio, archives, documents — up to 2 GB._",
            parse_mode="markdown",
        )
        return

    size  = msg.file.size or 0
    name  = _guess_name(msg)
    mime  = msg.file.mime_type or "application/octet-stream"
    token = encode_token(msg.chat_id, msg.id, size, name, mime)
    url   = f"{PUBLIC_URL}/file/{token}"

    await event.reply(
        f"✅ **Ready!**\n\n"
        f"📄 `{name}`\n"
        f"📦 {_fmt_size(size)}\n\n"
        f"🔗 **Direct link:**\n`{url}`\n\n"
        f"_Works in browser, VLC, wget, curl, or any download manager._",
        parse_mode="markdown",
    )

# ─── HTTP streaming handler ────────────────────────────────────────────────────
async def handle_stream(req: web.Request) -> web.StreamResponse:
    info = decode_token(req.match_info["token"])
    if not info:
        return web.Response(status=403, text="Invalid token")

    total = info["size"]   # 0 means unknown

    # ── Parse Range header ─────────────────────────────────────────────────
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", req.headers.get("Range", ""))
    if m:
        start = int(m.group(1)) if m.group(1) else 0
        end   = int(m.group(2)) if m.group(2) else max(0, total - 1)
        code  = 206
    else:
        start, end, code = 0, max(0, total - 1), 200

    if total > 0:
        end = min(end, total - 1)
    length = (end - start + 1) if total > 0 else None

    # ── Build response headers ─────────────────────────────────────────────
    headers: dict[str, str] = {
        "Content-Type":        info["mime"],
        "Content-Disposition": f'inline; filename="{info["name"]}"',
        "Accept-Ranges":       "bytes",
    }
    if total > 0:
        headers["Content-Range"]  = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(length)

    resp = web.StreamResponse(status=code, headers=headers)
    await resp.prepare(req)

    # ── Stream chunks from Telegram ────────────────────────────────────────
    try:
        entity  = await client.get_entity(info["chat_id"])
        message = await client.get_messages(entity, ids=info["msg_id"])
        if not message or not message.media:
            return web.Response(status=404, text="File not found")

        # Align start offset to chunk boundary for Telegram API
        aligned = (start // CHUNK) * CHUNK
        skip    = start - aligned   # bytes to discard from first chunk
        written = 0

        async for chunk in client.iter_download(
            message.media,
            offset=aligned,
            request_size=CHUNK,
        ):
            chunk = bytes(chunk)   # ensure plain bytes

            if not chunk:
                break

            if skip:
                chunk = chunk[skip:]
                skip  = 0

            if length is not None:
                remaining = length - written
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]

            await resp.write(chunk)
            written += len(chunk)

            if length is not None and written >= length:
                break

    except FloodWaitError as exc:
        log.warning("FloodWait %ds – closing stream early", exc.seconds)
    except (ConnectionError, ConnectionResetError):
        pass   # client disconnected mid-download, that's fine
    except Exception as exc:
        log.error("Stream error: %s", exc, exc_info=True)

    try:
        await resp.write_eof()
    except Exception:
        pass

    return resp

# ─── Health check ──────────────────────────────────────────────────────────────
async def handle_health(_req: web.Request) -> web.Response:
    return web.Response(text="OK")

# ─── Application bootstrap ─────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("Telegram bot authenticated")

    app = web.Application()
    app.router.add_get("/file/{token}", handle_stream)
    app.router.add_get("/health",       handle_health)
    app.router.add_get("/",             handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    log.info("HTTP server on :%d", PORT)
    log.info("Public base URL: %s", PUBLIC_URL)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
