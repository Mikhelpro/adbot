import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
import db

def _run(conn):
    context.configure(connection=conn, target_metadata=db.Base.metadata)
    with context.begin_transaction():
        context.run_migrations()

async def _main():
    eng = create_async_engine(db._url, connect_args=db.CONNECT_ARGS)
    async with eng.begin() as c:
        await c.run_sync(_run)
    await eng.dispose()

asyncio.run(_main())
