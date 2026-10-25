# Ad service bot

## Setup (all free)
1. @BotFather -> create bot, copy token. @userinfobot -> your Telegram user ID.
2. Neon.tech -> free Postgres project -> Connect -> copy the postgresql:// string.
3. Push this folder to a PRIVATE GitHub repo.
4. Render -> New Web Service from the repo (free plan):
   - Build command:  pip install -r requirements.txt
   - Start command:  uvicorn main:app --host 0.0.0.0 --port $PORT
   - Environment: BOT_TOKEN, DATABASE_URL, ADMIN_IDS, WEBHOOK_SECRET, PAY_INSTRUCTIONS, CHANNEL_ID (optional)
5. Open your bot, send /start. As admin send /help.

## Backups (no GitHub needed)
Send /backup to the bot: it replies with a .json file containing ALL data.
Do it before every update and daily when orders are coming in. Keep the files private.

## Updating safely
/backup -> push code -> Render redeploys -> database migrations run automatically (rows are kept).
Never delete the Neon project or point DATABASE_URL at a new empty database.
