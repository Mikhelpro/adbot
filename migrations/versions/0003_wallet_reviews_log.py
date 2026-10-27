"""wallet, reviews, activity log"""
from alembic import op
import sqlalchemy as sa
revision = "0003"; down_revision = "0002"
TS = lambda: sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)

def upgrade():
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "wallet_tx" not in have:
        op.create_table("wallet_tx", sa.Column("id", sa.Integer, primary_key=True), sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.tg_id"), nullable=False),
            sa.Column("amount", sa.Integer, nullable=False), sa.Column("kind", sa.String(20), nullable=False), sa.Column("status", sa.String(20), nullable=False),
            sa.Column("reference", sa.String(100), unique=True), sa.Column("screenshot_file_id", sa.String(300)), sa.Column("note", sa.String(200)), TS())
    if "reviews" not in have:
        op.create_table("reviews", sa.Column("id", sa.Integer, primary_key=True), sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False, unique=True),
            sa.Column("user_id", sa.BigInteger, nullable=False), sa.Column("rating", sa.Integer, nullable=False), sa.Column("text", sa.Text, nullable=False),
            sa.Column("hidden", sa.Boolean, nullable=False), TS())
    if "activity" not in have:
        op.create_table("activity", sa.Column("id", sa.Integer, primary_key=True), sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("text", sa.String(300), nullable=False), TS())

def downgrade():
    pass  # never drop customer data automatically
