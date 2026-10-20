"""initial schema (skips tables that already exist)"""
from alembic import op
import sqlalchemy as sa
revision = "0001"; down_revision = None
TS = lambda n: sa.Column(n, sa.DateTime(timezone=True), nullable=False)

def upgrade():
    have = set(sa.inspect(op.get_bind()).get_table_names())
    def mk(name, *cols):
        if name not in have: op.create_table(name, *cols)
    mk("users", sa.Column("tg_id", sa.BigInteger, primary_key=True), sa.Column("name", sa.String(200), nullable=False),
       sa.Column("username", sa.String(100)), TS("created_at"))
    mk("services", sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String(100), nullable=False),
       sa.Column("active", sa.Boolean, nullable=False))
    mk("packages", sa.Column("id", sa.Integer, primary_key=True), sa.Column("service_id", sa.Integer, sa.ForeignKey("services.id"), nullable=False),
       sa.Column("name", sa.String(100), nullable=False), sa.Column("price_etb", sa.Integer, nullable=False), sa.Column("active", sa.Boolean, nullable=False))
    mk("orders", sa.Column("id", sa.Integer, primary_key=True), sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.tg_id"), nullable=False),
       sa.Column("package_id", sa.Integer, sa.ForeignKey("packages.id"), nullable=False), sa.Column("details", sa.Text, nullable=False),
       sa.Column("amount_etb", sa.Integer, nullable=False), sa.Column("status", sa.String(30), nullable=False), TS("created_at"),
       sa.Column("deleted_at", sa.DateTime(timezone=True)))
    mk("payments", sa.Column("id", sa.Integer, primary_key=True), sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
       sa.Column("reference", sa.String(100), nullable=False, unique=True), sa.Column("screenshot_file_id", sa.String(300)),
       sa.Column("status", sa.String(20), nullable=False), sa.Column("verified_by", sa.BigInteger), TS("created_at"))
    mk("audit_log", sa.Column("id", sa.Integer, primary_key=True), sa.Column("actor", sa.BigInteger, nullable=False),
       sa.Column("action", sa.String(300), nullable=False), TS("created_at"))
    mk("support_messages", sa.Column("id", sa.Integer, primary_key=True), sa.Column("user_id", sa.BigInteger, nullable=False),
       sa.Column("text", sa.Text, nullable=False), sa.Column("from_admin", sa.Boolean, nullable=False), TS("created_at"))

def downgrade():
    pass  # never drop customer data automatically
