"""editable texts (welcome, payment instructions)"""
from alembic import op
import sqlalchemy as sa
revision = "0002"; down_revision = "0001"

def upgrade():
    if "settings" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("settings", sa.Column("key", sa.String(50), primary_key=True), sa.Column("value", sa.Text, nullable=False))

def downgrade():
    pass
