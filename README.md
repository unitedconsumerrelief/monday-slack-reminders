# Monday → Slack Reminders (UCR/DL)

Polls a Monday.com board. If Status=Suspended, sends Slack notifications every N hours until Status=Active. Routes messages to UCR or DL channel based on "UCR / DL" column. Persists state in a "Last Notified" text column.

## Env Vars
MONDAY_API_KEY=...
BOARD_ID=123456789
COLUMN_STATUS_TITLE=Status
COLUMN_TAG_TITLE=UCR / DL
COLUMN_LAST_NOTIFIED_TITLE=Last Notified
SLACK_WEBHOOK_UCR=https://hooks.slack.com/services/...
SLACK_WEBHOOK_DL=https://hooks.slack.com/services/...
NOTIFY_INTERVAL_HOURS=2
POLL_SECONDS=300

## Run locally
pip install -r requirements.txt
$env:MONDAY_API_KEY="..."   # set env vars
python app.py
open http://localhost:5000/health

## Deploy (Render)
Build: pip install -r requirements.txt
Start: gunicorn app:app
Add the env vars above in Render dashboard.

## Initialize git and push to GitHub

Use the terminal inside Cursor:

git init
git add .
git commit -m "Initial commit: Monday→Slack reminders"
# Replace with your new repo URL:
git remote add origin https://github.com/<your-username>/monday-slack-reminders.git
git branch -M main
git push -u origin main

## Render deployment settings

Service type: Web Service (Free)

Runtime: Python 3.11

Build command: pip install -r requirements.txt

Start command: gunicorn app:app

Environment variables: set all from README (paste your Slack webhooks + Monday API key + board id).

After deploy, visit /health to confirm ("OK").

## Quick validation

On the board, ensure columns exist (exact titles):

- Status (values include Suspended, Active)
- UCR / DL (text/dropdown with UCR or DL)
- Last Notified (Text)

Flip an item to Suspended and set UCR / DL.

First ping arrives within one poll cycle (default up to 5 min).

Change to Active → pings stop and Last Notified clears.
