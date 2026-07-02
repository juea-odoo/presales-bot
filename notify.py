"""
Odoo -> Discord digest notifier
Every run, posts ONE combined message listing matching project tasks.

Matches tasks that are ALL of:
  - in the "(PRES-BU) Presales" project
  - in the "New" stage
  - in the "Approved" task state (the green checkmark status)

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
# EDIT THESE if your names change in Odoo
# ---------------------------------------------------------------
MODEL = "project.task"
PROJECT_NAME = "(PRES-BU) Presales"  # exact project name as shown in Odoo
STAGE_NAME = "New"                   # exact stage (kanban column) name
TASK_STATE = "03_approved"           # internal code for the "Approved" state

# ONLY_NEW = False -> every digest lists ALL tasks currently matching
#                     (same task can appear in multiple digests until it
#                     leaves the stage/state).
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
    """Send one message. If it's too long for Discord, split into chunks
    at line boundaries so nothing gets cut mid-task."""
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

   # ---------- DIAGNOSTIC: what does Odoo actually have? ----------
    # 1. Find the project (fuzzy match, case-insensitive)
    projects = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "project.project", "search_read",
        [[["name", "ilike", "presales"]]],
        {"fields": ["id", "name"]},
    )
    print("Projects containing 'presales':", projects)

    if projects:
        pid = projects[0]["id"]

        # 2. List the stages that exist in that project
        stages = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "project.task.type", "search_read",
            [[["project_ids", "in", [pid]]]],
            {"fields": ["id", "name"]},
        )
        print("Stages in that project:", stages)

        # 3. Show the states of tasks currently in that project
        state_counts = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "project.task", "read_group",
            [[["project_id", "=", pid]], ["state"], ["state"]],
        )
        print("Task states in that project:", state_counts)
    # ---------- END DIAGNOSTIC ----------

    # --- 3. Optionally drop tasks already reported in a past digest ---
    seen = load_seen()
    if ONLY_NEW:
        tasks = [t for t in tasks if str(t["id"]) not in seen]

    if not tasks:
        print("No matching tasks this run — no message sent.")
        return

    # --- 4. Look up all assignee names in one call ---
    all_user_ids = sorted({uid_ for t in tasks for uid_ in t["user_ids"]})
    user_names = {}
    if all_user_ids:
        for u in models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.users", "read",
            [all_user_ids], {"fields": ["name"]},
        ):
            user_names[u["id"]] = u["name"]

    # --- 5. Build ONE digest message ---
    lines = [f"📋 **Approved tasks in New — {PROJECT_NAME}** ({len(tasks)} task{'s' if len(tasks) != 1 else ''})", ""]
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
