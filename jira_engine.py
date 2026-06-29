import re
import json
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from config import DOMAIN, HOURS_PER_DAY, WORK_START

# ── field-id cache (avoids repeated /field API calls per session) ──────────
_field_cache = {}

def format_time(seconds):
    if not seconds or seconds <= 0: return "0m"
    total_minutes = int(seconds // 60)
    days    = total_minutes // (HOURS_PER_DAY * 60)
    rem_min = total_minutes %  (HOURS_PER_DAY * 60)
    hours   = rem_min // 60
    minutes = rem_min %  60
    parts = []
    if days    > 0: parts.append(f"{days}d")
    if hours   > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"

def get_user_identity(config, username=None):
    url = (f"https://{DOMAIN}/rest/api/2/myself" if not username
           else f"https://{DOMAIN}/rest/api/2/user/search?username={username}")
    try:
        res = requests.get(url, auth=HTTPBasicAuth(config["user"], config["token"]), timeout=10)
        denied_reason = res.headers.get("X-Authentication-Denied-Reason", "")
        if res.status_code == 403 or "CAPTCHA" in denied_reason:
            return {"error": "CAPTCHA_CHALLENGE", "reason": denied_reason}
        if res.status_code != 200:
            return None
        res_json = res.json()
        data = res_json[0] if isinstance(res_json, list) and res_json else res_json
        return {
            "accountId": data.get("accountId"),
            "email":     data.get("emailAddress"),
            "name":      data.get("displayName"),
            "key":       data.get("key"),
        }
    except Exception as e:
        print(f"[get_user_identity] Error: {e}")
        return None

def get_current_sprint(config):
    """
    Return the most recently started active sprint across all boards.
    Picking the sprint with the latest startDate avoids stale sprints on
    long-running boards from poisoning the expected-hours calculation.
    """
    try:
        boards_resp = requests.get(
            f"https://{DOMAIN}/rest/agile/1.0/board",
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=10
        ).json()
        best_sprint = None
        best_start  = None
        for board in boards_resp.get("values", []):
            try:
                res = requests.get(
                    f"https://{DOMAIN}/rest/agile/1.0/board/{board['id']}/sprint?state=active",
                    auth=HTTPBasicAuth(config["user"], config["token"]),
                    timeout=10
                ).json()
                for sprint in res.get("values", []):
                    raw_start = sprint.get("startDate")
                    if not raw_start:
                        continue
                    try:
                        start_dt = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    # Only consider sprints whose startDate is not in the future
                    now = datetime.now(start_dt.tzinfo)
                    if start_dt > now:
                        continue
                    if best_start is None or start_dt > best_start:
                        best_sprint = sprint
                        best_start  = start_dt
            except Exception as e:
                print(f"[get_current_sprint] Board {board.get('id')} error: {e}")
                continue
        return best_sprint
    except Exception as e:
        print(f"[get_current_sprint] Error: {e}")
    return None

def calculate_expected_hours(start_date):
    try:
        start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        now   = datetime.now(start.tzinfo)
        # Safety cap: sprints longer than 90 days are almost certainly a bad
        # startDate (wrong sprint returned, timezone issue, etc.). Clamp so the
        # UI never shows thousands of "missing" days.
        if (now - start).days > 90:
            print(f"[calculate_expected_hours] startDate {start_date!r} is >90 days ago — clamping to 0.")
            return 0
        total, current = 0, start
        while current.date() <= now.date():
            if current.weekday() < 5:
                d_start = current.replace(hour=WORK_START, minute=0, second=0)
                if current.date() < now.date():
                    total += HOURS_PER_DAY
                elif now > d_start:
                    total += min((now - d_start).total_seconds() / 3600, HOURS_PER_DAY)
            current += timedelta(days=1)
        return round(total, 2)
    except Exception as e:
        print(f"[calculate_expected_hours] Error: {e}")
        return 0

def calculate_expected_hours_j1(start_date):
    """Same as calculate_expected_hours but excludes today — only fully elapsed working days."""
    try:
        start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        now   = datetime.now(start.tzinfo)
        if (now - start).days > 90:
            print(f"[calculate_expected_hours_j1] startDate {start_date!r} is >90 days ago — clamping to 0.")
            return 0
        total, current = 0, start
        while current.date() < now.date():      # strict <: today excluded
            if current.weekday() < 5:
                total += HOURS_PER_DAY
            current += timedelta(days=1)
        return round(total, 2)
    except Exception as e:
        print(f"[calculate_expected_hours_j1] Error: {e}")
        return 0

def _get_all_fields(config):
    """Fetch and cache the full Jira field list. One call per process lifetime."""
    if "all_fields" not in _field_cache:
        try:
            fields = requests.get(
                f"https://{DOMAIN}/rest/api/2/field",
                auth=HTTPBasicAuth(config["user"], config["token"]),
                timeout=10
            ).json()
            _field_cache["all_fields"] = fields
        except Exception as e:
            print(f"[_get_all_fields] Error: {e}")
            return []
    return _field_cache["all_fields"]

def _get_field_id(config, field_name, cache_key, case_insensitive=False):
    """
    Generic Jira custom-field-id resolver. Checks the in-process cache first,
    then the persisted config (so we don't re-hit the /field endpoint across
    app restarts), then falls back to scanning the full field list.
    Replaces what used to be 5 separately copy-pasted lookup functions.
    """
    if cache_key in _field_cache:
        return _field_cache[cache_key]
    if cache_key in config:
        _field_cache[cache_key] = config[cache_key]
        return config[cache_key]
    target = field_name.lower() if case_insensitive else field_name
    for f in _get_all_fields(config):
        name = f.get("name", "")
        candidate = name.lower() if case_insensitive else name
        if candidate == target:
            fid = f.get("id")
            _field_cache[cache_key] = fid
            config[cache_key] = fid
            return fid
    return None

def get_checklist_field_id(config):
    """
    Resolve the custom field id backing the 'Checklist for Jira' (Okapya)
    app — visible in the issue view as a 'To Do List: X/Y resolved' panel.
    Tries the common display names first, then falls back to scanning all
    fields for one whose name suggests it's a checklist.
    """
    for candidate_name in ("To Do List", "Checklist"):
        fid = _get_field_id(config, candidate_name, "checklist_field_id", case_insensitive=True)
        if fid:
            return fid
    for f in _get_all_fields(config):
        name = f.get("name", "")
        if "checklist" in name.lower() or "to do" in name.lower() or "to-do" in name.lower():
            fid = f.get("id")
            _field_cache["checklist_field_id"] = fid
            config["checklist_field_id"] = fid
            return fid
    return None

def _parse_checklist_value(raw):
    """
    Normalize a 'Checklist for Jira' (Okapya) custom field value into a list
    of (text, is_done) tuples. The REST representation is documented as an
    array of checklist-item objects, but some plugin versions / Groovy paths
    hand back a JSON-encoded string instead, so both shapes are handled here.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("value") or []
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw:
        if isinstance(entry, dict):
            text = (entry.get("todo") or entry.get("name") or entry.get("text")
                    or entry.get("label") or entry.get("value") or "")
            # 'value' sometimes carries a leading checkbox glyph (e.g. '☐ description')
            text = re.sub(r'^[☐☑✓✔□\s]+', '', str(text)).strip()
            checked = entry.get("done")
            if checked is None:
                checked = entry.get("checked", False)
            if isinstance(checked, str):
                checked = checked.strip().lower() == "true"
            if not text:
                fallback_parts = [f"{k}={v}" for k, v in entry.items()
                                   if k != "checked" and isinstance(v, (str, int, float, bool))]
                text = "RAW: " + (", ".join(fallback_parts) if fallback_parts else str(entry))
            items.append((text, bool(checked)))
        elif isinstance(entry, str):
            items.append((entry.strip(), False))
    return items

def get_ticket_checklist(config, ticket_key):
    """
    Fetch a single ticket's 'Checklist for Jira' (Okapya) custom field value
    - the 'To Do List: X/Y resolved' checkbox list seen in the issue view -
    and normalize it into (text, is_done) tuples.

    Returns: (items, resolved_count, total_count, error)
        error is None on success, else a short user-facing message.
    """
    field_id = get_checklist_field_id(config)
    if not field_id:
        return [], 0, 0, "No checklist field found on this Jira instance (looked for 'To Do List' / 'Checklist')."
    try:
        res = requests.get(
            f"https://{DOMAIN}/rest/api/2/issue/{ticket_key}",
            params={"fields": field_id},
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=10
        )
        if res.status_code != 200:
            return [], 0, 0, f"Could not load ticket ({res.status_code})."
        raw = res.json().get("fields", {}).get(field_id)
        print(f"[get_ticket_checklist] {ticket_key}: field_id={field_id}, raw_type={type(raw).__name__}, raw_preview={str(raw)[:200]!r}")
        items = _parse_checklist_value(raw)
        resolved = sum(1 for _, done in items if done)
        return items, resolved, len(items), None
    except Exception as e:
        print(f"[get_ticket_checklist] {ticket_key}: {e}")
        return [], 0, 0, "Error loading checklist."

def get_team_field_name(config):
    return _get_field_id(config, "team", "team_field_id", case_insensitive=True)

def get_custom_labels_field_name(config):
    return _get_field_id(config, "Custom Labels", "custom_labels_field_id")

def get_validation_name_field_name(config):
    return _get_field_id(config, "Validation Name", "validation_name_field_id")

def get_achieved_tcs_field_id(config):
    return _get_field_id(config, "Achieved TCs", "achieved_tcs_field_id")

def get_achieved_reqs_field_id(config):
    return _get_field_id(config, "Achieved Reqs", "achieved_reqs_field_id")

def get_achieved_tickets_field_id(config):
    return _get_field_id(config, "Achieved Tickets", "achieved_tickets_field_id")

# ── team-name matching ─────────────────────────────────────────────────────

def _team_matches(team_name, field_value, ticket_key):
    """
    Return True if ticket belongs to team_name.
    Uses exact/word-boundary field match first, then project-key prefix fallback.
    Avoids substring false positives (e.g. 'CA' inside 'Capacity').
    """
    clean = team_name.strip()
    # 1. Field value match
    if field_value:
        fv = str(field_value.get("name", field_value.get("value", ""))
                 if isinstance(field_value, dict) else field_value)
        if fv.strip().lower() == clean.lower():
            return True
        if re.search(
            r'(?<![A-Za-z0-9_])' + re.escape(clean) + r'(?![A-Za-z0-9_])',
            fv, re.IGNORECASE
        ):
            return True
    # 2. Key-prefix fallback  (e.g. "CA" → "CA-123")
    prefix = clean.lower().replace(" team", "").strip()
    if ticket_key.upper().startswith(f"{prefix.upper()}-"):
        return True
    return False

# ── shared helpers for extracting custom field values ─────────────────────

def _extract_field_value(raw):
    if not raw:
        return "N/A"
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first.get("value", first.get("name", "N/A")) if isinstance(first, dict) else str(first)
    if isinstance(raw, dict):
        return raw.get("value", raw.get("name", "N/A"))
    return str(raw)

def _int_field(fields, fid):
    """Safely coerce a Jira custom-field value to int. fid may be None
    (field not configured on this instance) — returns 0 in that case."""
    if not fid:
        return 0
    val = fields.get(fid)
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0

# ── public data-fetch functions ────────────────────────────────────────────

def get_team_member_logging(config, team_name):
    """
    For each assignee with tickets in team_name, sum the seconds they personally
    logged across all team tickets in the open sprint.
    Returns: {display_name: logged_seconds}  or  {} on failure.
    """
    team_field_id = get_team_field_name(config)

    jql    = "sprint in openSprints() AND issuetype = 'Task'AND project = SytProjectMgt"
    fields = "summary,worklog,assignee"
    if team_field_id: fields += f",{team_field_id}"

    try:
        all_issues, start_at, page = [], 0, 100
        while True:
            resp = requests.get(
                f"https://{DOMAIN}/rest/api/2/search",
                params={"jql": jql, "fields": fields, "maxResults": page, "startAt": start_at},
                auth=HTTPBasicAuth(config["user"], config["token"]),
                timeout=20
            ).json()
            batch = resp.get("issues", [])
            all_issues.extend(batch)
            if len(batch) < page: break
            start_at += page

        # member_logged: { display_name -> seconds }
        member_logged = {}

        for issue in all_issues:
            key, f = issue["key"], issue["fields"]
            if not _team_matches(team_name, f.get(team_field_id) if team_field_id else None, key):
                continue

            assignee = f.get("assignee") or {}
            owner = (assignee.get("displayName") or assignee.get("name")
                     or assignee.get("emailAddress") or "Unassigned")

            # get full worklog list if truncated
            w_data = f.get("worklog", {})
            if w_data.get("total", 0) > len(w_data.get("worklogs", [])):
                try:
                    full = requests.get(
                        f"https://{DOMAIN}/rest/api/2/issue/{key}/worklog",
                        auth=HTTPBasicAuth(config["user"], config["token"]),
                        timeout=10
                    ).json()
                    worklogs = full.get("worklogs", [])
                except Exception as e:
                    print(f"[worklog fetch] {key}: {e}")
                    worklogs = w_data.get("worklogs", [])
            else:
                worklogs = w_data.get("worklogs", [])

            # credit each log entry to its author (not necessarily the assignee)
            for log in worklogs:
                author = log.get("author", {})
                author_name = (author.get("displayName") or author.get("name")
                               or author.get("emailAddress") or owner)
                seconds = log.get("timeSpentSeconds", 0)
                member_logged[author_name] = member_logged.get(author_name, 0) + seconds

            # ensure the assignee appears even with 0 logged
            if owner not in member_logged:
                member_logged[owner] = 0

        return member_logged

    except Exception as e:
        print(f"[get_team_member_logging] Error: {e}")
        return {}


def get_all_teams(config):
    """
    Discover the full set of distinct 'Team' field values actually used on
    Jira tickets right now, instead of relying on a manually maintained,
    hardcoded list (config.TEAMS_LIST) that silently drifts out of sync the
    moment a team is renamed, added, or removed in Jira — giving zero
    results for that team until someone notices and corrects the code.

    Scope: tickets in the currently open sprint (same scope used everywhere
    else in this app), so the list always reflects teams that are actually
    active right now, and automatically includes any newly-introduced team
    name the very next time it's called — no code change required.

    Returns: sorted list of team names with "All Teams" always first.
             Falls back to ["All Teams"] only if the Team field itself
             isn't configured on this Jira instance, or the request fails.
    """
    team_field_id = get_team_field_name(config)
    if not team_field_id:
        return ["All Teams"]

    jql = "sprint in openSprints() AND issuetype = 'Task' AND project = SytProjectMgt"

    try:
        teams_found = set()
        all_issues, start_at, page = [], 0, 100
        while True:
            resp = requests.get(
                f"https://{DOMAIN}/rest/api/2/search",
                params={"jql": jql, "fields": team_field_id, "maxResults": page, "startAt": start_at},
                auth=HTTPBasicAuth(config["user"], config["token"]),
                timeout=20
            ).json()
            batch = resp.get("issues", [])
            all_issues.extend(batch)
            if len(batch) < page:
                break
            start_at += page

        for issue in all_issues:
            raw = issue.get("fields", {}).get(team_field_id)
            value = _extract_field_value(raw)  # handles plain string / dict / list shapes
            if value and value != "N/A":
                teams_found.add(value.strip())

        # Sort case-insensitively so e.g. "cdl" and "CDL" don't end up far apart.
        return ["All Teams"] + sorted(teams_found, key=str.lower)
    except Exception as e:
        print(f"[get_all_teams] Error: {e}")
        return ["All Teams"]


def get_team_overview_data(config, team_name):
    """
    Fetch all open-sprint tickets for team_name directly from Jira.
    No predefined member list — assignees discovered dynamically.
    Returns: (data, totals, total_logged) or (None, None, None) on failure.
    """
    team_field_id        = get_team_field_name(config)
    custom_labels_id     = get_custom_labels_field_name(config)
    validation_name_id   = get_validation_name_field_name(config)
    achieved_tcs_id      = get_achieved_tcs_field_id(config)
    achieved_reqs_id     = get_achieved_reqs_field_id(config)
    achieved_tickets_id  = get_achieved_tickets_field_id(config)

    jql = "sprint in openSprints() AND issuetype = 'Task' AND project = SytProjectMgt"
    fields = "summary,timeoriginalestimate,timeestimate,status,worklog,assignee"
    if team_field_id:       fields += f",{team_field_id}"
    if custom_labels_id:    fields += f",{custom_labels_id}"
    if validation_name_id:  fields += f",{validation_name_id}"
    if achieved_tcs_id:     fields += f",{achieved_tcs_id}"
    if achieved_reqs_id:    fields += f",{achieved_reqs_id}"
    if achieved_tickets_id: fields += f",{achieved_tickets_id}"

    try:
        all_issues, start_at, page = [], 0, 100
        while True:
            resp = requests.get(
                f"https://{DOMAIN}/rest/api/2/search",
                params={"jql": jql, "fields": fields, "maxResults": page, "startAt": start_at},
                auth=HTTPBasicAuth(config["user"], config["token"]),
                timeout=20
            ).json()
            batch = resp.get("issues", [])
            all_issues.extend(batch)
            if len(batch) < page:
                break
            start_at += page

        data   = {"todo": [], "in_progress": [], "approved": [], "blocked": [], "partially_blocked": [], "done": []}
        totals = {"todo": 0,  "in_progress": 0,  "approved": 0,  "blocked": 0,  "partially_blocked": 0,  "done": 0}
        total_logged = 0

        for issue in all_issues:
            key, f = issue["key"], issue["fields"]

            if not _team_matches(team_name, f.get(team_field_id) if team_field_id else None, key):
                continue

            # Assignee
            assignee = f.get("assignee") or {}
            owner = (assignee.get("displayName") or assignee.get("name")
                     or assignee.get("emailAddress") or "Unassigned")

            # Worklogs — Jira returns max 20 inline; fetch full list if truncated
            w_data = f.get("worklog", {})
            if w_data.get("total", 0) > len(w_data.get("worklogs", [])):
                try:
                    full = requests.get(
                        f"https://{DOMAIN}/rest/api/2/issue/{key}/worklog",
                        auth=HTTPBasicAuth(config["user"], config["token"]),
                        timeout=10
                    ).json()
                    worklogs = full.get("worklogs", [])
                except Exception as e:
                    print(f"[worklog fetch] {key}: {e}")
                    worklogs = w_data.get("worklogs", [])
            else:
                worklogs = w_data.get("worklogs", [])

            spent = sum(log.get("timeSpentSeconds", 0) for log in worklogs)
            total_logged += spent

            rem = (f.get("timeestimate") if f.get("timeestimate") is not None
                   else max((f.get("timeoriginalestimate") or 0) - spent, 0))

            est_time = f.get("timeoriginalestimate", 0)
            est_time_formatted = format_time(est_time) if est_time else "0h"

            status_obj = f.get("status", {})
            st_name = status_obj.get("name", "")
            st_cat  = status_obj.get("statusCategory", {}).get("key", "")

            activity_label   = _extract_field_value(f.get(custom_labels_id)   if custom_labels_id   else None)
            validation_label = _extract_field_value(f.get(validation_name_id) if validation_name_id else None)

            achieved_tcs     = _int_field(f, achieved_tcs_id)
            achieved_reqs    = _int_field(f, achieved_reqs_id)
            achieved_tickets = _int_field(f, achieved_tickets_id)

            ticket = (key, f["summary"], f"https://{DOMAIN}/browse/{key}",
                      format_time(spent), format_time(rem),
                      activity_label, validation_label, owner,
                      est_time_formatted,
                      achieved_tcs, achieved_reqs, achieved_tickets)

            if st_name == "Approved":
                data["approved"].append(ticket); totals["approved"] += spent
            elif st_name == "Blocked":
                data["blocked"].append(ticket);  totals["blocked"]  += spent
            elif st_name == "Partially Blocked":
                data["partially_blocked"].append(ticket); totals["partially_blocked"] += spent
            elif st_cat == "done":
                data["done"].append(ticket);     totals["done"]     += spent
            elif st_cat == "indeterminate":
                data["in_progress"].append(ticket); totals["in_progress"] += spent
            else:
                data["todo"].append(ticket);     totals["todo"]     += spent

        return data, totals, total_logged

    except Exception as e:
        print(f"[get_team_overview_data] Error: {e}")
        return None, None, None


def get_jira_data(config, target_username=None, force_team_filter=None):
    identity = get_user_identity(config, target_username)
    if not identity or "error" in identity:
        return None, None, None, False

    search_user = f"'{target_username}'" if target_username else "currentUser()"
    jql = f"assignee={search_user} AND sprint in openSprints()"
    if target_username:
        jql += " AND issuetype = 'Task'AND project = SytProjectMgt"

    team_field_id        = get_team_field_name(config)
    custom_labels_id     = get_custom_labels_field_name(config)
    validation_name_id   = get_validation_name_field_name(config)
    achieved_tcs_id      = get_achieved_tcs_field_id(config)
    achieved_reqs_id     = get_achieved_reqs_field_id(config)
    achieved_tickets_id  = get_achieved_tickets_field_id(config)

    fields = "summary,timeoriginalestimate,timeestimate,status,worklog"
    if team_field_id:       fields += f",{team_field_id}"
    if custom_labels_id:    fields += f",{custom_labels_id}"
    if validation_name_id:  fields += f",{validation_name_id}"
    if achieved_tcs_id:     fields += f",{achieved_tcs_id}"
    if achieved_reqs_id:    fields += f",{achieved_reqs_id}"
    if achieved_tickets_id: fields += f",{achieved_tickets_id}"

    try:
        response = requests.get(
            f"https://{DOMAIN}/rest/api/2/search",
            params={"jql": jql, "fields": fields, "maxResults": 100},
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=15
        ).json()

        data   = {"todo": [], "in_progress": [], "approved": [], "blocked": [], "partially_blocked": [], "done": []}
        totals = {"todo": 0,  "in_progress": 0,  "approved": 0,  "blocked": 0,  "partially_blocked": 0,  "done": 0}
        total_logged = 0

        for issue in response.get("issues", []):
            key, f = issue["key"], issue["fields"]

            # Team filter
            if force_team_filter and force_team_filter != "All Teams":
                if not _team_matches(force_team_filter,
                                     f.get(team_field_id) if team_field_id else None,
                                     key):
                    continue

            # Worklogs — only count this user's time
            w_data   = f.get("worklog", {})
            if w_data.get("total", 0) > len(w_data.get("worklogs", [])):
                try:
                    full = requests.get(
                        f"https://{DOMAIN}/rest/api/2/issue/{key}/worklog",
                        auth=HTTPBasicAuth(config["user"], config["token"]),
                        timeout=10
                    ).json()
                    worklogs = full.get("worklogs", [])
                except Exception as e:
                    print(f"[worklog fetch] {key}: {e}")
                    worklogs = w_data.get("worklogs", [])
            else:
                worklogs = w_data.get("worklogs", [])

            spent = 0
            for log in worklogs:
                author = log.get("author", {})
                if ((identity.get("accountId") and author.get("accountId") == identity["accountId"])
                        or (identity.get("email") and author.get("emailAddress") == identity["email"])
                        or (identity.get("name")  and author.get("displayName") == identity["name"])
                        or (target_username and author.get("name") == target_username)):
                    spent += log.get("timeSpentSeconds", 0)

            total_logged += spent
            rem = (f.get("timeestimate") if f.get("timeestimate") is not None
                   else max((f.get("timeoriginalestimate") or 0) - spent, 0))

            est_time = f.get("timeoriginalestimate", 0)
            est_time_formatted = format_time(est_time) if est_time else "0h"

            status_obj = f.get("status", {})
            st_name = status_obj.get("name", "")
            st_cat  = status_obj.get("statusCategory", {}).get("key", "")

            activity_label   = _extract_field_value(f.get(custom_labels_id)   if custom_labels_id   else None)
            validation_label = _extract_field_value(f.get(validation_name_id) if validation_name_id else None)

            achieved_tcs     = _int_field(f, achieved_tcs_id)
            achieved_reqs    = _int_field(f, achieved_reqs_id)
            achieved_tickets = _int_field(f, achieved_tickets_id)

            ticket = (key, f["summary"], f"https://{DOMAIN}/browse/{key}",
                      format_time(spent), format_time(rem),
                      activity_label, validation_label,
                      est_time_formatted,
                      achieved_tcs, achieved_reqs, achieved_tickets)

            if st_name == "Approved":
                data["approved"].append(ticket); totals["approved"] += spent
            elif st_name == "Blocked":
                data["blocked"].append(ticket);  totals["blocked"]  += spent
            elif st_name == "Partially Blocked":
                data["partially_blocked"].append(ticket); totals["partially_blocked"] += spent
            elif st_cat == "done":
                data["done"].append(ticket);     totals["done"]     += spent
            elif st_cat == "indeterminate":
                data["in_progress"].append(ticket); totals["in_progress"] += spent
            else:
                data["todo"].append(ticket);     totals["todo"]     += spent

        total_tickets = sum(len(data[cat]) for cat in data)
        return data, totals, total_logged, total_tickets > 0

    except Exception as e:
        print(f"[get_jira_data] Error: {e}")
        return None, None, None, False