import asyncio, csv, io, os
from datetime import timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from db import AuditLog, Order, Package, Payment, Service, Session, SupportMessage, User, now

bot = Bot(os.environ["BOT_TOKEN"])
dp = Dispatcher(storage=MemoryStorage())
r = Router(); dp.include_router(r)
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PAY = os.environ.get("PAY_INSTRUCTIONS", "Telebirr: 09XXXXXXXX").replace("\\n", "\n")
STATUSES = {"in_progress", "completed", "cancelled"}

class Flow(StatesGroup):
    details = State()
    ref = State()
    support = State()

def kb(rows, width=1):
    b = InlineKeyboardBuilder()
    for text, data in rows: b.button(text=text, callback_data=data)
    b.adjust(width); return b.as_markup()

MENU = kb([("🛒 Order", "order"), ("💰 Prices", "prices"), ("📦 My orders", "mine"), ("🆘 Support", "support")], 2)

@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    async with Session() as s:
        if not await s.get(User, m.from_user.id):
            s.add(User(tg_id=m.from_user.id, name=m.from_user.full_name, username=m.from_user.username)); await s.commit()
    await m.answer("Welcome! What would you like to do?", reply_markup=MENU)

@r.callback_query(F.data == "prices")
async def prices(c: CallbackQuery):
    async with Session() as s:
        text = ""
        for svc in (await s.scalars(select(Service).where(Service.active))).all():
            text += f"\n{svc.name}\n"
            for p in (await s.scalars(select(Package).where(Package.service_id == svc.id, Package.active))).all():
                text += f"  • {p.name}: {p.price_etb} ETB\n"
    await c.message.answer(text or "No services yet.", reply_markup=kb([("🛒 Order", "order")])); await c.answer()

@r.callback_query(F.data == "order")
async def order(c: CallbackQuery):
    async with Session() as s:
        svcs = (await s.scalars(select(Service).where(Service.active))).all()
    await c.message.answer("Choose a service:", reply_markup=kb([(x.name, f"svc:{x.id}") for x in svcs])); await c.answer()

@r.callback_query(F.data.startswith("svc:"))
async def pick_service(c: CallbackQuery):
    sid = int(c.data[4:])
    async with Session() as s:
        pk = (await s.scalars(select(Package).where(Package.service_id == sid, Package.active))).all()
    await c.message.answer("Choose a package:", reply_markup=kb([(f"{p.name} - {p.price_etb} ETB", f"pkg:{p.id}") for p in pk])); await c.answer()

@r.callback_query(F.data.startswith("pkg:"))
async def pick_package(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.details); await state.update_data(pkg=int(c.data[4:]))
    await c.message.answer("Describe what you need (goal, link/page, budget, target audience):"); await c.answer()

@r.message(Flow.details, F.text)
async def got_details(m: Message, state: FSMContext):
    pkg_id = (await state.get_data())["pkg"]; await state.clear()
    async with Session() as s:
        p = await s.get(Package, pkg_id)
        recent = await s.scalar(select(func.count()).select_from(Order).where(Order.user_id == m.from_user.id, Order.created_at > now() - timedelta(hours=1)))
        if recent >= 5: return await m.answer("Too many orders in the last hour. Please try again later.")
        o = Order(user_id=m.from_user.id, package_id=p.id, details=m.text, amount_etb=p.price_etb)
        s.add(o); await s.commit()
    await m.answer(f"Order {o.code} created.\n\nPlease pay {o.amount_etb} ETB to:\n{PAY}\n\n"
                   f"Use {o.code} as the note if possible. After paying, tap the button below.",
                   reply_markup=kb([("✅ I've paid", f"paid:{o.id}")]))

@r.callback_query(F.data.startswith("paid:"))
async def paid(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.ref); await state.update_data(order=int(c.data[5:]))
    await c.message.answer("Send the transaction reference number (or a screenshot with the reference as caption):"); await c.answer()

@r.message(Flow.ref, F.text | F.photo)
async def got_ref(m: Message, state: FSMContext):
    oid = (await state.get_data())["order"]
    ref = ((m.caption if m.photo else m.text) or "").strip().upper()
    if len(ref) < 4: return await m.answer("Please include the transaction reference (at least 4 characters).")
    shot = m.photo[-1].file_id if m.photo else None
    async with Session() as s:
        o = await s.get(Order, oid)
        pay = Payment(order_id=oid, reference=ref, screenshot_file_id=shot)
        s.add(pay)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback(); return await m.answer("This reference was already used. Check it or contact support.")
        o.status = "payment_submitted"; await s.commit()
    await state.clear()
    await m.answer(f"Thanks! We're verifying your payment for {o.code}. You'll get a message soon.")
    text = f"💳 Payment to verify\n{o.code} - {o.amount_etb} ETB\nRef: {ref}\nFrom: {m.from_user.full_name} (@{m.from_user.username})\n\n{o.details}"
    k = kb([("✅ Approve", f"adm:ok:{pay.id}"), ("❌ Reject", f"adm:no:{pay.id}")], 2)
    for a in ADMINS:
        if shot: await bot.send_photo(a, shot, caption=text[:1000], reply_markup=k)
        else: await bot.send_message(a, text, reply_markup=k)

@r.callback_query(F.data.startswith("adm:"), F.from_user.id.in_(ADMINS))
async def admin_decision(c: CallbackQuery):
    _, act, pid = c.data.split(":")
    async with Session() as s:
        pay = await s.get(Payment, int(pid)); o = await s.get(Order, pay.order_id)
        if pay.status != "submitted": return await c.answer("Already handled.", show_alert=True)
        ok = act == "ok"
        pay.status, pay.verified_by = ("approved" if ok else "rejected"), c.from_user.id
        o.status = "paid" if ok else "pending_payment"
        s.add(AuditLog(actor=c.from_user.id, action=f"{act} payment {pay.id} ({o.code})")); await s.commit()
    await bot.send_message(o.user_id, f"✅ Payment for {o.code} confirmed. We're starting work!" if ok else
        f"❌ We couldn't verify the payment for {o.code}. Please check and tap 'I've paid' again.",
        reply_markup=None if ok else kb([("✅ I've paid", f"paid:{o.id}")]))
    await c.message.answer(f"{o.code}: {'approved' if ok else 'rejected'}"); await c.answer()

@r.callback_query(F.data == "mine")
async def mine(c: CallbackQuery):
    async with Session() as s:
        os_ = (await s.scalars(select(Order).where(Order.user_id == c.from_user.id, Order.deleted_at.is_(None)).order_by(Order.id.desc()).limit(10))).all()
    await c.message.answer("\n".join(f"{o.code} - {o.amount_etb} ETB - {o.status}" for o in os_) or "No orders yet."); await c.answer()

@r.callback_query(F.data == "support")
async def support(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.support)
    await c.message.answer("Type your message for our team (send /start to cancel):"); await c.answer()

@r.message(Flow.support, F.text)
async def support_msg(m: Message, state: FSMContext):
    await state.clear()
    async with Session() as s:
        s.add(SupportMessage(user_id=m.from_user.id, text=m.text)); await s.commit()
    for a in ADMINS:
        await bot.send_message(a, f"🆘 {m.from_user.full_name} (id {m.from_user.id}):\n{m.text}\n\nReply: /reply {m.from_user.id} your text")
    await m.answer("Sent! We'll reply here soon.")

@r.message(Command("status"), F.from_user.id.in_(ADMINS))
async def set_status(m: Message):
    try: code, st = m.text.split()[1:3]; oid = int(code.split("-")[1]) - 1000
    except Exception: return await m.answer("Usage: /status ORD-1001 in_progress|completed|cancelled")
    if st not in STATUSES: return await m.answer("Status must be: " + ", ".join(STATUSES))
    async with Session() as s:
        o = await s.get(Order, oid)
        if not o: return await m.answer("Order not found.")
        o.status = st; s.add(AuditLog(actor=m.from_user.id, action=f"status {o.code} -> {st}")); await s.commit()
    await bot.send_message(o.user_id, f"Update: {o.code} is now {st}."); await m.answer("Done.")

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
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
HELP = """Admin commands
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
/export  - CSV of all orders and payments"""

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
    await m.answer("Added. See /catalog")

@r.message(Command("addpackage"), admin)
async def addpackage(m: Message):
    a = rest(m, 3)
    if len(a) < 3 or not (a[0].isdigit() and a[1].isdigit()): return await m.answer("Usage: /addpackage SERVICE_ID PRICE Name")
    async with Session() as s:
        s.add(Package(service_id=int(a[0]), price_etb=int(a[1]), name=a[2])); s.add(AuditLog(actor=m.from_user.id, action=f"addpackage {a}")); await s.commit()
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

@r.message(StateFilter(None), F.text | F.photo)
async def fallback(m: Message):
    """Catches messages when the bot has no active step (e.g. after a free-tier restart lost the in-memory step)."""
    if m.text and m.text.startswith("/"): return
    async with Session() as s:
        o = await s.scalar(select(Order).where(Order.user_id == m.from_user.id, Order.status == "pending_payment", Order.deleted_at.is_(None)).order_by(Order.id.desc()))
    if o: await m.answer(f"Did you pay for {o.code}? Tap the button, then send your reference again.", reply_markup=kb([("✅ I've paid", f"paid:{o.id}")]))
    else: await m.answer("Please use the menu:", reply_markup=MENU)
