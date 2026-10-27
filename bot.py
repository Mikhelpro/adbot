import asyncio, csv, io, os
from datetime import timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from db import Activity, AuditLog, Order, Package, Payment, Review, Service, Session, SupportMessage, User, WalletTx, now

bot = Bot(os.environ["BOT_TOKEN"])
dp = Dispatcher(storage=MemoryStorage())
r = Router(); dp.include_router(r)
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PAY = os.environ.get("PAY_INSTRUCTIONS", "Telebirr: 09XXXXXXXX").replace("\\n", "\n")
WELCOME = os.environ.get("WELCOME_TEXT", "Welcome! 👋 We run your TikTok, Meta and Telegram ads and host your website.\nWhat would you like to do?").replace("\\n", "\n")
STATUSES = {"in_progress", "completed", "cancelled"}

CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

class Flow(StatesGroup):
    details = State()
    ref = State()
    support = State()

def kb(rows, width=1):
    b = InlineKeyboardBuilder()
    for text, data in rows:
        if data.startswith("http"): b.button(text=text, url=data)
        else: b.button(text=text, callback_data=data)
    b.adjust(width); return b.as_markup()

import html
esc = html.escape
HTML = "HTML"
DIV = "━━━━━━━━━━━━━━"
BRAND = os.environ.get("BRAND_NAME", "Our Ad Studio")
TAGLINE = os.environ.get("TAGLINE", "Ads that bring you customers")
WELCOME_BODY = os.environ.get("WELCOME_TEXT", "We run your TikTok, Meta and Telegram ads and host your website, so you can focus on your business.").replace("\\n", "\n")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@your_username")
MIN_DEPOSIT = int(os.environ.get("MIN_DEPOSIT", "100"))
DEFAULT_FAQ = """Q: How do I order?
Open 🛍 Shop, pick a service and a plan, describe your goal, then pay.
Q: How can I pay?
Pay with the Telebirr or CBE details shown on your order, then send us the transaction reference.
Q: What happens after I pay?
Our team verifies the payment, starts work, and updates you in this chat.
Q: Can I top up first and pay later?
Yes. Add money to your 💼 Wallet and use it to pay for any order.
Q: I have a problem with an order.
Open 💬 Support and send us your order number."""
DEFAULTS = {"brand": BRAND, "tagline": TAGLINE, "welcome": WELCOME_BODY, "pay": PAY, "faq": DEFAULT_FAQ, "support": SUPPORT_USERNAME, "channel": ""}
LABEL = {"pending_payment": "🕒 Waiting for payment", "payment_submitted": "🔎 Verifying your payment", "paid": "✅ Paid",
         "in_progress": "🚀 In progress", "completed": "🏁 Completed", "cancelled": "✖️ Cancelled", "expired": "⌛ Expired"}
TXLABEL = {"submitted": "🔎 Verifying", "approved": "✅ Approved", "rejected": "❌ Rejected"}
ICONS = [("tiktok", "🎵"), ("meta", "📘"), ("facebook", "📘"), ("instagram", "📸"), ("telegram", "✈️"), ("host", "🌐"), ("web", "🌐")]
def icon(name): n = name.lower(); return next((i for k, i in ICONS if k in n), "🔹")
def money(n): return f"{n:,} ETB"
def mask(name): name = (name or "?").strip(); return (name[:1] or "?") + "***"
def when(d): return d.strftime("%d %b %Y")
def ago(d):
    s = int((now() - d).total_seconds())
    return "just now" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 86400 else f"{s // 86400}d ago"
def back(to): return ("⬅️ Back", to)

ROWS = [("🛍 Shop", "shop"), ("📦 My orders", "mine"), ("👤 My profile", "profile"), ("💼 Wallet", "wallet"), ("⏳ Pending payment", "pending"),
        ("⭐ Reviews", "reviews"), ("📜 Bot log", "log"), ("💬 Support", "support"), ("📢 Channel", "channel")]
MENU = kb(ROWS, 2)
MENU_ADMIN = kb(ROWS + [("🛠 Admin panel", "a:home")], 2)

class Wal(StatesGroup):
    amount = State()
    ref = State()

class Rev(StatesGroup):
    text = State()

# ---------- helpers ----------
async def screen(target, text, parse_mode=HTML, reply_markup=None):
    """One-screen feel: edit the message the button was on; fall back to a new message."""
    try: await target.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e): return
        await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)

async def screen_by_id(m, mid, text, parse_mode=HTML, reply_markup=None):
    """After the customer typed something: turn the bot's earlier prompt into the next screen and tidy away their text."""
    try:
        if not mid: raise ValueError
        await bot.edit_message_text(text, chat_id=m.chat.id, message_id=mid, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        await m.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    if m.text:
        try: await m.delete()
        except Exception: pass

async def balance(s, uid):
    return await s.scalar(select(func.coalesce(func.sum(WalletTx.amount), 0)).where(WalletTx.user_id == uid, WalletTx.status == "approved"))

async def log_activity(text, kind="info"):
    async with Session() as s: s.add(Activity(kind=kind, text=text[:300])); await s.commit()

async def names(s, o):
    p = await s.get(Package, o.package_id); return await s.get(Service, p.service_id), p

async def ref_taken(s, ref):
    return bool(await s.scalar(select(Payment.id).where(Payment.reference == ref)) or await s.scalar(select(WalletTx.id).where(WalletTx.reference == ref)))

async def welcome_text(name=""):
    brand = esc(await get_setting("brand", BRAND)); tag = esc(await get_setting("tagline", TAGLINE)); body = esc(await get_setting("welcome", WELCOME_BODY))
    hello = f"Hello, <b>{esc(name)}</b> 👋\n" if name else ""
    return f"✨ <b>{brand}</b>\n<i>{tag}</i>\n{DIV}\n\n{hello}{body}\n\n<b>Where would you like to start?</b>"

async def send_menu(target, uid, name=""):
    await target.answer(await welcome_text(name), parse_mode=HTML, reply_markup=MENU_ADMIN if uid in ADMINS else MENU)

async def notify_status(o):
    async with Session() as s: v, p = await names(s, o)
    k = None
    if o.status == "completed":
        k = kb([("⭐ Rate this order", f"rvo:{o.id}")]); await log_activity(f"🏁 Delivered: {v.name} · {p.name}", "delivered")
    await bot.send_message(o.user_id, f"📣 Update on <b>{o.code}</b>: {LABEL.get(o.status, o.status)}", parse_mode=HTML, reply_markup=k)

# ---------- start / menu ----------
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    async with Session() as s:
        if not await s.get(User, m.from_user.id):
            s.add(User(tg_id=m.from_user.id, name=m.from_user.full_name, username=m.from_user.username)); await s.commit()
    await send_menu(m, m.from_user.id, m.from_user.first_name)

@r.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear(); await screen(c.message, await welcome_text(c.from_user.first_name), reply_markup=MENU_ADMIN if c.from_user.id in ADMINS else MENU); await c.answer()

# ---------- 1) shop ----------
@r.callback_query(F.data == "shop")
async def shop(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s: svcs = (await s.scalars(select(Service).where(Service.active).order_by(Service.id))).all()
    await screen(c.message, f"🛍 <b>Shop</b>\n{DIV}\n\nChoose a service to see its plans.",
        reply_markup=kb([(f"{icon(x.name)}  {x.name}", f"pr:{x.id}") for x in svcs] + [back("menu")])); await c.answer()

@r.callback_query(F.data.startswith("pr:"))
async def shop_service(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        svc = await s.get(Service, int(c.data[3:]))
        pk = (await s.scalars(select(Package).where(Package.service_id == svc.id, Package.active).order_by(Package.id))).all()
    text = f"{icon(svc.name)} <b>{esc(svc.name)}</b>\n{DIV}\n\n" + "".join(f"<b>{esc(p.name)}</b>\n{money(p.price_etb)}\n\n" for p in pk) + "<i>Tap a plan to order it.</i>"
    await screen(c.message, text, reply_markup=kb([(f"{p.name} · {money(p.price_etb)}", f"pkg:{p.id}") for p in pk] + [back("shop")])); await c.answer()

@r.callback_query(F.data.startswith("pkg:"))
async def pick_package(c: CallbackQuery, state: FSMContext):
    async with Session() as s: p = await s.get(Package, int(c.data[4:]))
    await state.set_state(Flow.details); await state.update_data(pkg=p.id, mid=c.message.message_id)
    await screen(c.message, "<b>Step 3 of 3</b>  ●●●\nTell us about your goal\n\nWrite one short message with:\n"
        "• what you want to promote (link or page)\n• your goal (sales, followers, messages…)\n• your budget and target audience\n\n"
        "<i>Example: “Promote my clothing shop in Addis. Goal: more orders. Audience: women 18–35.”</i>", reply_markup=kb([back(f"pr:{p.service_id}")])); await c.answer()

@r.message(Flow.details, F.text)
async def got_details(m: Message, state: FSMContext):
    d = await state.get_data(); pkg_id = d["pkg"]; mid = d.get("mid"); await state.clear()
    async with Session() as s:
        p = await s.get(Package, pkg_id); svc = await s.get(Service, p.service_id)
        recent = await s.scalar(select(func.count()).select_from(Order).where(Order.user_id == m.from_user.id, Order.created_at > now() - timedelta(hours=1)))
        if recent >= 5: return await m.answer("Too many orders in the last hour. Please try again later.")
        o = Order(user_id=m.from_user.id, package_id=p.id, details=m.text, amount_etb=p.price_etb)
        s.add(o); await s.commit(); bal = await balance(s, m.from_user.id)
    await log_activity(f"🛒 {mask(m.from_user.first_name)} placed an order: {svc.name} · {p.name}", "order")
    pay = esc(await get_setting("pay", PAY))
    rows = [("✅ I've paid", f"paid:{o.id}")]
    if bal >= o.amount_etb: rows.append((f"💼 Pay with wallet ({money(bal)})", f"wpay:{o.id}"))
    await screen_by_id(m, mid, f"🧾 <b>Order created</b>\n{DIV}\n\n<b>{o.code}</b>\n{icon(svc.name)} {esc(svc.name)} · {esc(p.name)}\n\nTotal  <b>{money(o.amount_etb)}</b>\n\n{DIV}\n"
                   f"<b>Payment details</b>\n{pay}\n{DIV}\n\n<i>Add {o.code} to the transfer note, then tap “I've paid”.</i>",
                   reply_markup=kb(rows + [("🏠 Menu", "menu")]))

@r.callback_query(F.data.startswith("paid:"))
async def paid(c: CallbackQuery, state: FSMContext):
    oid = int(c.data[5:])
    async with Session() as s: o = await s.get(Order, oid)
    if not o or o.user_id != c.from_user.id or o.status != "pending_payment": return await c.answer("This order can't take a payment now.", show_alert=True)
    await state.set_state(Flow.ref); await state.update_data(order=oid, mid=c.message.message_id)
    await screen(c.message, "<b>Final step</b>  ●●●\nSend the <b>transaction reference</b> from your payment confirmation (SMS or app).\n"
        "You can also send a screenshot with the reference as the caption.", reply_markup=kb([back(f"ord:{oid}")])); await c.answer()

@r.message(Flow.ref, F.text | F.photo)
async def got_ref(m: Message, state: FSMContext):
    d = await state.get_data(); oid = d["order"]; mid = d.get("mid")
    ref = ((m.caption if m.photo else m.text) or "").strip().upper()
    if len(ref) < 4: return await m.answer("Please include the transaction reference (at least 4 characters).")
    shot = m.photo[-1].file_id if m.photo else None
    async with Session() as s:
        o = await s.get(Order, oid)
        if await ref_taken(s, ref): return await m.answer("This reference was already used. Please check it or tap 💬 Support.")
        pay = Payment(order_id=oid, reference=ref, screenshot_file_id=shot)
        s.add(pay)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback(); return await m.answer("This reference was already used. Please check it or tap 💬 Support.")
        o.status = "payment_submitted"; await s.commit()
    await state.clear()
    await screen_by_id(m, mid, f"⏳ <b>Payment received</b>\n{DIV}\n\n<b>{o.code}</b> is now being verified by our team.\nYou'll be notified here the moment it's confirmed.",
                       reply_markup=kb([back("mine")]))
    text = f"💳 Payment to verify\n{o.code} - {o.amount_etb} ETB\nRef: {ref}\nFrom: {m.from_user.full_name} (@{m.from_user.username})\n\n{o.details}"
    k = kb([("✅ Approve", f"adm:ok:{pay.id}"), ("❌ Reject", f"adm:no:{pay.id}")], 2)
    for a in ADMINS:
        if shot: await bot.send_photo(a, shot, caption=text[:1000], reply_markup=k)
        else: await bot.send_message(a, text, reply_markup=k)

@r.callback_query(F.data.startswith("wpay:"))
async def wallet_pay(c: CallbackQuery):
    oid = int(c.data[5:])
    async with Session() as s:
        await s.execute(select(User).where(User.tg_id == c.from_user.id).with_for_update())   # lock: no double spending
        o = await s.get(Order, oid)
        if not o or o.user_id != c.from_user.id or o.status != "pending_payment": return await c.answer("This order can't be paid now.", show_alert=True)
        bal = await balance(s, c.from_user.id)
        if bal < o.amount_etb: return await c.answer("Not enough wallet balance. Top up first.", show_alert=True)
        s.add(WalletTx(user_id=c.from_user.id, amount=-o.amount_etb, kind="spend", status="approved", note=o.code))
        o.status = "paid"; v, p = await names(s, o); await s.commit(); bal -= o.amount_etb
    await log_activity(f"💳 {mask(c.from_user.first_name)} purchased {v.name} · {p.name}", "purchase")
    for a in ADMINS: await bot.send_message(a, f"💼 {o.code} was paid from the customer's wallet ({o.amount_etb} ETB). Start work!")
    await screen(c.message, f"🎉 <b>Paid from wallet</b>\n{DIV}\n\n<b>{o.code}</b> is confirmed.\nWallet balance: <b>{money(bal)}</b>\n\nOur team is starting work.",
                 reply_markup=kb([back("mine")])); await c.answer()

@r.callback_query(F.data.startswith("adm:"), F.from_user.id.in_(ADMINS))
async def admin_decision(c: CallbackQuery):
    _, act, pid = c.data.split(":")
    async with Session() as s:
        pay = await s.get(Payment, int(pid)); o = await s.get(Order, pay.order_id)
        if pay.status != "submitted": return await c.answer("Already handled.", show_alert=True)
        ok = act == "ok"
        pay.status, pay.verified_by = ("approved" if ok else "rejected"), c.from_user.id
        o.status = "paid" if ok else "pending_payment"
        s.add(AuditLog(actor=c.from_user.id, action=f"{act} payment {pay.id} ({o.code})")); u = await s.get(User, o.user_id); v, p = await names(s, o); await s.commit()
    if ok:
        await log_activity(f"💳 {mask(u.name)} purchased {v.name} · {p.name}", "purchase")
        await bot.send_message(o.user_id, f"🎉 <b>Payment confirmed</b>\n{DIV}\n\n<b>{o.code}</b> is confirmed. Thank you!\nOur team is starting work, and you'll get updates right here.", parse_mode=HTML)
    else:
        await bot.send_message(o.user_id, f"⚠️ We couldn't verify the payment for <b>{o.code}</b>.\nPlease check the reference and amount, then tap the button to send it again.",
                               parse_mode=HTML, reply_markup=kb([("✅ I've paid", f"paid:{o.id}")]))
    await c.message.answer(f"{o.code}: {'approved' if ok else 'rejected'}"); await c.answer()

# ---------- 2) my orders ----------
@r.callback_query(F.data == "mine")
async def mine(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        os_ = (await s.scalars(select(Order).where(Order.user_id == c.from_user.id, Order.deleted_at.is_(None)).order_by(Order.id.desc()).limit(10))).all()
    text = f"📦 <b>My orders</b>\n{DIV}\n\n" + ("Tap an order to see its details." if os_ else "You have no orders yet.\nOpen 🛍 Shop to place your first one!")
    await screen(c.message, text, reply_markup=kb([(f"{o.code} · {money(o.amount_etb)} · {LABEL.get(o.status, o.status)}", f"ord:{o.id}") for o in os_] + [back("menu")])); await c.answer()

@r.callback_query(F.data.startswith("ord:"))
async def order_detail(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        o = await s.get(Order, int(c.data[4:]))
        if not o or o.user_id != c.from_user.id: return await c.answer("Order not found.", show_alert=True)
        v, p = await names(s, o); bal = await balance(s, c.from_user.id); rv = await s.scalar(select(Review.id).where(Review.order_id == o.id))
    rows = []
    if o.status == "pending_payment":
        rows.append(("✅ I've paid", f"paid:{o.id}"))
        if bal >= o.amount_etb: rows.append((f"💼 Pay with wallet ({money(bal)})", f"wpay:{o.id}"))
    if o.status == "completed" and not rv: rows.append(("⭐ Rate this order", f"rvo:{o.id}"))
    await screen(c.message, f"🧾 <b>{o.code}</b>\n{DIV}\n\n{icon(v.name)} {esc(v.name)} · {esc(p.name)}\nTotal  <b>{money(o.amount_etb)}</b>\nStatus  {LABEL.get(o.status, o.status)}\nDate  {when(o.created_at)}",
                 reply_markup=kb(rows + [back("mine")])); await c.answer()

# ---------- 3) profile ----------
@r.callback_query(F.data == "profile")
async def profile(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        u = await s.get(User, c.from_user.id)
        done = await s.scalar(select(func.count()).select_from(Order).where(Order.user_id == u.tg_id, Order.status == "completed", Order.deleted_at.is_(None)))
        bal = await balance(s, u.tg_id)
    await screen(c.message, f"👤 <b>My profile</b>\n{DIV}\n\n<b>Name</b>  {esc(u.name)}\n<b>Username</b>  {('@' + esc(u.username)) if u.username else '—'}\n<b>ID</b>  <code>{u.tg_id}</code>\n"
                 f"<b>Member since</b>  {when(u.created_at)}\n<b>Delivered orders</b>  {done}\n<b>Wallet</b>  {money(bal)}",
                 reply_markup=kb([("💼 Wallet", "wallet"), back("menu")])); await c.answer()

# ---------- 5) wallet ----------
@r.callback_query(F.data == "wallet")
async def wallet(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        bal = await balance(s, c.from_user.id)
        waiting = await s.scalar(select(func.coalesce(func.sum(WalletTx.amount), 0)).where(WalletTx.user_id == c.from_user.id, WalletTx.status == "submitted"))
    extra = f"\n🔎 Deposit being verified: <b>{money(waiting)}</b>" if waiting else ""
    await screen(c.message, f"💼 <b>Wallet</b>\n{DIV}\n\nBalance\n<b>{money(bal)}</b>{extra}\n\n<i>Top up once and pay for orders in one tap.</i>",
                 reply_markup=kb([("➕ Deposit", "wdep"), ("🧾 History", "whist"), back("menu")])); await c.answer()

@r.callback_query(F.data == "wdep")
async def deposit(c: CallbackQuery, state: FSMContext):
    await state.set_state(Wal.amount); await state.update_data(mid=c.message.message_id)
    await screen(c.message, f"➕ <b>Deposit</b>\n{DIV}\n\nHow much would you like to add?\nSend the amount in ETB (minimum {MIN_DEPOSIT}).", reply_markup=kb([back("wallet")])); await c.answer()

@r.message(Wal.amount, F.text & ~F.text.startswith("/"))
async def deposit_amount(m: Message, state: FSMContext):
    raw = m.text.replace(",", "").replace("ETB", "").strip()
    if not raw.isdigit() or int(raw) < MIN_DEPOSIT: return await m.answer(f"Please send a number of at least {MIN_DEPOSIT}, for example 1000.")
    d = await state.get_data(); amount = int(raw); await state.update_data(amount=amount)
    pay = esc(await get_setting("pay", PAY))
    await screen_by_id(m, d.get("mid"), f"➕ <b>Deposit {money(amount)}</b>\n{DIV}\n\n<b>Payment details</b>\n{pay}\n{DIV}\n\n<i>Send exactly {money(amount)}, then tap “I've paid”.</i>",
                       reply_markup=kb([("✅ I've paid", "wpaid"), back("wallet")]))

@r.callback_query(F.data == "wpaid")
async def deposit_paid(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("amount"): return await c.answer("Please start the deposit again.", show_alert=True)
    await state.set_state(Wal.ref); await state.update_data(mid=c.message.message_id)
    await screen(c.message, "<b>Final step</b>\nSend the <b>transaction reference</b> from your payment confirmation.\nYou can also send a screenshot with the reference as the caption.", reply_markup=kb([back("wallet")])); await c.answer()

@r.message(Wal.ref, F.text | F.photo)
async def deposit_ref(m: Message, state: FSMContext):
    d = await state.get_data(); amount = d["amount"]; mid = d.get("mid")
    ref = ((m.caption if m.photo else m.text) or "").strip().upper()
    if len(ref) < 4: return await m.answer("Please include the transaction reference (at least 4 characters).")
    shot = m.photo[-1].file_id if m.photo else None
    async with Session() as s:
        if await ref_taken(s, ref): return await m.answer("This reference was already used. Please check it or tap 💬 Support.")
        tx = WalletTx(user_id=m.from_user.id, amount=amount, kind="deposit", status="submitted", reference=ref, screenshot_file_id=shot)
        s.add(tx)
        try: await s.commit()
        except IntegrityError:
            await s.rollback(); return await m.answer("This reference was already used. Please check it or tap 💬 Support.")
    await state.clear()
    await screen_by_id(m, mid, f"⏳ <b>Deposit received</b>\n{DIV}\n\n<b>{money(amount)}</b> is being verified.\nYou'll be notified here as soon as it's added to your wallet.", reply_markup=kb([back("wallet")]))
    text = f"💼 Deposit to verify\n{amount} ETB\nRef: {ref}\nFrom: {m.from_user.full_name} (@{m.from_user.username})"
    k = kb([("✅ Approve", f"wd:ok:{tx.id}"), ("❌ Reject", f"wd:no:{tx.id}")], 2)
    for a in ADMINS:
        if shot: await bot.send_photo(a, shot, caption=text[:1000], reply_markup=k)
        else: await bot.send_message(a, text, reply_markup=k)

@r.callback_query(F.data.startswith("wd:"), F.from_user.id.in_(ADMINS))
async def deposit_decision(c: CallbackQuery):
    _, act, tid = c.data.split(":")
    async with Session() as s:
        tx = await s.get(WalletTx, int(tid))
        if tx.status != "submitted": return await c.answer("Already handled.", show_alert=True)
        ok = act == "ok"; tx.status = "approved" if ok else "rejected"
        s.add(AuditLog(actor=c.from_user.id, action=f"{act} deposit {tx.id} ({tx.amount} ETB)")); await s.commit(); bal = await balance(s, tx.user_id)
    if ok:
        await log_activity("💼 A customer topped up their wallet", "deposit")
        await bot.send_message(tx.user_id, f"💼 <b>{money(tx.amount)} added to your wallet</b>\nNew balance: <b>{money(bal)}</b>", parse_mode=HTML, reply_markup=kb([("💼 Wallet", "wallet")]))
    else:
        await bot.send_message(tx.user_id, f"⚠️ We couldn't verify your deposit of {money(tx.amount)}. Please check the reference or contact support.", parse_mode=HTML)
    await c.message.answer(f"Deposit {tx.amount} ETB: {'approved' if ok else 'rejected'}"); await c.answer()

@r.callback_query(F.data == "whist")
async def wallet_history(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s: txs = (await s.scalars(select(WalletTx).where(WalletTx.user_id == c.from_user.id).order_by(WalletTx.id.desc()).limit(12))).all()
    rows = [(f"{'➕' if t.amount > 0 else '➖'} {abs(t.amount):,} ETB · {TXLABEL.get(t.status, t.status)} · {when(t.created_at)}", f"wtx:{t.id}") for t in txs]
    await screen(c.message, f"🧾 <b>Wallet history</b>\n{DIV}\n\n" + ("Tap a transaction for details." if txs else "No transactions yet."), reply_markup=kb(rows + [back("wallet")])); await c.answer()

@r.callback_query(F.data.startswith("wtx:"))
async def wallet_tx(c: CallbackQuery):
    async with Session() as s:
        t = await s.get(WalletTx, int(c.data[4:]))
        if not t or t.user_id != c.from_user.id: return await c.answer("Not found.", show_alert=True)
    kind = "Deposit" if t.kind == "deposit" else "Payment"
    await screen(c.message, f"🧾 <b>{kind}</b>\n{DIV}\n\nAmount  <b>{'+' if t.amount > 0 else '−'}{abs(t.amount):,} ETB</b>\nStatus  {TXLABEL.get(t.status, t.status)}\nDate  {when(t.created_at)}\n"
                 + (f"Reference  <code>{esc(t.reference)}</code>\n" if t.reference else "") + (f"Order  {esc(t.note)}\n" if t.note else ""), reply_markup=kb([back("whist")])); await c.answer()

# ---------- 6) pending payment ----------
@r.callback_query(F.data == "pending")
async def pending(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s:
        due = (await s.scalars(select(Order).where(Order.user_id == c.from_user.id, Order.status == "pending_payment", Order.deleted_at.is_(None)).order_by(Order.id.desc()).limit(8))).all()
        checking = await s.scalar(select(func.count()).select_from(Order).where(Order.user_id == c.from_user.id, Order.status == "payment_submitted", Order.deleted_at.is_(None)))
    pay = esc(await get_setting("pay", PAY))
    text = f"⏳ <b>Pending payment</b>\n{DIV}\n\n"
    if due: text += f"You have {len(due)} order(s) waiting for payment. Pay with:\n\n{pay}\n\n<i>Add the order number to the transfer note, then tap the order and “I've paid”.</i>"
    else: text += "Nothing is waiting for payment. ✅"
    if checking: text += f"\n\n🔎 {checking} payment(s) are being verified by our team."
    await screen(c.message, text, reply_markup=kb([(f"{o.code} · {money(o.amount_etb)}", f"ord:{o.id}") for o in due] + [back("menu")])); await c.answer()

# ---------- 8) reviews ----------
@r.callback_query(F.data == "reviews")
async def reviews(c: CallbackQuery, state: FSMContext):
    await state.clear(); is_admin = c.from_user.id in ADMINS
    async with Session() as s:
        rows = (await s.execute(select(Review, Order, User).join(Order, Order.id == Review.order_id).join(User, User.tg_id == Review.user_id).where(Review.hidden.is_(False)).order_by(Review.id.desc()).limit(8))).all()
        avg, n = (await s.execute(select(func.avg(Review.rating), func.count()).where(Review.hidden.is_(False)))).one()
        cards = []
        for rv, o, u in rows:
            v, p = await names(s, o)
            quote = f"\n“{esc(rv.text[:200])}”" if rv.text else ""
            cards.append(f"{'⭐' * rv.rating}  <b>{esc(v.name)} · {esc(p.name)}</b>{quote}\n— {esc(mask(u.name))} · {when(rv.created_at)}" + (f"  (#{rv.id})" if is_admin else ""))
    head = f"⭐ <b>Reviews</b>\n{DIV}\n\n" + (f"<b>{float(avg):.1f} / 5</b> · {n} review(s)\n\n" if n else "No reviews yet. Be the first!\n\n")
    await screen(c.message, head + "\n\n".join(cards), reply_markup=kb([("✍️ Write a review", "rvnew"), back("menu")])); await c.answer()

@r.callback_query(F.data == "rvnew")
async def review_new(c: CallbackQuery):
    async with Session() as s:
        os_ = (await s.scalars(select(Order).where(Order.user_id == c.from_user.id, Order.status == "completed", Order.deleted_at.is_(None), ~Order.id.in_(select(Review.order_id))).order_by(Order.id.desc()).limit(8))).all()
        rows = []
        for o in os_:
            v, p = await names(s, o); rows.append((f"{o.code} · {v.name} · {p.name}", f"rvo:{o.id}"))
    text = f"✍️ <b>Write a review</b>\n{DIV}\n\n" + ("Which order would you like to review?" if rows else "You can review an order once it's completed.")
    await screen(c.message, text, reply_markup=kb(rows + [back("reviews")])); await c.answer()

@r.callback_query(F.data.startswith("rvo:"))
async def review_rate(c: CallbackQuery, state: FSMContext):
    await state.clear(); oid = int(c.data[4:])
    await screen(c.message, f"⭐ <b>How was your order?</b>\n{DIV}\n\nTap a rating.", reply_markup=kb([("⭐" * n, f"rvr:{oid}:{n}") for n in (5, 4, 3, 2, 1)] + [back(f"ord:{oid}")])); await c.answer()

@r.callback_query(F.data.startswith("rvr:"))
async def review_text(c: CallbackQuery, state: FSMContext):
    _, oid, n = c.data.split(":")
    await state.set_state(Rev.text); await state.update_data(oid=int(oid), rating=int(n), mid=c.message.message_id)
    await screen(c.message, f"{'⭐' * int(n)}\n<b>Add a short comment</b>\nTell others what you liked — or skip.", reply_markup=kb([("Skip comment", "rvskip"), back(f"rvo:{oid}")])); await c.answer()

async def save_review(uid, name, oid, rating, text):
    async with Session() as s:
        o = await s.get(Order, oid)
        if not o or o.user_id != uid or o.status != "completed": return None
        if await s.scalar(select(Review.id).where(Review.order_id == oid)): return None
        s.add(Review(order_id=oid, user_id=uid, rating=rating, text=text[:400])); v, p = await names(s, o); await s.commit()
    await log_activity(f"⭐ New {rating}/5 review: {v.name} · {p.name}", "review")
    return True

@r.callback_query(F.data == "rvskip")
async def review_skip(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); await state.clear()
    ok = d and await save_review(c.from_user.id, c.from_user.full_name, d["oid"], d["rating"], "")
    await screen(c.message, "🙏 <b>Thank you!</b>\nYour review is now live." if ok else "This order was already reviewed.", reply_markup=kb([("⭐ See reviews", "reviews"), back("menu")])); await c.answer()

@r.message(Rev.text, F.text & ~F.text.startswith("/"))
async def review_save(m: Message, state: FSMContext):
    d = await state.get_data(); await state.clear()
    ok = await save_review(m.from_user.id, m.from_user.full_name, d["oid"], d["rating"], m.text.strip())
    await screen_by_id(m, d.get("mid"), "🙏 <b>Thank you!</b>\nYour review is now live." if ok else "This order was already reviewed.", reply_markup=kb([("⭐ See reviews", "reviews"), back("menu")]))

# ---------- 4) bot log ----------
@r.callback_query(F.data == "log")
async def bot_log(c: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as s: rows = (await s.scalars(select(Activity).order_by(Activity.id.desc()).limit(15))).all()
    text = f"📜 <b>Bot log</b>\n{DIV}\n\n" + ("\n\n".join(f"{esc(a.text)}\n<i>{ago(a.created_at)}</i>" for a in rows) if rows else "No activity yet.")
    await screen(c.message, text, reply_markup=kb([back("menu")])); await c.answer()

# ---------- 7) support + FAQ ----------
@r.callback_query(F.data == "support")
async def support(c: CallbackQuery, state: FSMContext):
    await state.clear(); who = esc(await get_setting("support", SUPPORT_USERNAME))
    await screen(c.message, f"💬 <b>Support</b>\n{DIV}\n\nWe're here to help.\n\n📩 Contact us directly: <b>{who}</b>\nor send a message to our team right here.",
                 reply_markup=kb([("✉️ Message our team here", "supmsg"), ("❓ FAQ", "faq"), back("menu")])); await c.answer()

@r.callback_query(F.data == "supmsg")
async def support_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.support); await state.update_data(mid=c.message.message_id)
    await screen(c.message, "✉️ <b>Message our team</b>\nType your message and we'll reply right here.", reply_markup=kb([back("support")])); await c.answer()

@r.message(Flow.support, F.text)
async def support_msg(m: Message, state: FSMContext):
    mid = (await state.get_data()).get("mid"); await state.clear()
    async with Session() as s:
        s.add(SupportMessage(user_id=m.from_user.id, text=m.text)); await s.commit()
    for a in ADMINS:
        await bot.send_message(a, f"🆘 {m.from_user.full_name} (id {m.from_user.id}):\n{m.text}\n\nReply: /reply {m.from_user.id} your text")
    await screen_by_id(m, mid, "✅ Message sent. We'll reply here soon.", reply_markup=kb([back("support")]))

@r.callback_query(F.data == "faq")
async def faq(c: CallbackQuery, state: FSMContext):
    await state.clear(); raw = await get_setting("faq", DEFAULT_FAQ)
    body = "\n".join(f"\n<b>{esc(l[2:].strip())}</b>" if l.startswith("Q:") else esc(l) for l in raw.splitlines())
    await screen(c.message, f"❓ <b>FAQ</b>\n{DIV}\n{body}", reply_markup=kb([back("support")])); await c.answer()

# ---------- 9) channel ----------
@r.callback_query(F.data == "channel")
async def channel(c: CallbackQuery, state: FSMContext):
    await state.clear(); ch = (await get_setting("channel", CHANNEL_ID) or CHANNEL_ID or "").strip()
    link = ch if ch.startswith("http") else (f"https://t.me/{ch[1:]}" if ch.startswith("@") else "")
    text = f"📢 <b>Our channel</b>\n{DIV}\n\nFollow us for offers, results and updates." + ("" if link else "\n\n<i>The channel link isn't set yet.</i>")
    await screen(c.message, text, reply_markup=kb(([("📢 Open channel", link)] if link else []) + [back("menu")])); await c.answer()

@r.message(Command("status"), F.from_user.id.in_(ADMINS))
async def set_status(m: Message):
    try: code, st = m.text.split()[1:3]; oid = int(code.split("-")[1]) - 1000
    except Exception: return await m.answer("Usage: /status ORD-1001 in_progress|completed|cancelled")
    if st not in STATUSES: return await m.answer("Status must be: " + ", ".join(STATUSES))
    async with Session() as s:
        o = await s.get(Order, oid)
        if not o: return await m.answer("Order not found.")
        o.status = st; s.add(AuditLog(actor=m.from_user.id, action=f"status {o.code} -> {st}")); await s.commit()
    await notify_status(o); await m.answer("Done.")

@r.message(Command("export"), F.from_user.id.in_(ADMINS))
async def export(m: Message):
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["order", "user_id", "amount_etb", "status", "created", "details", "payment_ref", "payment_status"])
    async with Session() as s:
        for o in (await s.scalars(select(Order).order_by(Order.id))).all():
            pays = (await s.scalars(select(Payment).where(Payment.order_id == o.id))).all() or [None]
            for p in pays:
                w.writerow([o.code, o.user_id, o.amount_etb, o.status, o.created_at, o.details, p.reference if p else "", p.status if p else ""])
    await m.answer_document(BufferedInputFile(buf.getvalue().encode(), "orders_export.csv"))

# ---------------- extra admin tools ----------------
admin = F.from_user.id.in_(ADMINS)
HELP = """Admin commands
/admin  - control panel with buttons (start here)
/orders [status]  - latest orders
/stats  - counts and revenue
/status ORD-1001 in_progress|completed|cancelled
/report ORD-1001 text  - send a campaign report to the customer
/reply USER_ID text  - answer a support message
/broadcast text  - message all customers
/post text  - publish to your channel only
/announce text  - channel post + DM to all customers
/catalog  - services and packages with IDs
/addservice Name
/addpackage SERVICE_ID PRICE Name
/setprice PACKAGE_ID PRICE
/toggle service|package ID  - hide/show
/delete ORD-1001  - soft delete (kept in DB)
/hidereview ID  - hide/show a review
/export  - CSV of all orders and payments
/backup  - full data backup file"""

def rest(m, n=1):
    p = m.text.split(maxsplit=n); return p[1:] if len(p) > n else []

async def find_order(s, code):
    try: return await s.get(Order, int(code.upper().split("-")[1]) - 1000)
    except Exception: return None

@r.message(Command("help"), admin)
async def help_(m: Message): await m.answer(HELP)

@r.message(Command("orders"), admin)
async def orders(m: Message):
    st = rest(m)
    async with Session() as s:
        q = select(Order).where(Order.deleted_at.is_(None)).order_by(Order.id.desc()).limit(15)
        if st: q = q.where(Order.status == st[0])
        rows = (await s.scalars(q)).all()
    await m.answer("\n".join(f"{o.code} | {o.amount_etb} ETB | {o.status} | user {o.user_id}" for o in rows) or "No orders.")

@r.message(Command("stats"), admin)
async def stats(m: Message):
    async with Session() as s:
        rows = (await s.execute(select(Order.status, func.count(), func.coalesce(func.sum(Order.amount_etb), 0)).where(Order.deleted_at.is_(None)).group_by(Order.status))).all()
        users = await s.scalar(select(func.count()).select_from(User))
    earned = sum(t for st, _, t in rows if st in ("paid", "in_progress", "completed"))
    await m.answer(f"Customers: {users}\n" + "\n".join(f"{st}: {n}" for st, n, _ in rows) + f"\n\nRevenue (paid orders): {earned} ETB")

@r.message(Command("report"), admin)
async def report(m: Message):
    a = rest(m, 2)
    if len(a) < 2: return await m.answer("Usage: /report ORD-1001 your report text")
    async with Session() as s: o = await find_order(s, a[0])
    if not o: return await m.answer("Order not found.")
    await bot.send_message(o.user_id, f"📊 Report for {o.code}\n\n{a[1]}"); await m.answer("Report sent.")

@r.message(Command("reply"), admin)
async def reply(m: Message):
    a = rest(m, 2)
    if len(a) < 2 or not a[0].isdigit(): return await m.answer("Usage: /reply USER_ID text")
    async with Session() as s:
        s.add(SupportMessage(user_id=int(a[0]), text=a[1], from_admin=True)); await s.commit()
    await bot.send_message(int(a[0]), f"💬 Support:\n{a[1]}"); await m.answer("Sent.")

@r.message(Command("broadcast"), admin)
async def broadcast(m: Message):
    a = rest(m)
    if not a: return await m.answer("Usage: /broadcast text")
    async with Session() as s: ids = (await s.scalars(select(User.tg_id))).all()
    ok = 0
    for uid in ids:
        try: await bot.send_message(uid, a[0]); ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await m.answer(f"Delivered to {ok}/{len(ids)} customers.")

@r.message(Command("post"), admin)
async def post(m: Message):
    a = rest(m)
    if not (a and CHANNEL_ID): return await m.answer("Usage: /post text (and set CHANNEL_ID; add the bot as channel admin)")
    await bot.send_message(CHANNEL_ID, a[0]); await m.answer("Posted.")

@r.message(Command("catalog"), admin)
async def catalog(m: Message):
    t = ""
    async with Session() as s:
        for v in (await s.scalars(select(Service).order_by(Service.id))).all():
            t += f"\n[{v.id}] {v.name} {'' if v.active else '(hidden)'}\n"
            for p in (await s.scalars(select(Package).where(Package.service_id == v.id).order_by(Package.id))).all():
                t += f"   ({p.id}) {p.name}: {p.price_etb} ETB {'' if p.active else '(hidden)'}\n"
    await m.answer(t or "Empty.")

@r.message(Command("addservice"), admin)
async def addservice(m: Message):
    a = rest(m)
    if not a: return await m.answer("Usage: /addservice Name")
    async with Session() as s: s.add(Service(name=a[0])); s.add(AuditLog(actor=m.from_user.id, action=f"addservice {a[0]}")); await s.commit()
    await log_activity(f"🆕 New service: {a[0]}", "package")
    await m.answer("Added. See /catalog")

@r.message(Command("addpackage"), admin)
async def addpackage(m: Message):
    a = rest(m, 3)
    if len(a) < 3 or not (a[0].isdigit() and a[1].isdigit()): return await m.answer("Usage: /addpackage SERVICE_ID PRICE Name")
    async with Session() as s:
        s.add(Package(service_id=int(a[0]), price_etb=int(a[1]), name=a[2])); s.add(AuditLog(actor=m.from_user.id, action=f"addpackage {a}")); sv = await s.get(Service, int(a[0])); await s.commit()
    await log_activity(f"🆕 New package: {sv.name if sv else a[0]} · {a[2]} ({money(int(a[1]))})", "package")
    await m.answer("Added. See /catalog")

@r.message(Command("setprice"), admin)
async def setprice(m: Message):
    a = rest(m, 2)
    if len(a) < 2 or not (a[0].isdigit() and a[1].isdigit()): return await m.answer("Usage: /setprice PACKAGE_ID PRICE")
    async with Session() as s:
        p = await s.get(Package, int(a[0]))
        if not p: return await m.answer("Package not found.")
        s.add(AuditLog(actor=m.from_user.id, action=f"setprice {p.id} {p.price_etb}->{a[1]}")); p.price_etb = int(a[1]); await s.commit()
    await m.answer("Price updated (existing orders keep their old price).")

@r.message(Command("toggle"), admin)
async def toggle(m: Message):
    a = rest(m, 2)
    if len(a) < 2 or a[0] not in ("service", "package") or not a[1].isdigit(): return await m.answer("Usage: /toggle service|package ID")
    async with Session() as s:
        x = await s.get(Service if a[0] == "service" else Package, int(a[1]))
        if not x: return await m.answer("Not found.")
        x.active = not x.active; await s.commit()
    await m.answer(f"Now {'visible' if x.active else 'hidden'}.")

@r.message(Command("delete"), admin)
async def delete(m: Message):
    a = rest(m)
    async with Session() as s:
        o = await find_order(s, a[0]) if a else None
        if not o: return await m.answer("Usage: /delete ORD-1001")
        o.deleted_at = now(); s.add(AuditLog(actor=m.from_user.id, action=f"soft-delete {o.code}")); await s.commit()
    await m.answer("Hidden from lists (still stored in the database).")

async def expire_loop():
    """Every 30 min: unpaid orders older than 24h become 'expired' (never deleted)."""
    while True:
        try:
            async with Session() as s:
                rows = (await s.scalars(select(Order).where(Order.status == "pending_payment", Order.deleted_at.is_(None), Order.created_at < now() - timedelta(hours=24)))).all()
                for o in rows: o.status = "expired"
                await s.commit()
            for o in rows:
                try: await bot.send_message(o.user_id, f"⌛ {o.code} expired because no payment was received. Order again anytime with /start.")
                except Exception: pass
        except Exception as e: print("expire_loop error:", e)
        await asyncio.sleep(1800)

@r.message(Command("announce"), admin)
async def announce(m: Message):
    a = rest(m)
    if not a: return await m.answer("Usage: /announce text")
    ch = "skipped (CHANNEL_ID not set)"
    if CHANNEL_ID:
        try: await bot.send_message(CHANNEL_ID, a[0]); ch = "posted"
        except Exception as e: ch = f"failed ({e})"
    async with Session() as s: ids = (await s.scalars(select(User.tg_id))).all()
    ok = 0
    for uid in ids:
        try: await bot.send_message(uid, a[0]); ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await m.answer(f"Channel: {ch}\nDMs delivered: {ok}/{len(ids)}")

from aiogram.filters import StateFilter

@r.message(StateFilter(None), F.photo | (F.text & ~F.text.startswith("/")))
async def fallback(m: Message):
    """Catches messages when the bot has no active step (e.g. after a free-tier restart lost the in-memory step)."""
    if m.text and m.text.startswith("/"): return
    async with Session() as s:
        o = await s.scalar(select(Order).where(Order.user_id == m.from_user.id, Order.status == "pending_payment", Order.deleted_at.is_(None)).order_by(Order.id.desc()))
    if o: await m.answer(f"Did you pay for {o.code}? Tap the button, then send your reference again.", reply_markup=kb([("✅ I've paid", f"paid:{o.id}")]))
    else: await m.answer("Tap a button below 👇", reply_markup=MENU)

# ---------------- /backup : full data copy sent to your Telegram ----------------
import json
from db import Base

def _j(v): return v.isoformat() if hasattr(v, "isoformat") else v

async def make_backup() -> bytes:
    out = {"created_at": now().isoformat(), "tables": {}}
    async with Session() as s:
        for t in Base.metadata.sorted_tables:
            rows = (await s.execute(t.select())).mappings().all()
            out["tables"][t.name] = [{k: _j(v) for k, v in row.items()} for row in rows]
    return json.dumps(out, ensure_ascii=False, indent=1).encode()

@r.message(Command("backup"), admin)
async def backup(m: Message):
    data = await make_backup()
    name = f"backup-{now():%Y-%m-%d-%H%M}.json"
    await m.answer_document(BufferedInputFile(data, name), caption="Full data backup. Keep it private: it contains customer data.")

# ======================= ADMIN PANEL: control everything from Telegram =======================
from db import Setting

class Adm(StatesGroup):
    text = State(); price = State(); newpkg = State(); newsvc = State(); announce = State(); report = State()

async def get_setting(key, default=""):
    async with Session() as s: row = await s.get(Setting, key)
    return row.value if row else default

async def set_setting(key, value, actor=0):
    async with Session() as s:
        row = await s.get(Setting, key)
        if row: row.value = value
        else: s.add(Setting(key=key, value=value))
        s.add(AuditLog(actor=actor, action=f"setting '{key}' changed")); await s.commit()

HOME = ("⬅️ Panel", "a:home")
PANEL = [("📊 Dashboard", "a:dash"), ("💳 Payments to verify", "a:pay"), ("📦 Orders", "a:ord"), ("🛍 Catalog & prices", "a:cat"),
         ("✏️ Edit texts", "a:txt"), ("📣 Announce", "a:ann"), ("💾 Backup", "a:bak"), ("📤 Export CSV", "a:exp")]
TXT = F.text & ~F.text.startswith("/")

async def show(c, text, rows, width=1):
    k = kb(rows, width)
    try: await c.message.edit_text(text, reply_markup=k)
    except Exception: await c.message.answer(text, reply_markup=k)

async def stats_text():
    async with Session() as s:
        rows = (await s.execute(select(Order.status, func.count(), func.coalesce(func.sum(Order.amount_etb), 0)).where(Order.deleted_at.is_(None)).group_by(Order.status))).all()
        users = await s.scalar(select(func.count()).select_from(User))
        waiting = await s.scalar(select(func.count()).select_from(Payment).where(Payment.status == "submitted"))
    earned = sum(t for st, _, t in rows if st in ("paid", "in_progress", "completed"))
    return (f"📊 Dashboard\n\nCustomers: {users}\nPayments waiting for you: {waiting}\n\n" + "\n".join(f"{st}: {n}" for st, n, _ in rows) + f"\n\nRevenue (paid orders): {earned} ETB")

async def send_all(text):
    ch = "skipped (CHANNEL_ID not set)"
    if CHANNEL_ID:
        try: await bot.send_message(CHANNEL_ID, text); ch = "posted"
        except Exception as e: ch = f"failed ({e})"
    async with Session() as s: ids = (await s.scalars(select(User.tg_id))).all()
    ok = 0
    for uid in ids:
        try: await bot.send_message(uid, text); ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    return f"Channel: {ch}\nDMs delivered: {ok}/{len(ids)}"

@r.message(Command("admin", "panel"), admin)
async def open_panel(m: Message, state: FSMContext):
    await state.clear(); await m.answer("🛠 Admin panel", reply_markup=kb(PANEL, 2))

@r.callback_query(F.data.startswith("a:"), admin)
async def panel_cb(c: CallbackQuery, state: FSMContext):
    p = c.data.split(":"); act = p[1]; await c.answer()
    ask = lambda t: c.message.answer(t + "\n\n(send /start to cancel)")
    if act == "home": await state.clear(); return await show(c, "🛠 Admin panel", PANEL, 2)
    if act == "dash": return await show(c, await stats_text(), [HOME])
    if act == "pay":
        async with Session() as s:
            rows = (await s.execute(select(Payment, Order).join(Order, Order.id == Payment.order_id).where(Payment.status == "submitted").order_by(Payment.id).limit(10))).all()
            deps = (await s.scalars(select(WalletTx).where(WalletTx.status == "submitted", WalletTx.kind == "deposit").order_by(WalletTx.id).limit(10))).all()
        if not rows and not deps: return await show(c, "No payments waiting. ✅", [HOME])
        await show(c, f"{len(rows)} order payment(s) and {len(deps)} deposit(s) waiting. Check each reference in your Telebirr/bank app first:", [HOME])
        for pay, o in rows:
            k = kb([("✅ Approve", f"adm:ok:{pay.id}"), ("❌ Reject", f"adm:no:{pay.id}")], 2)
            t = f"{o.code} · {o.amount_etb} ETB\nRef: {pay.reference}"
            if pay.screenshot_file_id: await c.message.answer_photo(pay.screenshot_file_id, caption=t, reply_markup=k)
            else: await c.message.answer(t, reply_markup=k)
        for tx in deps:
            k = kb([("✅ Approve", f"wd:ok:{tx.id}"), ("❌ Reject", f"wd:no:{tx.id}")], 2)
            t = f"💼 Deposit {tx.amount} ETB\nRef: {tx.reference}"
            if tx.screenshot_file_id: await c.message.answer_photo(tx.screenshot_file_id, caption=t, reply_markup=k)
            else: await c.message.answer(t, reply_markup=k)
        return
    if act == "ord" and len(p) == 2:
        return await show(c, "Orders by status:", [(st, f"a:ord:{st}") for st in ("payment_submitted", "paid", "in_progress", "completed", "pending_payment", "expired")] + [HOME], 2)
    if act == "ord":
        async with Session() as s: os_ = (await s.scalars(select(Order).where(Order.status == p[2], Order.deleted_at.is_(None)).order_by(Order.id.desc()).limit(15))).all()
        return await show(c, f"{p[2]}: {len(os_)} order(s)", [(f"{o.code} · {o.amount_etb} ETB", f"a:o:{o.id}") for o in os_] + [("⬅️ Back", "a:ord"), HOME])
    if act == "o":
        async with Session() as s: o = await s.get(Order, int(p[2])); u = await s.get(User, o.user_id)
        return await show(c, f"{o.code} · {o.amount_etb} ETB · {o.status}\nCustomer: {u.name} (@{u.username}) id {u.tg_id}\n\n{o.details}",
            [("▶️ In progress", f"a:st:{o.id}:in_progress"), ("✅ Completed", f"a:st:{o.id}:completed"), ("✖ Cancel", f"a:st:{o.id}:cancelled"),
             ("📝 Send report", f"a:rep:{o.id}"), ("🗑 Hide order", f"a:del:{o.id}"), ("⬅️ Orders", "a:ord")], 2)
    if act in ("st", "del"):
        async with Session() as s:
            o = await s.get(Order, int(p[2]))
            if act == "st" and p[3] in STATUSES: o.status = p[3]; s.add(AuditLog(actor=c.from_user.id, action=f"status {o.code} -> {p[3]}"))
            if act == "del": o.deleted_at = now(); s.add(AuditLog(actor=c.from_user.id, action=f"soft-delete {o.code}"))
            await s.commit()
        if act == "st": await notify_status(o)
        return await show(c, f"{o.code}: done." + (" Customer notified." if act == "st" else ""), [("⬅️ Orders", "a:ord"), HOME])
    if act == "rep": await state.set_state(Adm.report); await state.update_data(oid=int(p[2])); return await ask("Type the report to send to the customer:")
    if act == "cat":
        async with Session() as s: svcs = (await s.scalars(select(Service).order_by(Service.id))).all()
        return await show(c, "🛍 Catalog: pick a service", [(f"{'' if v.active else '🙈 '}{v.name}", f"a:svc:{v.id}") for v in svcs] + [("➕ Add service", "a:newsvc"), HOME])
    if act == "svc":
        async with Session() as s:
            v = await s.get(Service, int(p[2])); pk = (await s.scalars(select(Package).where(Package.service_id == v.id).order_by(Package.id))).all()
        return await show(c, f"{v.name}{'' if v.active else ' (hidden)'}", [(f"{'' if k.active else '🙈 '}{k.name} · {k.price_etb} ETB", f"a:pkg:{k.id}") for k in pk]
            + [("➕ Add package", f"a:newpkg:{v.id}"), ("👁 Hide/Show service", f"a:tog:s:{v.id}"), ("⬅️ Back", "a:cat")])
    if act == "pkg":
        async with Session() as s: k = await s.get(Package, int(p[2]))
        return await show(c, f"{k.name} · {k.price_etb} ETB{'' if k.active else ' (hidden)'}", [("💰 Change price", f"a:price:{k.id}"), ("👁 Hide/Show", f"a:tog:p:{k.id}"), ("⬅️ Back", f"a:svc:{k.service_id}")])
    if act == "tog":
        async with Session() as s:
            x = await s.get(Service if p[2] == "s" else Package, int(p[3])); x.active = not x.active; await s.commit()
        return await show(c, f"Done: now {'visible' if x.active else 'hidden'}.", [("⬅️ Catalog", "a:cat"), HOME])
    if act == "price": await state.set_state(Adm.price); await state.update_data(pid=int(p[2])); return await ask("Send the new price in ETB (numbers only):")
    if act == "newpkg": await state.set_state(Adm.newpkg); await state.update_data(sid=int(p[2])); return await ask("Send: PRICE then name. Example:\n5000 Pro package")
    if act == "newsvc": await state.set_state(Adm.newsvc); return await ask("Send the new service name:")
    if act == "txt": return await show(c, "✏️ Which text do you want to edit?", [("Brand name", "a:t:brand"), ("Tagline", "a:t:tagline"), ("Welcome text", "a:t:welcome"), ("Payment instructions", "a:t:pay"), ("FAQ", "a:t:faq"), ("Support username", "a:t:support"), ("Channel link", "a:t:channel"), HOME])
    if act == "t":
        cur = await get_setting(p[2], DEFAULTS.get(p[2], ""))
        await state.set_state(Adm.text); await state.update_data(key=p[2]); return await ask(f"Current text:\n\n{cur}\n\nSend the NEW text now:")
    if act == "ann": await state.set_state(Adm.announce); return await ask("Send the announcement. It goes to your channel AND to every customer's DM:")
    if act == "bak": return await backup(c.message)
    if act == "exp": return await export(c.message)

@r.message(Adm.text, TXT, admin)
async def adm_text(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]; await state.clear()
    await set_setting(key, m.text, m.from_user.id); await m.answer("✅ Saved. Customers see it from now on.", reply_markup=kb([HOME]))

@r.message(Adm.price, TXT, admin)
async def adm_price(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Numbers only, e.g. 4500")
    pid = (await state.get_data())["pid"]; await state.clear()
    async with Session() as s:
        k = await s.get(Package, pid); s.add(AuditLog(actor=m.from_user.id, action=f"price {k.id} {k.price_etb}->{m.text.strip()}")); k.price_etb = int(m.text); await s.commit()
    await m.answer(f"✅ {k.name} is now {k.price_etb} ETB (existing orders keep their old price).", reply_markup=kb([("⬅️ Catalog", "a:cat"), HOME]))

@r.message(Adm.newpkg, TXT, admin)
async def adm_newpkg(m: Message, state: FSMContext):
    a = m.text.split(maxsplit=1)
    if len(a) < 2 or not a[0].isdigit(): return await m.answer("Format: PRICE then name. Example: 5000 Pro package")
    sid = (await state.get_data())["sid"]; await state.clear()
    async with Session() as s: s.add(Package(service_id=sid, price_etb=int(a[0]), name=a[1])); sv = await s.get(Service, sid); await s.commit()
    await log_activity(f"🆕 New package: {sv.name} · {a[1]} ({money(int(a[0]))})", "package")
    await m.answer("✅ Package added.", reply_markup=kb([("⬅️ Catalog", "a:cat"), HOME]))

@r.message(Adm.newsvc, TXT, admin)
async def adm_newsvc(m: Message, state: FSMContext):
    await state.clear()
    async with Session() as s: s.add(Service(name=m.text.strip())); await s.commit()
    await log_activity(f"🆕 New service: {m.text.strip()}", "package")
    await m.answer("✅ Service added. Open it in Catalog to add packages.", reply_markup=kb([("⬅️ Catalog", "a:cat"), HOME]))

@r.message(Adm.announce, TXT, admin)
async def adm_announce(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Sending…"); await m.answer(await send_all(m.text), reply_markup=kb([HOME]))

@r.message(Adm.report, TXT, admin)
async def adm_report(m: Message, state: FSMContext):
    oid = (await state.get_data())["oid"]; await state.clear()
    async with Session() as s: o = await s.get(Order, oid)
    await bot.send_message(o.user_id, f"📊 Report for {o.code}\n\n{m.text}"); await m.answer("✅ Report sent.", reply_markup=kb([HOME]))


@r.message(Command("hidereview"), admin)
async def hide_review(m: Message):
    a = rest(m)
    if not (a and a[0].isdigit()): return await m.answer("Usage: /hidereview ID (the #number shown in the Reviews screen)")
    async with Session() as s:
        x = await s.get(Review, int(a[0]))
        if not x: return await m.answer("Review not found.")
        x.hidden = not x.hidden; await s.commit()
    await m.answer("Review hidden." if x.hidden else "Review visible again.")
