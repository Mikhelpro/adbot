import asyncio, os, secrets
from collections import deque
from contextlib import asynccontextmanager
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
import db
from aiogram.types import BotCommand
from bot import bot, dp, expire_loop, get_setting, BRAND, TAGLINE

SECRET = os.environ.get("WEBHOOK_SECRET") or secrets.token_hex(16)
seen_ids, seen_order = set(), deque()   # remember recent update IDs so repeats are ignored
tasks = set()

@asynccontextmanager
async def lifespan(app):
    await db.init()
    base = os.environ.get("PUBLIC_URL") or os.environ["RENDER_EXTERNAL_URL"]
    await bot.set_webhook(f"{base}/webhook", secret_token=SECRET)
    try:  # branded bot profile
        brand, tag = await get_setting("brand", BRAND), await get_setting("tagline", TAGLINE)
        await bot.set_my_commands([BotCommand(command="start", description="Open the menu")])
        await bot.set_my_short_description(f"{brand}: {tag}"[:120])
        await bot.set_my_description(f"{brand}\n{tag}\n\nTikTok, Meta & Telegram ads plus website hosting. See prices, order and pay in minutes. Tap START."[:512])
    except Exception as e: print("profile update skipped:", e)
    task = asyncio.create_task(expire_loop())
    yield
    task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
async def health(): return {"ok": True}

async def handle(update: Update):
    try: await dp.feed_update(bot, update)
    except Exception as e: print("update error:", repr(e))

@app.post("/webhook")
async def webhook(req: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != SECRET: raise HTTPException(403)
    data = await req.json()
    uid = data.get("update_id")
    if uid in seen_ids: return {"ok": True}          # Telegram retry: already handled
    seen_ids.add(uid); seen_order.append(uid)
    if len(seen_order) > 1000: seen_ids.discard(seen_order.popleft())
    t = asyncio.create_task(handle(Update.model_validate(data, context={"bot": bot})))
    tasks.add(t); t.add_done_callback(tasks.discard)
    return {"ok": True}                               # reply to Telegram immediately
