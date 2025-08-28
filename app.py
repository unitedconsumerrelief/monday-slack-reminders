import os
import time
import json
import threading
import requests
from flask import Flask

# ──────────────────────────────────────────────────────────────────────────────
# Config via Environment Variables (title-based; no admin needed)
# ──────────────────────────────────────────────────────────────────────────────
MONDAY_API_KEY = os.getenv("MONDAY_API_KEY", "").strip()
BOARD_ID = int(os.getenv("BOARD_ID", "0"))

# Read columns by their DISPLAY TITLES (case-insensitive)
COLUMN_STATUS_TITLE = os.getenv("COLUMN_STATUS_TITLE", "Status")
COLUMN_TAG_TITLE = os.getenv("COLUMN_TAG_TITLE", "UCR / DL")      # expects "UCR" or "DL"
COLUMN_LAST_NOTIFIED_TITLE = os.getenv("COLUMN_LAST_NOTIFIED_TITLE", "Last Notified")  # Text column

# Slack targets (Incoming Webhook URLs)
SLACK_WEBHOOK_UCR = os.getenv("SLACK_WEBHOOK_UCR", "").strip()
SLACK_WEBHOOK_DL  = os.getenv("SLACK_WEBHOOK_DL", "").strip()

# Behaviour knobs
NOTIFY_INTERVAL_HOURS = float(os.getenv("NOTIFY_INTERVAL_HOURS", "2"))  # every 2h by default
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))                    # poll Monday every 5 min

# ──────────────────────────────────────────────────────────────────────────────
# HTTP setup
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
MONDAY_API_URL = "https://api.monday.com/v2"
HEADERS = {"Authorization": MONDAY_API_KEY, "Content-Type": "application/json"}

# Cache for board columns (title -> id), refreshed periodically
_columns_cache = {"ts": 0, "map": {}}
COLUMNS_CACHE_TTL = 10 * 60  # 10 minutes


# ──────────────────────────────────────────────────────────────────────────────
# Utils
# ──────────────────────────────────────────────────────────────────────────────
def monday_graphql(query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        MONDAY_API_URL,
        headers=HEADERS,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def get_columns_map(force_refresh: bool = False) -> dict:
    """
    Returns {lowercased_title: column_id}. Caches for performance.
    """
    now = time.time()
    if (not force_refresh) and _columns_cache["map"] and (now - _columns_cache["ts"] < COLUMNS_CACHE_TTL):
        return _columns_cache["map"]

    q = """
    query($boardId: [ID!]) {
      boards(ids: $boardId) {
        columns { id title }
      }
    }
    """
    data = monday_graphql(q, {"boardId": str(BOARD_ID)})
    cols = data["boards"][0]["columns"]
    cmap = {(c["title"] or "").strip().lower(): c["id"] for c in cols}
    _columns_cache["map"] = cmap
    _columns_cache["ts"] = now
    return cmap


def fetch_items() -> list[dict]:
    """
    Pull items with their column display titles & text values.
    """
    q = """
    query($boardId: [ID!]) {
      boards(ids: $boardId) {
        items_page {
          items {
            id
            name
            column_values { id type text }
          }
        }
      }
    }
    """
    data = monday_graphql(q, {"boardId": str(BOARD_ID)})
    return data["boards"][0]["items_page"]["items"]


def get_col_text_by_title(item: dict, wanted_title: str) -> str:
    target = (wanted_title or "").strip().lower()
    for cv in item.get("column_values", []):
        if (cv.get("type") or "").strip().lower() == target:
            return (cv.get("text") or "").strip()
    return ""


def set_text_column_by_title(item_id: int | str, wanted_title: str, value_str: str) -> None:
    """
    Writes to a TEXT-type column using its title (we map title->id first).
    Stores epoch seconds as string (or "" to clear).
    """
    cmap = get_columns_map()
    col_id = cmap.get((wanted_title or "").strip().lower())
    if not col_id:
        # Try a forced refresh once (in case titles changed recently)
        cmap = get_columns_map(force_refresh=True)
        col_id = cmap.get((wanted_title or "").strip().lower())
        if not col_id:
            print(f"[WARN] Column with title '{wanted_title}' not found; skipping update for item {item_id}")
            return

    mutation = """
    mutation($boardId: Int!, $itemId: Int!, $columnId: String!, $value: String!) {
      change_simple_column_value(
        board_id: $boardId,
        item_id: $itemId,
        column_id: $columnId,
        value: $value
      ) { id }
    }
    """
    # Monday expects the "value" field itself to be a JSON string.
    payload_value = json.dumps(str(value_str))
    monday_graphql(
        mutation,
        {
            "boardId": int(BOARD_ID),
            "itemId": int(item_id),
            "columnId": col_id,
            "value": payload_value,
        },
    )


def post_to_slack(webhook: str, text: str) -> None:
    if not webhook:
        return
    r = requests.post(webhook, json={"text": text}, timeout=15)
    r.raise_for_status()


def universal_item_link(board_id: int, item_id: int | str) -> str:
    # Account-agnostic permalink that redirects properly
    return f"https://view.monday.com/boards/{board_id}/pulses/{item_id}"


def should_notify(last_epoch_text: str, now_epoch: float, interval_hours: float) -> bool:
    if not last_epoch_text:
        return True
    try:
        last = float(last_epoch_text)
    except ValueError:
        return True
    return (now_epoch - last) >= interval_hours * 3600


# ──────────────────────────────────────────────────────────────────────────────
# Core cycle
# ──────────────────────────────────────────────────────────────────────────────
def process_cycle():
    now = time.time()
    try:
        items = fetch_items()
    except Exception as e:
        print("[ERROR] Fetch items failed:", e)
        return

    for it in items:
        item_id = it["id"]
        name = it["name"]

        status = get_col_text_by_title(it, COLUMN_STATUS_TITLE).lower()
        tag = get_col_text_by_title(it, COLUMN_TAG_TITLE).strip().upper()
        last_notified = get_col_text_by_title(it, COLUMN_LAST_NOTIFIED_TITLE)

        # Stop logic: if status becomes Active, clear the marker and skip
        if status == "active":
            if last_notified:
                try:
                    set_text_column_by_title(item_id, COLUMN_LAST_NOTIFIED_TITLE, "")
                except Exception as e:
                    print(f"[WARN] Failed clearing last_notified for item {item_id}: {e}")
            continue

        # Only process Suspended items
        if status != "suspended":
            continue

        # Choose Slack channel (default to DL if anything else)
        webhook = SLACK_WEBHOOK_UCR if tag == "UCR" else SLACK_WEBHOOK_DL
        if not webhook:
            print(f"[WARN] No webhook configured for tag '{tag}' on item {item_id}; skipping.")
            continue

        if should_notify(last_notified, now, NOTIFY_INTERVAL_HOURS):
            link = universal_item_link(BOARD_ID, item_id)
            text = (
                f"⚠️ *Suspended Item*: *{name}*\n"
                f"🔗 {link}\n"
                f"⏱️ Reminders every {int(NOTIFY_INTERVAL_HOURS)}h until status changes to *Active*."
            )
            try:
                post_to_slack(webhook, text)
                set_text_column_by_title(item_id, COLUMN_LAST_NOTIFIED_TITLE, str(int(now)))
                print(f"[INFO] Notified for item {item_id} ({name}) to {('UCR' if webhook == SLACK_WEBHOOK_UCR else 'DL')}.")
            except Exception as e:
                print(f"[ERROR] Slack or Monday update failed for item {item_id}: {e}")


def background_loop():
    while True:
        try:
            process_cycle()
        except Exception as e:
            print("[ERROR] Uncaught in cycle:", e)
        time.sleep(POLL_SECONDS)


# ──────────────────────────────────────────────────────────────────────────────
# Flask endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return "OK"


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start background worker
    threading.Thread(target=background_loop, daemon=True).start()
    # Run web server (Render uses gunicorn in production)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))