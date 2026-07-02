"""
Odoo -> Discord digest notifier (embed edition)
Every run, posts ONE rich embed listing matching project tasks.

Matches tasks that are ALL of:
  - in project id 20260 ("(PRES-BU) Presales")
  - in the "New" stage
  - in the "Approved" task state (stored internally as "03_approved")

Each task line shows: clickable task name (no link preview), assignee(s),
and last-updated time rendered in each viewer's local timezone.

Runs on a schedule (GitHub Actions, every 4 hours).
"""

import os
import json
import xmlrpc.client
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------
# CONFIG — these come from GitHub Secrets. No real values go here.
# ---------------------------------------------------------------
ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

# ---------------------------------------------------------------
# EDIT THESE if things change in Odoo
# ---------------------------------------------------------------
MODEL = "project.task"
PROJECT_ID = 20260          # (PRES-BU) Presales
PROJECT_LABEL = "(PRES-BU) Presales"
STAGE_NAME = "New"
TASK_STATE = "03_approved"  # internal code for the "Approved" state

# ONLY_NEW = False -> every digest lists ALL tasks currently matching.
# ONLY_NEW = True  -> each task is only ever included in one digest.
ONLY_NEW = False

SEEN_FILE = "seen.txt"

EMBED_COLOR = 0x714B67          # Odoo purple; any hex color works
EMBED_DESC_LIMIT = 4000         # Discord allows 4096; leave headroom
TASKS_PER_EMBED = 25            # keep each embed readable


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(sorted(seen)))


def to_discord_timestamp(odoo_dt_string):
    """Odoo returns UTC datetimes like '2026-07-02 14:33:21'.
    Convert to Discord's <t:epoch:R> which renders as e.g. '2 hours ago'
    in every viewer's own timezone."""
    dt = datetime.strptime(odoo_dt_string, "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"


def send_embeds(embeds):
    """Discord allows up to 10 embeds per webhook message."""
    for i in range(0, len(embeds), 10):
        resp = requests.post(
            DISCORD_WEBHOOK,
            data=json.dumps({"embeds": embeds[i:i + 10]}),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()


def main():
    # --- 1. Log into Odoo ---
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise SystemExit("Odoo login failed — check ODOO_URL, DB, USER, API_KEY secrets")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    # --- 2. Find matching tasks, most recently updated first ---
    tasks = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        MODEL, "search_read",
        [[["project_id", "=", PROJECT_ID],
          ["stage_id.name", "=", STAGE_NAME],
          ["state", "=", TASK_STATE]]],
        {"fields": ["name", "user_ids", "write_date"],
         "order": "write_date desc"},
    )

    # --- 3. Optionally drop tasks already reported in a past digest ---
    seen = load_seen()
    if ONLY_NEW:
        tasks = [t for t in tasks if str(t["id"]) not in seen]

    if not tasks:
        print("No matching tasks this run — no message sent.")
        return

    # --- 4. Look up all assignee names in one call ---
    all_user_ids = sorted({u for t in tasks for u in t["user_ids"]})
    user_names = {}
    if all_user_ids:
        for u in models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.users", "read",
            [all_user_ids], {"fields": ["name"]},
        ):
            user_names[u["id"]] = u["name"]

    # --- 5. Build embed(s) ---
    # Each task renders as:
    #   **[Task name](link)**
    #   Assignee — updated 2 hours ago
    # The [name](url) form inside an embed is clickable but NEVER
    # produces a link preview.
    task_lines = []
    for t in tasks:
        assignee = ", ".join(user_names[u] for u in t["user_ids"]) or "Unassigned"
        updated = to_discord_timestamp(t["write_date"])
        link = f"{ODOO_URL}/odoo/project/task/{t['id']}"
        task_lines.append(f"**[{t['name']}]({link})**\n{assignee} — updated {updated}")

    embeds = []
    header = (f"**{len(tasks)} task{'s' if len(tasks) != 1 else ''}** "
              f"in **{STAGE_NAME}**, approved")

    # Pack tasks into embeds, respecting count and character limits
    batch, batch_len = [], 0
    for line in task_lines:
        if len(batch) >= TASKS_PER_EMBED or batch_len + len(line) > EMBED_DESC_LIMIT:
            embeds.append(batch)
            batch, batch_len = [], 0
        batch.append(line)
        batch_len += len(line) + 2
    if batch:
        embeds.append(batch)

    payload = []
    for i, batch in enumerate(embeds):
        embed = {
            "description": "\n\n".join(batch),
            "color": EMBED_COLOR,
        }
        if i == 0:  # title/header only on the first card
            embed["title"] = f"📋 {PROJECT_LABEL} — Approved in {STAGE_NAME}"
            embed["description"] = header + "\n\n" + embed["description"]
        payload.append(embed)

    send_embeds(payload)

    # --- 6. Remember what we reported (only matters if ONLY_NEW = True) ---
    seen.update(str(t["id"]) for t in tasks)
    save_seen(seen)

    print(f"Done. Digest sent with {len(tasks)} tasks in {len(payload)} embed(s).")


if __name__ == "__main__":
    main()
