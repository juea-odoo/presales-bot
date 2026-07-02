"""
Odoo -> Discord digest notifier
Every run, posts ONE combined message listing matching project tasks.

Matches tasks that are ALL of:
  - in project id 20260  ("(PRES-BU) Presales " - note: name has a trailing
    space in the database, which is why we filter by ID, not name)
  - in the "New" stage
  - in the "Approved" task state (stored internally as "03_approved")

Runs on a schedule (GitHub Actions, every 4 hours).
"""

import os
import json
import xmlrpc.client
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
PROJECT_ID = 20260          # (PRES-BU) Presales — found via diagnostic
STAGE_NAME = "New"          # exact stage (kanban column) name
TASK_STATE = "03_approved"  # internal code for the "Approved" state

# ONLY_NEW = False -> every digest lists ALL tasks currently matching.
# ONLY_NEW = True  -> each task is only ever included in one digest.
ONLY_NEW = False

SEEN_FILE = "seen.txt"
DISCORD_CHAR_LIMIT = 1900  # Discord max is 2000; leave headroom


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(sorted(seen)))


def post_to_discord(text):
    """Send one message. If too long for Discord, split at line boundaries."""
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > DISCORD_CHAR_LIMIT:
            _send(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        _send(chunk)


def _send(content):
    resp = requests.post(
        DISCORD_WEBHOOK,
        data=json.dumps({"content": content}),
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

    def count(domain):
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
                                 MODEL, "search_count", [domain])

    # --- Diagnostics (safe to delete once everything works) ---
    stages = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "project.task.type", "search_read",
        [[["project_ids", "in", [PROJECT_ID]]]],
        {"fields": ["name"]},
    )
    print("Stages in project", PROJECT_ID, ":", [s["name"] for s in stages])
    print("Counts -> project:", count([["project_id", "=", PROJECT_ID]]),
          "| +stage:", count([["project_id", "=", PROJECT_ID],
                              ["stage_id.name", "=", STAGE_NAME]]),
          "| +approved:", count([["project_id", "=", PROJECT_ID],
                                 ["stage_id.name", "=", STAGE_NAME],
                                 ["state", "=", TASK_STATE]]))
    # --- End diagnostics ---

    # --- 2. Find tasks matching project + stage + approved state ---
    tasks = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        MODEL, "search_read",
        [[["project_id", "=", PROJECT_ID],
          ["stage_id.name", "=", STAGE_NAME],
          ["state", "=", TASK_STATE]]],
        {"fields": ["name", "partner_id", "user_ids"]},
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

    # --- 5. Build ONE digest message ---
    lines = [f"📋 **Approved tasks in New — (PRES-BU) Presales** "
             f"({len(tasks)} task{'s' if len(tasks) != 1 else ''})", ""]
    for t in tasks:
        assignee = ", ".join(user_names[u] for u in t["user_ids"]) or "Unassigned"
        customer = t["partner_id"][1] if t["partner_id"] else "N/A"
        link = f"{ODOO_URL}/odoo/project/task/{t['id']}"
        lines.append(f"• **{t['name']}** — {customer} — {assignee}")
        lines.append(f"  {link}")

    post_to_discord("\n".join(lines))

    # --- 6. Remember what we reported (only matters if ONLY_NEW = True) ---
    seen.update(str(t["id"]) for t in tasks)
    save_seen(seen)

    print(f"Done. Digest sent with {len(tasks)} tasks.")


if __name__ == "__main__":
    main()
