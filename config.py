import os
import json

DOMAIN = "jira.technica-engineering.net"
CONFIG_FILE = os.path.join(os.path.expanduser("~"), "scrum_config.json")
HOURS_PER_DAY = 8
WORK_START = 9
WORK_END = 17
REFRESH_INTERVAL = 300000  # ms -> 5 minutes; passed to app.after() for dashboard auto-refresh
ALERT_TIMES = ["11:00", "16:00"]  # daily logging-reminder notification times (24h "HH:MM")
# Fallback only: used when the app can't reach Jira yet (e.g. first ever
# launch, offline). Once it successfully talks to Jira, main.py discovers
# the REAL, current list of teams directly from ticket data via
# jira_engine.get_all_teams() and caches it as config["dynamic_teams_list"] -
# so this list no longer needs to be hand-edited when a team is renamed,
# added, or removed in Jira.
TEAMS_LIST = [
    "All Teams","CoBo_IPB","Climate","PowerMgt1", "Light_IPB","PowerMgt2", "CDL", "CA","Gateway", "Security", "SMACC", "SyFn"]

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError):
            return None
    return None

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error: config save failed: {e}")

def clear_config():
    if os.path.exists(CONFIG_FILE):
        try:
            os.remove(CONFIG_FILE)
        except OSError:
            pass
