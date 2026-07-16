import re
import json
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from config import DOMAIN, HOURS_PER_DAY, WORK_START

# ── field-id cache (avoids repeated /field API calls per session) ──────────
_field_cache = {}

def _fetch_full_worklogs(config, key):
    """Fetch the complete worklog list for a single issue. Used when Jira's
    inline worklog payload is truncated (it only returns the first 20)."""
    try:
        full = requests.get(
            f"https://{DOMAIN}/rest/api/2/issue/{key}/worklog",
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=10
        ).json()
        return key, full.get("worklogs", [])
    except Exception as e:
        print(f"[worklog fetch] {key}: {e}")
        return key, None

def _resolve_worklogs(config, issues_needing_fetch):
    """
    Concurrently fetch full worklogs for every issue whose inline payload was
    truncated, instead of one blocking request per ticket in a sequential
    loop. Returns {issue_key: worklogs_list}. Falls back silently (caller
    keeps the truncated inline list) for any key not present in the result.
    """
    results = {}
    if not issues_needing_fetch:
        return results
    with ThreadPoolExecutor(max_workers=10) as pool:
        for key, worklogs in pool.map(lambda k: _fetch_full_worklogs(config, k), issues_needing_fetch):
            if worklogs is not None:
                results[key] = worklogs
    return results

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

def get_available_projects(config):
    """
    Fetch every Jira project this account can see, for the project-selection
    step shown at login (and reachable later from dashboard settings).
    Returns a list of {"key": ..., "name": ...} dicts sorted by name, or []
    on failure (caller should treat that as "skip project selection").
    """
    try:
        res = requests.get(
            f"https://{DOMAIN}/rest/api/2/project",
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=15
        )
        if res.status_code != 200:
            return []
        projects = res.json()
        out = [{"key": p.get("key"), "name": p.get("name") or p.get("key")}
               for p in projects if p.get("key")]
        return sorted(out, key=lambda p: p["name"].lower())
    except Exception as e:
        print(f"[get_available_projects] Error: {e}")
        return []


# Preserves old behavior for configs saved before project selection existed.
_DEFAULT_PROJECT_KEY = "SytProjectMgt"


def _project_clause(config):
    """
    Build the JQL project filter from the project(s) the user picked at
    login / in Settings, instead of the single project that used to be
    hardcoded into every query here. Scoping to only the project(s) someone
    actually works on means Jira has far fewer tickets to search per
    request.
    """
    projects = config.get("selected_projects") or [_DEFAULT_PROJECT_KEY]
    if len(projects) == 1:
        return f"project = {projects[0]}"
    return "project in (%s)" % ", ".join(projects)


def get_available_te_projects(config):
    """
    Discover the distinct 'TE-Project' (sub-project) values present on
    tickets within the currently selected project(s) right now — 'TE-Project'
    is a custom field some Jira projects use to further divide a project
    into sub-projects. Shown as a second selection step right after picking
    Project(s) at login (and reachable later from Settings).

    Scope: tickets in the currently open sprint within the already-selected
    project(s) — same scope used by get_all_teams — so the list only shows
    sub-projects actually in use right now, and needs no code change when a
    new one appears.

    Returns: sorted list of TE-Project values, or [] if the field isn't
    configured on this Jira instance, nothing is found, or the request
    fails — callers should treat that as "skip this selection step".
    """
    field_id = get_te_project_field_id(config)
    if not field_id:
        return []

    jql = f"sprint in openSprints() AND {_project_clause(config)}"
    try:
        values_found = set()
        all_issues, start_at, page = [], 0, 100
        while True:
            resp = requests.get(
                f"https://{DOMAIN}/rest/api/2/search",
                params={"jql": jql, "fields": field_id, "maxResults": page, "startAt": start_at},
                auth=HTTPBasicAuth(config["user"], config["token"]),
                timeout=20
            ).json()
            batch = resp.get("issues", [])
            all_issues.extend(batch)
            if len(batch) < page:
                break
            start_at += page

        for issue in all_issues:
            raw = issue.get("fields", {}).get(field_id)
            value = _extract_field_value(raw)
            if value and value != "N/A":
                values_found.add(value.strip())

        return sorted(values_found, key=str.lower)
    except Exception as e:
        print(f"[get_available_te_projects] Error: {e}")
        return []


def _te_project_clause(config):
    """
    Optional JQL filter for the 'TE-Project' sub-project field, layered on
    top of _project_clause() — narrowing by TE-Project shrinks the ticket
    set further, on top of the project-level scope. Unlike project, this is
    optional: an empty/absent selection means "all sub-projects" and adds
    no filter at all, so behavior for configs saved before this feature
    existed (or users who skip it) is unaffected.
    """
    values = config.get("selected_te_projects")
    if not values:
        return ""
    quoted = ", ".join(f'"{v}"' for v in values)
    return f' AND "TE-Project" in ({quoted})'


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
    Board sprint lookups run concurrently — sequential per-board requests
    were a slow point on instances with many boards.
    """
    try:
        boards_resp = requests.get(
            f"https://{DOMAIN}/rest/agile/1.0/board",
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=10
        ).json()
        board_ids = [b["id"] for b in boards_resp.get("values", [])]
        if not board_ids:
            return None

        def _fetch_board_sprints(board_id):
            try:
                res = requests.get(
                    f"https://{DOMAIN}/rest/agile/1.0/board/{board_id}/sprint?state=active",
                    auth=HTTPBasicAuth(config["user"], config["token"]),
                    timeout=10
                ).json()
                return res.get("values", [])
            except Exception as e:
                print(f"[get_current_sprint] Board {board_id} error: {e}")
                return []

        best_sprint = None
        best_start  = None
        with ThreadPoolExecutor(max_workers=10) as pool:
            for sprints in pool.map(_fetch_board_sprints, board_ids):
                for sprint in sprints:
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
        return best_sprint
    except Exception as e:
        print(f"[get_current_sprint] Error: {e}")
    return None


def get_all_open_sprints(config):
    """
    Return ALL currently active sprints across every board, sorted by
    startDate descending (newest first) — so the most recent one appears
    at the top of the picker dropdown.

    Each entry is the raw Jira sprint dict, which includes at minimum:
        id, name, startDate, endDate, state, originBoardId

    This is used by the sprint-picker dropdown in the UI so the user can
    manually select the correct sprint when Jira has multiple open at once
    (e.g. a new sprint started while the old one wasn't closed yet).

    Returns an empty list if the request fails or no active sprints exist.
    """
    try:
        boards_resp = requests.get(
            f"https://{DOMAIN}/rest/agile/1.0/board",
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=10
        ).json()
        board_ids = [b["id"] for b in boards_resp.get("values", [])]
        if not board_ids:
            return []

        def _fetch_sprints(board_id):
            try:
                res = requests.get(
                    f"https://{DOMAIN}/rest/agile/1.0/board/{board_id}/sprint?state=active",
                    auth=HTTPBasicAuth(config["user"], config["token"]),
                    timeout=10
                ).json()
                return res.get("values", [])
            except Exception:
                return []

        seen_ids = set()    # De-duplicate: the same sprint can appear on multiple boards.
        all_sprints = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            for sprints in pool.map(_fetch_sprints, board_ids):
                for sprint in sprints:
                    sid = sprint.get("id")
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        all_sprints.append(sprint)

        # Sort newest-first so the most relevant sprint is at the top.
        def _start_key(s):
            try:
                return datetime.fromisoformat(s.get("startDate", "").replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        all_sprints.sort(key=_start_key, reverse=True)
        return all_sprints

    except Exception as e:
        print(f"[get_all_open_sprints] Error: {e}")
        return []


def _get_project_board_ids(config):
    """
    Return board IDs scoped to the currently selected project(s)
    (config['selected_projects']), via the Agile API's projectKeyOrId
    filter — instead of every board on the Jira server. jira.technica-
    engineering.net is a company-wide instance, so an unscoped board list
    is dominated by other teams' boards that have nothing to do with this
    app's project(s). Paginated the same way board/sprint listings
    normally are (startAt/isLast).
    """
    projects = config.get("selected_projects") or [_DEFAULT_PROJECT_KEY]
    board_ids = set()
    for proj in projects:
        start_at = 0
        while True:
            try:
                resp = requests.get(
                    f"https://{DOMAIN}/rest/agile/1.0/board",
                    params={"projectKeyOrId": proj, "startAt": start_at, "maxResults": 50},
                    auth=HTTPBasicAuth(config["user"], config["token"]),
                    timeout=10
                ).json()
            except Exception as e:
                print(f"[_get_project_board_ids] {proj}: {e}")
                break
            values = resp.get("values", [])
            board_ids.update(b["id"] for b in values if b.get("id") is not None)
            if resp.get("isLast", True) or not values:
                break
            start_at += len(values)
    return list(board_ids)


def get_last_n_sprints(config, n=5):
    """
    Return the n most recently started sprints — active, closed, or future
    — across every board belonging to the selected project(s), sorted
    newest-first by startDate. Unlike get_all_open_sprints (which only
    looks at currently-active sprints), this includes recently-closed
    sprints too, since a ticket often lingers a sprint or two behind
    before it's marked Closed/Approved.

    Used to scope the "Open Tickets" search to recent history (JQL
    "sprint in (id1, id2, ...)") instead of every sprint a ticket has ever
    been in, which is what made that search slow.

    Boards are scoped to config['selected_projects'] (_get_project_board_ids,
    same scope as _project_clause) rather than every board on the server —
    an unscoped "n most recent sprints" on a company-wide instance is
    almost always sprints from unrelated teams, which starves the Open
    Tickets JQL of the sprint IDs it actually needed and silently drops
    this project's own last sprints from the results.

    Each entry is the raw Jira sprint dict (id, name, startDate, endDate,
    state, originBoardId, ...). Returns an empty list if the request fails
    or no boards/sprints are found.
    """
    try:
        board_ids = _get_project_board_ids(config)
        if not board_ids:
            return []

        def _fetch_sprints(board_id):
            try:
                res = requests.get(
                    f"https://{DOMAIN}/rest/agile/1.0/board/{board_id}/sprint",
                    params={"state": "active,closed,future"},
                    auth=HTTPBasicAuth(config["user"], config["token"]),
                    timeout=10
                ).json()
                return res.get("values", [])
            except Exception:
                return []

        seen_ids = set()    # De-duplicate: the same sprint can appear on multiple boards.
        all_sprints = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            for sprints in pool.map(_fetch_sprints, board_ids):
                for sprint in sprints:
                    sid = sprint.get("id")
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        all_sprints.append(sprint)

        def _start_key(s):
            try:
                return datetime.fromisoformat(s.get("startDate", "").replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        # Only sprints that have actually started — future sprints have no
        # meaningful "recency" and would sort unpredictably otherwise.
        started = [s for s in all_sprints if s.get("startDate")]
        started.sort(key=_start_key, reverse=True)
        return started[:n]

    except Exception as e:
        print(f"[get_last_n_sprints] Error: {e}")
        return []


def get_closed_sprints(config, n=10):
    """
    Return the n most recently CLOSED sprints (state == 'closed' only —
    unlike get_last_n_sprints, which also includes active/future sprints)
    across every board, sorted newest-first by startDate.

    Used by the History picker: only fully-closed sprints make sense as
    "history" to look back at.
    """
    try:
        boards_resp = requests.get(
            f"https://{DOMAIN}/rest/agile/1.0/board",
            auth=HTTPBasicAuth(config["user"], config["token"]),
            timeout=10
        ).json()
        board_ids = [b["id"] for b in boards_resp.get("values", [])]
        if not board_ids:
            return []

        def _fetch_sprints(board_id):
            try:
                res = requests.get(
                    f"https://{DOMAIN}/rest/agile/1.0/board/{board_id}/sprint",
                    params={"state": "closed"},
                    auth=HTTPBasicAuth(config["user"], config["token"]),
                    timeout=10
                ).json()
                return res.get("values", [])
            except Exception:
                return []

        seen_ids = set()
        all_sprints = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            for sprints in pool.map(_fetch_sprints, board_ids):
                for sprint in sprints:
                    sid = sprint.get("id")
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        all_sprints.append(sprint)

        def _start_key(s):
            try:
                return datetime.fromisoformat(s.get("startDate", "").replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        started = [s for s in all_sprints if s.get("startDate")]
        started.sort(key=_start_key, reverse=True)
        return started[:n]

    except Exception as e:
        print(f"[get_closed_sprints] Error: {e}")
        return []


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

def calculate_expected_hours_for_sprint(start_date, end_date):
    """
    Full-duration expected hours across an entire sprint — every working
    day from start up to (but NOT including) end, at HOURS_PER_DAY each.

    calculate_expected_hours / calculate_expected_hours_j1 measure elapsed
    time up to "now", which is the right question for the currently active
    sprint but the wrong one for a closed sprint being viewed in History:
    "now" is long past the sprint's end, so those would count extra days
    that were never part of it. This instead treats the whole sprint span
    as elapsed, which is the correct comparison once a sprint has closed.

    end_date is treated as EXCLUSIVE: Jira sprint boundaries are typically
    back-to-back — a sprint's endDate is usually the exact same instant as
    the next sprint's startDate — so that boundary day belongs to the next
    sprint, not this one. Counting it here would silently add one extra
    day (today, if this sprint happens to end today) to a supposedly-closed
    sprint's expected hours.
    """
    try:
        start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end   = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        total, current = 0, start
        while current.date() < end.date():   # end date EXCLUSIVE
            if current.weekday() < 5:
                total += HOURS_PER_DAY
            current += timedelta(days=1)
        return round(total, 2)
    except Exception as e:
        print(f"[calculate_expected_hours_for_sprint] Error: {e}")
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

def get_te_project_field_id(config):
    return _get_field_id(config, "TE-Project", "te_project_field_id", case_insensitive=True)

def get_sprint_field_id(config):
    return _get_field_id(config, "Sprint", "sprint_field_id", case_insensitive=True)

_SPRINT_STR_NAME_RE = re.compile(r'name=([^,\]]+)')

def _extract_sprint_name(raw):
    """
    Normalize the 'Sprint' custom field into a single sprint name. A ticket
    can carry a history of sprints (moved from one to another), so the last
    entry — the most recent/current sprint — is used to represent it.
    Handles both the modern dict-list shape ([{id, name, state, ...}, ...])
    and the older greenhopper string shape
    ('com.atlassian.greenhopper...[id=1,...,name=Sprint 5,...]').
    Returns "No Sprint" if the field is empty/unset.
    """
    if not raw:
        return "No Sprint"
    if not isinstance(raw, list):
        raw = [raw]
    last = raw[-1]
    if isinstance(last, dict):
        return last.get("name") or "No Sprint"
    if isinstance(last, str):
        m = _SPRINT_STR_NAME_RE.search(last)
        if m:
            return m.group(1).strip()
        return last
    return "No Sprint"

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

def get_team_overview_and_logging(config, team_name, sprint_id=None):
    """
    Single combined fetch that replaces what used to be two separate calls
    (get_team_overview_data + get_team_member_logging), each running its own
    full JQL search + pagination + worklog-resolve pass over essentially the
    same ticket set. Doing it once here halves the Jira search load and the
    number of worklog-refetch requests needed per team-overview load.

    sprint_id: when given, scopes to that exact sprint ("sprint = {id}")
    instead of whatever sprint(s) are currently open — used by the History
    feature to pull a past, closed sprint's data instead of live data.

    Returns: (data, totals, total_logged, member_logged) or
             (None, None, None, {}) on failure.
    """
    team_field_id        = get_team_field_name(config)
    custom_labels_id     = get_custom_labels_field_name(config)
    validation_name_id   = get_validation_name_field_name(config)
    achieved_tcs_id      = get_achieved_tcs_field_id(config)
    achieved_reqs_id     = get_achieved_reqs_field_id(config)
    achieved_tickets_id  = get_achieved_tickets_field_id(config)

    sprint_clause = f"sprint = {sprint_id}" if sprint_id else "sprint in openSprints()"
    jql = f"{sprint_clause} AND issuetype = 'Task' AND {_project_clause(config)}{_te_project_clause(config)}"
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
        total_logged  = 0
        member_logged = {}
        seen_keys = set()  # guard against duplicate issues (multiple open sprints)

        # Pass 1: figure out which (already team-matched, deduped) issues need
        # a full worklog refetch, then fetch them all CONCURRENTLY instead of
        # one-by-one — this is the main source of slowness on larger sprints.
        relevant_issues = []
        needs_fetch = []
        for issue in all_issues:
            key, f = issue["key"], issue["fields"]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if not _team_matches(team_name, f.get(team_field_id) if team_field_id else None, key):
                continue
            relevant_issues.append(issue)
            w_data = f.get("worklog", {})
            if w_data.get("total", 0) > len(w_data.get("worklogs", [])):
                needs_fetch.append(key)

        fetched_worklogs = _resolve_worklogs(config, needs_fetch)

        # Pass 2: build tickets + lane totals + per-member logging together,
        # in one loop, off the one shared worklog dataset.
        for issue in relevant_issues:
            key, f = issue["key"], issue["fields"]

            assignee = f.get("assignee") or {}
            owner = (assignee.get("displayName") or assignee.get("name")
                     or assignee.get("emailAddress") or "Unassigned")

            w_data = f.get("worklog", {})
            worklogs = fetched_worklogs.get(key, w_data.get("worklogs", []))

            spent = sum(log.get("timeSpentSeconds", 0) for log in worklogs)
            total_logged += spent

            # per-member logging — credit each log entry to its author
            for log in worklogs:
                author = log.get("author", {})
                author_name = (author.get("displayName") or author.get("name")
                               or author.get("emailAddress") or owner)
                seconds = log.get("timeSpentSeconds", 0)
                member_logged[author_name] = member_logged.get(author_name, 0) + seconds
            if owner not in member_logged:
                member_logged[owner] = 0

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

        return data, totals, total_logged, member_logged

    except Exception as e:
        print(f"[get_team_overview_and_logging] Error: {e}")
        return None, None, None, {}


def get_team_member_logging(config, team_name, sprint_id=None):
    """Back-compat wrapper. Prefer get_team_overview_and_logging for callers
    that need both ticket data and member logging — calling both this and
    get_team_overview_data separately re-runs the same Jira search twice."""
    _, _, _, member_logged = get_team_overview_and_logging(config, team_name, sprint_id=sprint_id)
    return member_logged


def get_team_overview_data(config, team_name, sprint_id=None):
    """Back-compat wrapper. Prefer get_team_overview_and_logging for callers
    that need both ticket data and member logging — calling both this and
    get_team_member_logging separately re-runs the same Jira search twice."""
    data, totals, total_logged, _ = get_team_overview_and_logging(config, team_name, sprint_id=sprint_id)
    return data, totals, total_logged


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

    jql = f"sprint in openSprints() AND issuetype = 'Task' AND {_project_clause(config)}{_te_project_clause(config)}"

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


def get_sprint_member_logging(config, sprint_id, members):
    """
    Direct, assignee-based lookup used by the History feature: for the
    given member names, find every ticket assigned to them in this exact
    sprint and sum logged time per person (credited to whoever actually
    logged each worklog entry, same convention as
    get_team_overview_and_logging).

    Deliberately skips two filters get_team_overview_and_logging relies on
    for the LIVE dashboard — issuetype = 'Task', and a match on the
    ticket-level 'Team' field — because for a past, closed sprint those can
    silently zero out a member who genuinely had tickets (wrong issue type,
    team field not set/renamed since, etc.), showing "No tickets in this
    sprint" when they actually had some. Also does not filter by status —
    a closed sprint's tickets are usually Done/Closed/Approved, and that's
    exactly what we still want counted here.

    Returns: dict of {member_name: logged_seconds}. A member absent from
    the returned dict has zero tickets assigned in this sprint. Empty dict
    if `members` is empty or the request fails.
    """
    if not members:
        return {}

    quoted = ", ".join(f'"{m}"' for m in members)
    jql = f"sprint = {sprint_id} AND assignee in ({quoted}) AND {_project_clause(config)}"

    try:
        all_issues, start_at, page = [], 0, 100
        while True:
            resp = requests.get(
                f"https://{DOMAIN}/rest/api/2/search",
                params={"jql": jql, "fields": "assignee,worklog", "maxResults": page, "startAt": start_at},
                auth=HTTPBasicAuth(config["user"], config["token"]),
                timeout=20
            ).json()
            batch = resp.get("issues", [])
            all_issues.extend(batch)
            if len(batch) < page:
                break
            start_at += page

        needs_fetch = []
        for issue in all_issues:
            w_data = issue["fields"].get("worklog", {})
            if w_data.get("total", 0) > len(w_data.get("worklogs", [])):
                needs_fetch.append(issue["key"])
        fetched_worklogs = _resolve_worklogs(config, needs_fetch)

        member_logged = {}
        for issue in all_issues:
            key, f = issue["key"], issue["fields"]
            assignee = f.get("assignee") or {}
            owner = (assignee.get("displayName") or assignee.get("name")
                     or assignee.get("emailAddress") or "Unassigned")
            if owner not in member_logged:
                member_logged[owner] = 0   # ensure presence even if this ticket has 0 worklogs

            w_data = f.get("worklog", {})
            worklogs = fetched_worklogs.get(key, w_data.get("worklogs", []))
            for log in worklogs:
                author = log.get("author", {})
                author_name = (author.get("displayName") or author.get("name")
                               or author.get("emailAddress") or owner)
                seconds = log.get("timeSpentSeconds", 0)
                member_logged[author_name] = member_logged.get(author_name, 0) + seconds

        return member_logged
    except Exception as e:
        print(f"[get_sprint_member_logging] Error: {e}")
        return {}


def get_my_team_open_tickets(config, team_name=None):
    """
    Fetch open (not Closed/Approved/Resolved) Task tickets from the last 5
    sprints (get_last_n_sprints), scoped to the currently selected
    project(s) (config['selected_projects'] — the same scope used
    everywhere else in the app). Task-only matches every other JQL query
    in this file (get_team_overview_and_logging, get_jira_data, etc.).

    team_name=None (default — "Team Tickets" / main-dashboard call sites):
    scoped to the roster tracked in this app (config['members'], built via
    the "Add Members" flow) — assignee OR reporter must be one of those
    members. Unchanged from before.

    team_name="<Team>" (Teams Overview call site, which lets you browse
    several teams via a dropdown): scoped instead to the ticket-level
    'Team' custom field, matched Python-side via _team_matches — the same
    approach get_team_overview_and_logging uses, since Team-field JQL is
    unreliable. Ignores config['members'] entirely, so this reflects the
    team picked in Teams Overview instead of whatever roster happens to be
    tracked on the main dashboard.

    Scoped via JQL "sprint in (id1, id2, ...)" using the 5 most recently
    started sprints (active or closed) — this is what keeps the query
    fast, since Jira only searches those sprints' tickets server-side
    instead of a ticket's full sprint history. Falls back to
    "sprint in openSprints()" if sprint lookups fail for some reason.

    Returns: list of (key, url, status_name, assignee_name, sprint_name)
    tuples, sorted by sprint then assignee then key. Empty list if
    team_name is None and no members are tracked yet (no JQL is run in
    that case), or the request fails.
    """
    roster = sorted({m for m in config.get("members", {}).keys() if m and m != "Unassigned"})
    if team_name is None and not roster:
        return []

    recent_sprints = get_last_n_sprints(config, 5)

    # Drop the current (active) sprint so the Open Tickets window only
    # surfaces tickets from previous sprints. An active-state sprint is
    # already visible on the main dashboard — showing it here too causes
    # duplicate noise for the user.
    past_sprint_ids = [
        s["id"] for s in recent_sprints
        if s.get("id") and s.get("state") != "active"
    ]
    if not past_sprint_ids:
        # All recent sprints are still active (rare edge case, e.g. very first
        # sprint) — nothing meaningful to show in the past-sprint view.
        return []

    sprint_clause = "sprint in (%s)" % ", ".join(str(i) for i in past_sprint_ids)

    jql = (f"status not in (Closed, Approved, Resolved) AND issuetype = 'Task' "
           f'AND {sprint_clause} '
           f'AND {_project_clause(config)}')
    if team_name is None:
        quoted = ", ".join(f'"{m}"' for m in roster)
        jql += f' AND (assignee in ({quoted}) OR reporter in ({quoted}))'

    team_field_id = get_team_field_name(config) if team_name is not None else None
    sprint_field_id = get_sprint_field_id(config)
    fields = "status,assignee"
    if sprint_field_id:
        fields += f",{sprint_field_id}"
    if team_field_id:
        fields += f",{team_field_id}"

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

        results = []
        for issue in all_issues:
            key, f = issue["key"], issue["fields"]
            if team_name is not None:
                if not _team_matches(team_name, f.get(team_field_id) if team_field_id else None, key):
                    continue
            assignee = f.get("assignee") or {}
            assignee_name = (assignee.get("displayName") or assignee.get("name")
                              or assignee.get("emailAddress") or "Unassigned")
            status_name = f.get("status", {}).get("name", "Unknown")
            sprint_name = _extract_sprint_name(f.get(sprint_field_id)) if sprint_field_id else "No Sprint"
            results.append((key, f"https://{DOMAIN}/browse/{key}", status_name, assignee_name, sprint_name))

        results.sort(key=lambda x: (x[4], x[3], x[0]))
        return results
    except Exception as e:
        print(f"[get_my_team_open_tickets] Error: {e}")
        return []


def get_jira_data(config, target_username=None, force_team_filter=None, sprint_id=None):
    """
    sprint_id: when given, scopes to that exact sprint ("sprint = {id}")
    instead of whatever sprint(s) are currently open — used by the History
    feature to pull a past, closed sprint's data instead of live data.
    """
    identity = get_user_identity(config, target_username)
    if not identity or "error" in identity:
        return None, None, None, False

    search_user = f"'{target_username}'" if target_username else "currentUser()"
    sprint_clause = f"sprint = {sprint_id}" if sprint_id else "sprint in openSprints()"
    jql = f"assignee={search_user} AND {sprint_clause} AND {_project_clause(config)}{_te_project_clause(config)}"
    if target_username:
        jql += " AND issuetype = 'Task'"

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

        relevant_issues = []
        needs_fetch = []
        for issue in response.get("issues", []):
            key, f = issue["key"], issue["fields"]
            
            # --- UPDATED MULTI-TEAM FILTER LOGIC ---
            if force_team_filter and force_team_filter != "All Teams":
                team_val = f.get(team_field_id) if team_field_id else None
                if isinstance(force_team_filter, (set, list)):
                    # Ticket must match at least one of the selected teams
                    if "All Teams" not in force_team_filter:
                        if not any(_team_matches(t, team_val, key) for t in force_team_filter):
                            continue
                else:
                    if not _team_matches(force_team_filter, team_val, key):
                        continue
            # ---------------------------------------
            
            relevant_issues.append(issue)
            w_data = f.get("worklog", {})
            if w_data.get("total", 0) > len(w_data.get("worklogs", [])):
                needs_fetch.append(key)

        fetched_worklogs = _resolve_worklogs(config, needs_fetch)

        for issue in relevant_issues:
            key, f = issue["key"], issue["fields"]

            w_data = f.get("worklog", {})
            worklogs = fetched_worklogs.get(key, w_data.get("worklogs", []))

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
