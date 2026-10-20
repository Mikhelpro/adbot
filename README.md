# Ad service bot (complete)

## Setup (all free)
1. @BotFather -> create bot, copy token. @userinfobot -> your user ID.
2. Neon.tech -> free Postgres project -> copy connection string.
3. Push this folder to a PRIVATE GitHub repo.
4. Render -> New Blueprint from the repo (free plan). Fill env vars from .env.example.
5. GitHub repo -> Settings -> Secrets -> add DATABASE_URL (Neon DIRECT, non-pooled URL), BOT_TOKEN, BACKUP_CHAT_ID (your user ID or a private group ID). Message your bot once first so it can write to you.
6. Actions tab -> run "nightly-db-backup" manually once and confirm the file arrives in Telegram.
7. Send /start to your bot. Send /help for admin commands.

## Before launch
Edit SEED prices in db.py (or use /addservice /addpackage /setprice), PAY_INSTRUCTIONS env var, support contact is /reply based.

## Updating safely
Run the backup workflow -> deploy -> migrations run automatically at startup (rows are kept).
To change tables: `alembic revision -m "change"` locally, edit, commit. Never drop tables.

## Restore
pg_restore --clean --no-owner -d "$NEW_DATABASE_URL" backup-XXXX.dump
(Test this once on a spare free Neon database.)
