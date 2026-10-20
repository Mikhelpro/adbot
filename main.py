import asyncio, os, secrets
from contextlib import asynccontextmanager
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
import db
from bot import bot, dp, expire_loop

SECRET = os.environ.get("WEBHOOK_SECRET") or secrets.token_hex(16)

@asynccontextmanager
async def lifespan(app):
    await db.init()
    base = os.environ.get("PUBLIC_URL") or os.environ["RENDER_EXTERNAL_URL"]
    await bot.set_webhook(f"{base}/webhook", secret_token=SECRET)
    task = asyncio.create_task(expire_loop())
    yield
    task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
async def health(): return {"ok": True}

@app.post("/webhook")
async def webhook(req: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != SECRET: raise HTTPException(403)
    await dp.feed_webhook_update(bot, Update.model_validate(await req.json(), context={"bot": bot}))
    return {"ok": True}
