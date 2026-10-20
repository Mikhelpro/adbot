import asyncio, datetime as dt, os
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_url = os.environ["DATABASE_URL"].split("?")[0]
_url = _url.replace("postgres://", "postgresql+asyncpg://", 1).replace("postgresql://", "postgresql+asyncpg://", 1)
_ssl = os.environ.get("DB_SSL", "1") == "1"
CONNECT_ARGS = {"ssl": True} if _ssl else {}
engine = create_async_engine(_url, connect_args=CONNECT_ARGS, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)
now = lambda: dt.datetime.now(dt.timezone.utc)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)

class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(default=True)

class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    name: Mapped[str] = mapped_column(String(100))
    price_etb: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(default=True)

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"))
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    details: Mapped[str] = mapped_column(Text)
    amount_etb: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending_payment")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    @property
    def code(self): return f"ORD-{1000 + self.id}"

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    reference: Mapped[str] = mapped_column(String(100), unique=True)  # same reference can never pay twice
    screenshot_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    verified_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)

class SupportMessage(Base):
    __tablename__ = "support_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    from_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)

SEED = {  # PLACEHOLDERS: edit names/prices
    "TikTok Ads": [("Starter", 3000), ("Growth", 8000)],
    "Meta Ads": [("Starter", 3000), ("Growth", 8000)],
    "Telegram Ads": [("Single post", 1500), ("Weekly pack", 6000)],
    "Web Hosting": [("Basic (1 yr)", 2500), ("Business (1 yr)", 6000)],
}

def _upgrade():
    from alembic import command
    from alembic.config import Config
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    command.upgrade(cfg, "head")

async def init():
    await asyncio.to_thread(_upgrade)  # schema changes ONLY via migrations; existing rows are kept
    async with Session() as s:
        if not (await s.execute(select(Service))).first():
            for name, pkgs in SEED.items():
                svc = Service(name=name); s.add(svc); await s.flush()
                for pn, price in pkgs: s.add(Package(service_id=svc.id, name=pn, price_etb=price))
            await s.commit()
