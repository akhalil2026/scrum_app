import customtkinter as ctk
import webbrowser
import threading
import urllib.request
import io
import json
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox
from PIL import Image
from config import load_config, save_config, clear_config, TEAMS_LIST, REFRESH_INTERVAL , HOURS_PER_DAY 
from jira_engine import get_jira_data, get_team_overview_data, get_team_overview_and_logging, get_team_member_logging, get_current_sprint, calculate_expected_hours, calculate_expected_hours_j1, format_time, get_user_identity, get_achieved_tcs_field_id, get_achieved_reqs_field_id, get_achieved_tickets_field_id, get_ticket_checklist, get_all_teams
from notifications import start_scheduler

# =========================================================
# 📋 To-Do/checklist presence cache — avoids re-querying Jira
# every time a lane re-renders. Populated in bulk (in parallel)
# right after each ticket fetch, then read synchronously by the
# UI render functions.
# =========================================================
_checklist_cache = {}
_checklist_cache_lock = threading.Lock()

def _ticket_has_checklist(config, ticket_key):
    """Return True/False for whether ticket_key has a to-do/checklist, using the cache."""
    with _checklist_cache_lock:
        if ticket_key in _checklist_cache:
            return _checklist_cache[ticket_key]
    has = False
    try:
        _, _, total, error = get_ticket_checklist(config, ticket_key)
        has = bool(total) and not error
    except Exception:
        has = False
    with _checklist_cache_lock:
        _checklist_cache[ticket_key] = has
    return has

def _warm_checklist_cache(config, ticket_keys):
    """Pre-fetch checklist presence for many tickets in parallel so renders are instant."""
    with _checklist_cache_lock:
        keys_to_fetch = [k for k in set(ticket_keys) if k and k not in _checklist_cache]
    if not keys_to_fetch:
        return
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda k: _ticket_has_checklist(config, k), keys_to_fetch))

# =========================================================
# 🧠 Remember last-used Activity/Validation/Status/Owner filter
# selections per dashboard, persisted in config.json so they
# survive closing and reopening a dashboard window.
# =========================================================
ALL_STATUS_VALUES = {"TODO", "IN_PROGRESS", "APPROVED", "BLOCKED", "PARTIALLY_BLOCKED", "DONE"}

def _load_saved_filter_selection(config, dashboard_key, filter_name, fallback_values):
    """Return a set of saved selections for dashboard_key/filter_name, or all fallback_values if none saved yet."""
    saved = config.get("filter_prefs", {}).get(dashboard_key, {}).get(filter_name)
    if saved is None:
        return set(fallback_values)
    return set(saved)

def _save_filter_selection(config, dashboard_key, activity_sel, validation_sel, status_sel, owner_sel):
    prefs = config.setdefault("filter_prefs", {})
    prefs[dashboard_key] = {
        "activity": sorted(activity_sel),
        "validation": sorted(validation_sel),
        "status": sorted(status_sel),
        "owner": sorted(owner_sel),
    }
    save_config(config)

# =========================================================
# 🌍 Dynamic team list — parsed from real Jira ticket data instead of a
# hardcoded list in config.py. This prevents the exact bug where a team is
# renamed/added in Jira but the hardcoded name no longer matches, silently
# returning zero tickets for that team until someone notices and edits the
# code by hand.
#
# `get_teams_list()` always returns SOMETHING immediately (cached dynamic
# list if we have one, otherwise the static TEAMS_LIST from config.py as a
# safe fallback so dropdowns are never empty). `_refresh_teams_list_async()`
# re-fetches the real list from Jira in the background and caches it into
# config["dynamic_teams_list"] for next time.
# =========================================================
def get_teams_list(config):
    """Return the team list to show in dropdowns: dynamic (from Jira) if we have it, else the static fallback."""
    dynamic = config.get("dynamic_teams_list")
    return dynamic if dynamic else TEAMS_LIST

def _refresh_teams_list_async(config, on_done=None):
    """
    Background-thread refresh of the dynamic team list. Safe to call as
    often as you like (e.g. on every dashboard open, or via a manual
    refresh button) — it's a single lightweight Jira query.

    on_done: optional zero-arg callback invoked on the calling thread once
    the refresh finishes (only fired if the list actually changed), so a
    caller can update an already-open dropdown's values.
    """
    def _work():
        try:
            fresh = get_all_teams(config)
        except Exception:
            return
        if not fresh or len(fresh) <= 1:
            return  # Nothing useful came back (e.g. offline) — keep whatever we already had.
        changed = fresh != config.get("dynamic_teams_list")
        config["dynamic_teams_list"] = fresh
        save_config(config)
        if changed and on_done:
            on_done()
    threading.Thread(target=_work, daemon=True).start()

# =========================================================
# 🎨 Global Premium SaaS Light/Dark Mode Configurations
# =========================================================
FONT_FAMILY = "Segoe UI"
APP_BG = ("#f3f4f6", "#141517")             # Main application background
NAV_BG = ("#ffffff", "#1c1d21")             # Top navigation bar
CARD_BG = ("#ffffff", "#18191c")            # Main containers and cards
ITEM_BG = ("#f9fafb", "#1e1f24")            # Inner items and ticket boxes
BORDER_COLOR = ("#e5e7eb", "#374151")       # Clean borders
TEXT_MAIN = ("#111827", "#ffffff")          # Crisp primary text
TEXT_MUTED = ("#4b5563", "#9ca3af")         # Muted secondary text
BTN_BG = ("#e5e7eb", "#2e303b")             # Default standard buttons
BTN_HOVER = ("#d1d5db", "#3a3d4a")          # Standard button hover
BADGE_BG = ("#f3f4f6", "#25272c")           # Small metric badges
ACCENT_BLUE = ("#2563eb", "#1d4ed8")        # Primary Blue Accent      
ACCENT_HOVER = ("#1d4ed8", "#1e40af")       # Primary Blue Hover

# --- HELPER LOGIC TO CALCULATE HOLIDAYS BY SELECTED DATES ---
def calculate_holiday_subtraction_seconds(start_date_str, holiday_dates_list):
    if not start_date_str or not holiday_dates_list: return 0
    try:
        start_dt = datetime.fromisoformat(start_date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        now = datetime.utcnow()
        if now < start_dt: return 0
        holiday_days = set()
        for d_str in holiday_dates_list:
            try: holiday_days.add(datetime.strptime(d_str.strip(), "%Y-%m-%d").date())
            except ValueError: continue
        deducted_hours = 0.0
        current = start_dt
        while current.date() <= now.date():
            if current.date() in holiday_days and current.weekday() < 5:
                if current.date() == start_dt.date() and current.date() == now.date():
                    h1 = start_dt.hour + start_dt.minute/60.0
                    h2 = now.hour + now.minute/60.0
                    h1_clamped = max(8, min(17, h1))
                    h2_clamped = max(8, min(17, h2))
                    deducted_hours += max(0.0, h2_clamped - h1_clamped)
                elif current.date() == start_dt.date():
                    h1 = start_dt.hour + start_dt.minute/60.0
                    h1_clamped = max(8, min(17, h1))
                    deducted_hours += max(0.0, 17.0 - h1_clamped)
                elif current.date() == now.date():
                    h2 = now.hour + now.minute/60.0
                    h2_clamped = max(8, min(17, h2))
                    deducted_hours += max(0.0, h2_clamped - 8.0)
                else: deducted_hours += 8.0
            try: current = current + timedelta(days=1)
            except OverflowError: break
        return int(deducted_hours * 3600)
    except Exception: return 0

# --- CUSTOM EMBEDDED INLINE CALENDAR COMPONENT FRAME ---
class EmbeddedCalendarSelector(ctk.CTkFrame):
    def __init__(self, parent, current_selections, on_save_callback):
        super().__init__(parent, fg_color=CARD_BG, border_width=1, border_color=BORDER_COLOR, corner_radius=10)
        self.on_save_callback = on_save_callback
        self.selected_dates = set(current_selections)
        today = datetime.today()
        self.current_year = today.year
        self.current_month = today.month
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", padx=12, pady=(10, 5))
        self.prev_btn = ctk.CTkButton(self.header_frame, text="◀", width=26, height=26, fg_color=BTN_BG, hover_color=BTN_HOVER, command=self.prev_month)
        self.prev_btn.pack(side="left")
        self.month_label = ctk.CTkLabel(self.header_frame, text="", font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MAIN)
        self.month_label.pack(side="left", fill="x", expand=True)
        self.next_btn = ctk.CTkButton(self.header_frame, text="▶", width=26, height=26, fg_color=BTN_BG, hover_color=BTN_HOVER, command=self.next_month)
        self.next_btn.pack(side="right")
        self.days_grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.days_grid_frame.pack(fill="both", expand=True, padx=12, pady=5)
        self.bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.bottom_frame.pack(fill="x", padx=12, pady=(5, 10))
        self.save_btn = ctk.CTkButton(self.bottom_frame, text="Apply Changes", font=(FONT_FAMILY, 12, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, height=28, command=self.save_and_close)
        self.save_btn.pack(fill="x")
        self.draw_calendar_matrix()

    def draw_calendar_matrix(self):
        for child in self.days_grid_frame.winfo_children(): child.destroy()
        month_name = calendar.month_name[self.current_month]
        self.month_label.configure(text=f"{month_name} {self.current_year}")
        days_headers = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
        for i, heading in enumerate(days_headers):
            lbl = ctk.CTkLabel(self.days_grid_frame, text=heading, font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=38, height=20)
            lbl.grid(row=0, column=i, pady=(0, 2))
        month_cal = calendar.monthcalendar(self.current_year, self.current_month)
        for row_idx, week in enumerate(month_cal):
            for col_idx, day in enumerate(week):
                if day == 0: continue
                date_str = f"{self.current_year}-{self.current_month:02d}-{day:02d}"
                is_selected = date_str in self.selected_dates
                bg_color = "#fb923c" if is_selected else CARD_BG
                text_color = "white" if is_selected else TEXT_MAIN
                hover_color = "#ea580c" if is_selected else ITEM_BG
                btn = ctk.CTkButton(self.days_grid_frame, text=str(day), width=36, height=28, fg_color=bg_color, text_color=text_color, hover_color=hover_color, font=(FONT_FAMILY, 11, "bold" if is_selected else "normal"), command=lambda d=date_str: self.toggle_date(d))
                btn.grid(row=row_idx + 1, column=col_idx, padx=1, pady=1)

    def toggle_date(self, date_str):
        if date_str in self.selected_dates: self.selected_dates.remove(date_str)
        else: self.selected_dates.add(date_str)
        self.draw_calendar_matrix()

    def prev_month(self):
        self.current_month -= 1
        if self.current_month < 1: self.current_month = 12; self.current_year -= 1
        self.draw_calendar_matrix()

    def next_month(self):
        self.current_month += 1
        if self.current_month > 12: self.current_month = 1; self.current_year += 1
        self.draw_calendar_matrix()

    def save_and_close(self):
        self.on_save_callback(sorted(list(self.selected_dates)))

def open_member_dashboard(config, username):
    win = ctk.CTkToplevel()
    win.geometry("1050x750")  
    win.title(f"Team Member Details: {username}")
    win.configure(fg_color=APP_BG)
    win.attributes('-topmost', True)
    scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=20, pady=20)
    cancelled = {"value": False}
    
    def load():
        active_team = config.get("selected_team", "All Teams")
        tickets, totals, logged, _ = get_jira_data(config, username, force_team_filter=active_team)
        sprint = get_current_sprint(config)
        if not tickets or cancelled["value"]: return
        team_dedications = config.get("team_member_dedications", {}).get(active_team, {})
        old_fallback = config.get("member_dedications", {}).get(username, 1.0)
        m_dedication = team_dedications.get(username, old_fallback)
        base_expected = calculate_expected_hours(sprint["startDate"]) * 3600 if sprint else 0
        holiday_seconds = calculate_holiday_subtraction_seconds(sprint["startDate"] if sprint else None, config.get("holiday_dates", []))
        expected = max(0, (base_expected - holiday_seconds) * m_dedication)
        if not cancelled["value"]: win.after(0, lambda: render_dashboard_ui(scroll, username, m_dedication, logged, expected, tickets, totals))

    def render_dashboard_ui(scroll, username, m_dedication, logged, expected, tickets, totals):
        if cancelled["value"]: return
        header_card = ctk.CTkFrame(scroll, fg_color=CARD_BG, corner_radius=12, border_width=1, border_color=BORDER_COLOR)
        header_card.pack(fill="x", pady=(0, 20), ipady=10)
        ctk.CTkLabel(header_card, text=f"👤 {username}", font=(FONT_FAMILY, 24, "bold"), text_color=TEXT_MAIN).pack(pady=(10, 2))
        ctk.CTkLabel(header_card, text=f"Dedication: {int(m_dedication*100)}%", font=(FONT_FAMILY, 13), text_color=TEXT_MUTED).pack()
        msg, badge_color = ("🚀 On Track / Great Job!", "#059669") if logged >= expected else (f"⚠️ Missing : {format_time(expected - logged)}", "#dc2626")
        status_badge = ctk.CTkFrame(header_card, fg_color=badge_color, corner_radius=20, height=32)
        status_badge.pack(pady=12, padx=20)
        status_badge.pack_propagate(False)
        ctk.CTkLabel(status_badge, text=msg, font=(FONT_FAMILY, 13, "bold"), text_color="white").pack(expand=True)
        categories = [("🟥 To Do / Open", "todo", "#f87171"), ("🟧 In Progress", "in_progress", "#fb923c"), ("🟦 Approved", "approved", "#60a5fa"), ("⛔ Blocked", "blocked", "#dc2626"), ("🟡 Partially Blocked", "partially_blocked", "#eab308"), ("🟩 Done / Resolved", "done", "#34d399")]
        for title, key, heading_color in categories:
            section_frame = ctk.CTkFrame(scroll, fg_color=CARD_BG, corner_radius=10, border_width=1, border_color=BORDER_COLOR)
            section_frame.pack(fill="x", pady=10, ipady=5)
            ctk.CTkLabel(section_frame, text=f"{title}  •  Total Logged: {format_time(totals[key])}", font=(FONT_FAMILY, 14, "bold"), text_color=heading_color).pack(anchor="w", padx=15, pady=10)
            if not tickets[key]:
                ctk.CTkLabel(section_frame, text="No tickets in this category.", font=(FONT_FAMILY, 12, "italic"), text_color=TEXT_MUTED).pack(anchor="w", padx=30, pady=5)
                continue
            th_row = ctk.CTkFrame(section_frame, fg_color="transparent")
            th_row.pack(fill="x", padx=15, pady=(2, 5))
            ctk.CTkLabel(th_row, text="Ticket Title", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=5)
            ctk.CTkLabel(th_row, text="Logged Time", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=110, anchor="e").pack(side="right", padx=(0, 25))
            ctk.CTkLabel(th_row, text="Estimated Time", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=120, anchor="e").pack(side="right", padx=10)
            ctk.CTkLabel(th_row, text="Activity", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=140, anchor="w").pack(side="right", padx=15)
            for item in tickets[key]:
                t_key, sumry, link, sp, rm, activity_lbl, validation_lbl, est_time, *extra = item
                activity_lbl = activity_lbl if activity_lbl else "N/A"
                t_box = ctk.CTkFrame(section_frame, fg_color=ITEM_BG, corner_radius=6, border_width=1, border_color=BORDER_COLOR)
                t_box.pack(fill="x", padx=15, pady=4)
                # Pack right-side elements FIRST so title fills remaining space
                lbl_logged = ctk.CTkLabel(t_box, text=sp if sp else "0h", font=(FONT_FAMILY, 12, "bold"), text_color="#34d399", width=110, anchor="e")
                lbl_logged.pack(side="right", padx=20, pady=8)
                lbl_est = ctk.CTkLabel(t_box, text=est_time if est_time else "0h", font=(FONT_FAMILY, 12), text_color=TEXT_MUTED, width=120, anchor="e")
                lbl_est.pack(side="right", padx=10, pady=8)
                act_badge = ctk.CTkFrame(t_box, fg_color=BTN_BG if activity_lbl != "N/A" else "transparent", corner_radius=4)
                act_badge.pack(side="right", padx=15, pady=6)
                lbl_act = ctk.CTkLabel(act_badge, text=str(activity_lbl).upper(), font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE if activity_lbl != "N/A" else TEXT_MUTED, width=120, anchor="center")
                lbl_act.pack(padx=8, pady=2)
                # Title packed LAST so it fills remaining space without pushing right elements off screen
                lbl_title = ctk.CTkLabel(t_box, text=f"[{t_key}] {sumry}", text_color=TEXT_MAIN, font=(FONT_FAMILY, 12), cursor="hand2", justify="left", anchor="w", wraplength=0)
                lbl_title.pack(side="left", fill="x", expand=True, padx=12, pady=8)
                lbl_title.bind("<Button-1>", lambda e, url=link: webbrowser.open(url))

    def cleanup_window():
        cancelled["value"] = True
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", cleanup_window)
    threading.Thread(target=load, daemon=True).start()

def open_ticket_checklist_window(config, parent, ticket_key, link):
    """
    Popup showing a single ticket's native Jira description checklist
    ('To Do List: X/Y resolved'), fetched live and rendered read-only.
    """
    win = ctk.CTkToplevel(parent)
    win.geometry("520x600")
    win.title(f"To Do List — {ticket_key}")
    win.configure(fg_color=APP_BG)
    win.attributes('-topmost', True)
    win.transient(parent)
    cancelled = {"value": False}
    win.protocol("WM_DELETE_WINDOW", lambda: (cancelled.update(value=True), win.destroy()))

    header = ctk.CTkFrame(win, fg_color=NAV_BG, height=50, corner_radius=8)
    header.pack(fill="x", padx=15, pady=(15, 5))
    header.pack_propagate(False)
    ctk.CTkLabel(header, text=f"📋 To Do List — {ticket_key}", font=(FONT_FAMILY, 14, "bold"), text_color=TEXT_MAIN).pack(side="left", padx=15)
    ctk.CTkButton(header, text="Open in Jira ↗", width=120, height=28, font=(FONT_FAMILY, 11, "bold"),
                  fg_color=BTN_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN,
                  command=lambda: webbrowser.open(link)).pack(side="right", padx=10)

    progress_label = ctk.CTkLabel(win, text="Loading checklist...", font=(FONT_FAMILY, 12, "bold"), text_color=TEXT_MUTED)
    progress_label.pack(anchor="w", padx=22, pady=(8, 0))

    list_container = ctk.CTkScrollableFrame(win, fg_color="transparent")
    list_container.pack(fill="both", expand=True, padx=15, pady=10)

    def render(items, resolved, total, error):
        if cancelled["value"]: return
        for child in list_container.winfo_children(): child.destroy()
        if error:
            progress_label.configure(text=error, text_color="#f87171")
            return
        if total == 0:
            progress_label.configure(text="This ticket has no to-do / checklist in its description.")
            return
        progress_label.configure(text=f"{resolved} / {total} resolved", text_color=TEXT_MUTED)
        for text, done in items:
            row = ctk.CTkFrame(list_container, fg_color=ITEM_BG, corner_radius=6)
            row.pack(fill="x", pady=3)
            icon = "☑" if done else "☐"
            icon_color = "#34d399" if done else TEXT_MUTED
            ctk.CTkLabel(row, text=icon, font=(FONT_FAMILY, 14), text_color=icon_color, width=26).pack(side="left", padx=(8, 0), pady=8)
            item_font = ctk.CTkFont(family=FONT_FAMILY, size=12, overstrike=done)
            ctk.CTkLabel(row, text=text if text else "(untitled item)", font=item_font,
                         text_color=TEXT_MUTED if done else ACCENT_BLUE,
                         anchor="w", justify="left", wraplength=420).pack(side="left", fill="x", expand=True, padx=(4, 8), pady=8)

    def load():
        items, resolved, total, error = get_ticket_checklist(config, ticket_key)
        if cancelled["value"]: return
        win.after(0, lambda: render(items, resolved, total, error))

    threading.Thread(target=load, daemon=True).start()

def open_team_tickets_dashboard(config):
    win = ctk.CTkToplevel()
    win.geometry("1150x850")
    win.title("Global Tracked Team Tickets Lane Overview")
    win.configure(fg_color=APP_BG)
    win.attributes('-topmost', True)
    win.grid_columnconfigure(0, weight=1)
    win.grid_rowconfigure(2, weight=1)
    raw_aggregated_tickets = {"todo": [], "in_progress": [], "approved": [], "blocked": [], "partially_blocked": [], "done": []}
    available_activities = set()
    show_summary = {"value": True}
    cancelled = {"value": False}
    top_bar = ctk.CTkFrame(win, fg_color=NAV_BG, height=60, corner_radius=8)
    top_bar.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 10))
    top_bar.pack_propagate(False)
    ctk.CTkLabel(top_bar, text="👥 Sprint Summary", font=(FONT_FAMILY, 16, "bold"), text_color=TEXT_MAIN).pack(side="left", padx=20)
    


    def toggle_summary():
        if show_summary["value"]:
            summary_container.grid_remove()
            toggle_btn.configure(text="📊 Show Summary")
        else:
            summary_container.grid()
            render_activity_summary_box()
            toggle_btn.configure(text="📊 Hide Summary")
        show_summary["value"] = not show_summary["value"]

    btns_right = ctk.CTkFrame(top_bar, fg_color="transparent")
    btns_right.pack(side="right", padx=10)
    toggle_btn = ctk.CTkButton(btns_right, text="📊 Hide Summary", width=120, height=28, font=(FONT_FAMILY, 12, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, command=toggle_summary)
    toggle_btn.pack(side="left", padx=5)
    filter_container = ctk.CTkFrame(top_bar, fg_color="transparent")
    filter_container.pack(side="right", padx=20)
    summary_container = ctk.CTkFrame(win, fg_color="transparent")
    summary_container.grid(row=1, column=0, sticky="ew", padx=20)
    summary_frame = ctk.CTkFrame(summary_container, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
    summary_frame.pack(fill="x")
    scroll_container = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll_container.grid(row=2, column=0, sticky="nsew", padx=20, pady=10)

    def parse_time_str_to_seconds(time_str):
        if not time_str or time_str == "0m" or time_str == "0h": return 0
        try:
            seconds = 0
            parts = str(time_str).split()
            for p in parts:
                if 'd' in p: seconds += int(p.replace('d','')) * HOURS_PER_DAY * 3600
                elif 'h' in p: seconds += int(p.replace('h','')) * 3600
                elif 'm' in p: seconds += int(p.replace('m','')) * 60
            return seconds
        except Exception: return 0

    def render_activity_summary_box():
        for child in summary_frame.winfo_children(): child.destroy()
        ctk.CTkLabel(summary_frame, text="📊 Total Logged & Estimated Time per Activity & Validation", font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MUTED).pack(anchor="w", padx=15, pady=(8, 4))
        combined_totals = {}
        for lane in ["todo", "in_progress", "approved", "blocked", "partially_blocked", "done"]:
            for item in raw_aggregated_tickets[lane]:
                activity_lbl = str(item[5]).strip().upper() if len(item) > 5 else "N/A"
                validation_lbl = str(item[6]).strip() if len(item) > 6 else "N/A"
                if activity_lbl == "N/A": continue
                logged_sec = parse_time_str_to_seconds(item[3])
                est_sec = parse_time_str_to_seconds(item[7])  # index 7 = est_time (not index 4 = remaining)
                # achieved fields at indices 8, 9, 10 (user appended at 11)
                ach_tcs     = item[8]  if len(item) > 8  and isinstance(item[8],  int) else 0
                ach_reqs    = item[9]  if len(item) > 9  and isinstance(item[9],  int) else 0
                ach_tickets = item[10] if len(item) > 10 and isinstance(item[10], int) else 0
                combo_key = (activity_lbl, validation_lbl)
                if combo_key not in combined_totals:
                    combined_totals[combo_key] = {"logged": 0, "estimated": 0, "achieved": 0, "ach_reqs": 0, "ach_tcs": 0, "ach_tickets": 0}
                combined_totals[combo_key]["logged"]   += logged_sec
                combined_totals[combo_key]["estimated"] += est_sec
                combined_totals[combo_key]["achieved"] += ach_tcs + ach_reqs + ach_tickets
                combined_totals[combo_key]["ach_reqs"]    += ach_reqs
                combined_totals[combo_key]["ach_tcs"]     += ach_tcs
                combined_totals[combo_key]["ach_tickets"] += ach_tickets
        if not combined_totals:
            ctk.CTkLabel(summary_frame, text="No activities found.", font=(FONT_FAMILY, 12, "italic"), text_color=TEXT_MUTED).pack(anchor="w", padx=15, pady=8)
            return
        badge_container = ctk.CTkScrollableFrame(summary_frame, fg_color="transparent", height=220)
        badge_container.pack(fill="x", padx=15, pady=(0, 10))
        for (act, val), totals in sorted(combined_totals.items()):
            percent = int((totals['logged'] / totals['estimated'] * 100)) if totals['estimated'] > 0 else 0
            prog_color = "#34d399" if percent >= 100 else ("#fb923c" if percent > 50 else "#f87171")
            badge = ctk.CTkFrame(badge_container, fg_color=BADGE_BG, corner_radius=6, border_width=1, border_color=BORDER_COLOR)
            badge.pack(fill="x", pady=3, anchor="w")
            display_name = f"{act} ({val})" if val and val != "N/A" else f"{act}"
            ctk.CTkLabel(badge, text=f"{display_name}:", font=(FONT_FAMILY, 11, "bold"), text_color=ACCENT_BLUE).pack(side="left", padx=(10, 2), pady=6)
            ctk.CTkLabel(badge, text=f"Estimated:{format_time(totals['estimated'])}", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=(2, 5), pady=6)
            ctk.CTkLabel(badge, text=f"Logged:{format_time(totals['logged'])}", font=(FONT_FAMILY, 11, "bold"), text_color="#34d399").pack(side="left", padx=(2, 5), pady=6)
            ach_summary_text = f"Achieved : {totals['achieved']} (Req {totals['ach_reqs']} || TC {totals['ach_tcs']} || Ticket {totals['ach_tickets']})"
            ctk.CTkLabel(badge, text=ach_summary_text, font=(FONT_FAMILY, 11, "bold"), text_color="#ecb80c").pack(side="left", padx=(2, 10), pady=6)

    def render_filtered_lanes(act_f, val_f, stat_f, own_f):
        for child in scroll_container.winfo_children(): child.destroy()
        swimlanes = [("🟥 Team To Do / Open", "todo", "#f87171"), ("🟧 Team In Progress", "in_progress", "#fb923c"), ("🟦 Team Approved", "approved", "#60a5fa"), ("⛔ Team Blocked", "blocked", "#dc2626"), ("🟡 Team Partially Blocked", "partially_blocked", "#eab308"), ("🟩 Team Done / Resolved", "done", "#34d399")]
        for title, key, heading_color in swimlanes:
            # Check status filter (handle both string and set)
            if isinstance(stat_f, set):
                stat_match = (("TODO" in stat_f and key == "todo") or
                             ("IN_PROGRESS" in stat_f and key == "in_progress") or
                             ("APPROVED" in stat_f and key == "approved") or
                             ("BLOCKED" in stat_f and key == "blocked") or
                             ("PARTIALLY_BLOCKED" in stat_f and key == "partially_blocked") or
                             ("DONE" in stat_f and key == "done"))
            else:
                stat_match = stat_f == "ALL STATUSES" or key == stat_f.lower()
            
            if not stat_match: continue
            
            # Filter items by activity, validation and owner
            lane_items = []
            for t in raw_aggregated_tickets[key]:
                act_match = (len(act_f) == 0 or str(t[5]).strip().upper() in act_f) if isinstance(act_f, set) else (act_f == "ALL ACTIVITIES" or str(t[5]).strip().upper() == act_f)
                val_match = (len(val_f) == 0 or str(t[6]).strip().upper() in val_f) if isinstance(val_f, set) else (val_f == "ALL VALIDATIONS" or str(t[6]).strip().upper() == val_f.upper())
                t_owner = str(t[11]).strip() if len(t) > 11 else "Unknown"
                own_match = (len(own_f) == 0 or t_owner in own_f) if isinstance(own_f, set) else (own_f == "ALL OWNERS" or t_owner == own_f)
                if act_match and val_match and own_match:
                    lane_items.append(t)
            
            section_frame = ctk.CTkFrame(scroll_container, fg_color=CARD_BG, corner_radius=10, border_width=1, border_color=BORDER_COLOR)
            section_frame.pack(fill="x", pady=10, ipady=5)
            ctk.CTkLabel(section_frame, text=f"{title} ({len(lane_items)} tickets)", font=(FONT_FAMILY, 14, "bold"), text_color=heading_color).pack(anchor="w", padx=15, pady=12)
            if not lane_items: continue
            for item in lane_items:
                t_key, sumry, link, sp, rm, act_lbl, val_lbl, estimated_time = item[0], item[1], item[2], item[3], item[4], item[5], item[6], item[7]
                achieved_tcs     = item[8] if len(item) > 8 else 0
                achieved_reqs    = item[9] if len(item) > 9 else 0
                achieved_tickets = item[10] if len(item) > 10 else 0
                owner            = item[11] if len(item) > 11 else "Unknown"

                # Achievement = sum of all three achieved fields
                achievement_val = (achieved_tcs if isinstance(achieved_tcs, int) else 0) + \
                                  (achieved_reqs if isinstance(achieved_reqs, int) else 0) + \
                                  (achieved_tickets if isinstance(achieved_tickets, int) else 0)
                
                item_box = ctk.CTkFrame(section_frame, fg_color=ITEM_BG, corner_radius=6, border_width=1, border_color=BORDER_COLOR)
                item_box.pack(fill="x", padx=15, pady=4)
                
                # Right side packed FIRST: Owner | Achievement | Logged | Est | Activity
                ctk.CTkLabel(item_box, text=f"👤 {owner}", font=(FONT_FAMILY, 11, "bold"), text_color=ACCENT_BLUE, width=100, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Achievement — only shown on the ticket row when it actually has a non-zero achievement;
                # the aggregated summary box above still counts every ticket regardless.
                if achievement_val != 0:
                    ach_color = "#34d399" if achievement_val > 0 else TEXT_MUTED
                    ach_text = f"✓ {achievement_val} (Req {achieved_reqs} TC {achieved_tcs} Ticket {achieved_tickets})"
                    ctk.CTkLabel(item_box, text=ach_text, font=(FONT_FAMILY, 11, "bold"), text_color=ach_color, width=220, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Logged Time
                ctk.CTkLabel(item_box, text=sp if sp else "0h", font=(FONT_FAMILY, 11, "bold"), text_color="#34d399", width=60, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Est Time
                ctk.CTkLabel(item_box, text=estimated_time, font=(FONT_FAMILY, 11), text_color=TEXT_MUTED, width=60, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Activity
                badge_text = str(act_lbl).upper() if act_lbl != "N/A" else "—"
                if val_lbl and val_lbl != "N/A":
                    badge_text += f"|{val_lbl}"
                ctk.CTkLabel(item_box, text=badge_text, font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE if act_lbl != "N/A" else TEXT_MUTED, width=80, anchor="e").pack(side="right", padx=8, pady=8)
                
                # To Do List button — opens this ticket's native description checklist
                # Only shown when the ticket actually has a to-do / checklist
                if _ticket_has_checklist(config, t_key):
                    ctk.CTkButton(item_box, text="📋", width=30, height=26, font=(FONT_FAMILY, 12, "bold"),
                                  fg_color=BTN_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN,
                                  command=lambda k=t_key, l=link: open_ticket_checklist_window(config, win, k, l)
                                  ).pack(side="right", padx=(8, 2), pady=8)
                
                # Title packed LAST so it fills remaining space without pushing right elements off screen
                lbl_title = ctk.CTkLabel(item_box, text=f"[{t_key}] {sumry}", text_color=TEXT_MAIN, font=(FONT_FAMILY, 12, "bold"), cursor="hand2", justify="left", anchor="w", wraplength=0)
                lbl_title.pack(side="left", fill="x", expand=True, padx=12, pady=8)
                lbl_title.bind("<Button-1>", lambda e, url=link: webbrowser.open(url))

    def initialize_ui_views():
        if cancelled["value"]: return
        activities = sorted(list(available_activities))
        # Include N/A in validations list
        validations = sorted(list({str(t[6]).strip().upper() for lane in raw_aggregated_tickets.values() for t in lane if len(t) > 6}))
        if "N/A" not in validations and any(t[6] == "N/A" for lane in raw_aggregated_tickets.values() for t in lane if len(t) > 6):
            validations.append("N/A")
        validations = sorted(validations)
        owners = sorted(list({str(t[11]).strip() for lane in raw_aggregated_tickets.values() for t in lane if len(t) > 11}))
        
        # Filter states — restore last-used selections if previously saved.
        # Owner is the exception: who owns tickets is intrinsically tied to
        # whichever team/members are currently loaded, so persisting it across
        # fetches/teams can leave stale names selected that no longer match
        # anyone in view (silently filtering out everything). Owner therefore
        # always starts fully selected on every fresh load.
        activity_filter = {"selected": _load_saved_filter_selection(config, "team_tickets", "activity", activities), "btn": None, "window": None}
        validation_filter = {"selected": _load_saved_filter_selection(config, "team_tickets", "validation", validations), "btn": None, "window": None}
        status_filter = {"selected": _load_saved_filter_selection(config, "team_tickets", "status", ALL_STATUS_VALUES), "btn": None, "window": None}
        owner_filter = {"selected": set(owners), "btn": None, "window": None}
        
        def _persist_filters():
            _save_filter_selection(config, "team_tickets", activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
        
        def toggle_activity_filter():
            if activity_filter["window"] and activity_filter["window"].winfo_exists():
                activity_filter["window"].destroy()
                activity_filter["window"] = None
                activity_filter["btn"].configure(text="📋 Activity")
            else:
                open_activity_dropdown()
        
        def open_activity_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x400")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = activity_filter["btn"].winfo_rootx()
                btn_y = activity_filter["btn"].winfo_rooty()
                btn_height = activity_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            act_vars = {}
            select_all_var = ctk.BooleanVar(value=len(activity_filter["selected"]) == len(activities))
            
            def toggle_all_activities():
                if select_all_var.get():
                    activity_filter["selected"] = set(activities)
                else:
                    activity_filter["selected"] = set()
                for var in act_vars.values(): var.set(False)
                for act in activity_filter["selected"]:
                    if act in act_vars: act_vars[act].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_activities,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for act in activities:
                var = ctk.BooleanVar(value=act in activity_filter["selected"])
                act_vars[act] = var
                def on_change(a=act, v=var):
                    if v.get(): activity_filter["selected"].add(a)
                    else: activity_filter["selected"].discard(a)
                    select_all_var.set(len(activity_filter["selected"]) == len(activities))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=act, variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            activity_filter["window"] = dropdown
            activity_filter["btn"].configure(text="📋 Activity ✓")
        
        def toggle_validation_filter():
            if validation_filter["window"] and validation_filter["window"].winfo_exists():
                validation_filter["window"].destroy()
                validation_filter["window"] = None
                validation_filter["btn"].configure(text="✓ Validation")
            else:
                open_validation_dropdown()
        
        def open_validation_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x400")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = validation_filter["btn"].winfo_rootx()
                btn_y = validation_filter["btn"].winfo_rooty()
                btn_height = validation_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            val_vars = {}
            select_all_var = ctk.BooleanVar(value=len(validation_filter["selected"]) == len(validations))
            
            def toggle_all_validations():
                if select_all_var.get():
                    validation_filter["selected"] = set(validations)
                else:
                    validation_filter["selected"] = set()
                for var in val_vars.values(): var.set(False)
                for val in validation_filter["selected"]:
                    if val in val_vars: val_vars[val].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_validations,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for val in validations:
                var = ctk.BooleanVar(value=val in validation_filter["selected"])
                val_vars[val] = var
                def on_change(v=val, var=var):
                    if var.get(): validation_filter["selected"].add(v)
                    else: validation_filter["selected"].discard(v)
                    select_all_var.set(len(validation_filter["selected"]) == len(validations))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=val, variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            validation_filter["window"] = dropdown
            validation_filter["btn"].configure(text="✓ Validation ✓")
        
        def toggle_status_filter():
            if status_filter["window"] and status_filter["window"].winfo_exists():
                status_filter["window"].destroy()
                status_filter["window"] = None
                status_filter["btn"].configure(text="⬜ Status")
            else:
                open_status_dropdown()
        
        def open_status_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x200")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = status_filter["btn"].winfo_rootx()
                btn_y = status_filter["btn"].winfo_rooty()
                btn_height = status_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            stat_vars = {}
            all_statuses = {"TODO", "IN_PROGRESS", "APPROVED", "BLOCKED", "PARTIALLY_BLOCKED", "DONE"}
            select_all_var = ctk.BooleanVar(value=len(status_filter["selected"]) == len(all_statuses))
            
            def toggle_all_statuses():
                if select_all_var.get():
                    status_filter["selected"] = set(all_statuses)
                else:
                    status_filter["selected"] = set()
                for var in stat_vars.values(): var.set(False)
                for stat in status_filter["selected"]:
                    if stat in stat_vars: stat_vars[stat].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_statuses,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for stat in all_statuses:
                var = ctk.BooleanVar(value=stat in status_filter["selected"])
                stat_vars[stat] = var
                def on_change(s=stat, var=var):
                    if var.get(): status_filter["selected"].add(s)
                    else: status_filter["selected"].discard(s)
                    select_all_var.set(len(status_filter["selected"]) == len(all_statuses))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=stat.replace("_", " "), variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            status_filter["window"] = dropdown
            status_filter["btn"].configure(text="⬜ Status ✓")
        
        def toggle_owner_filter():
            if owner_filter["window"] and owner_filter["window"].winfo_exists():
                owner_filter["window"].destroy()
                owner_filter["window"] = None
                owner_filter["btn"].configure(text="👤 Owner")
            else:
                open_owner_dropdown()
        
        def open_owner_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x400")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = owner_filter["btn"].winfo_rootx()
                btn_y = owner_filter["btn"].winfo_rooty()
                btn_height = owner_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            own_vars = {}
            select_all_var = ctk.BooleanVar(value=len(owner_filter["selected"]) == len(owners))
            
            def toggle_all_owners():
                if select_all_var.get():
                    owner_filter["selected"] = set(owners)
                else:
                    owner_filter["selected"] = set()
                for var in own_vars.values(): var.set(False)
                for o in owner_filter["selected"]:
                    if o in own_vars: own_vars[o].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_owners,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for o in owners:
                var = ctk.BooleanVar(value=o in owner_filter["selected"])
                own_vars[o] = var
                def on_change(o=o, var=var):
                    if var.get(): owner_filter["selected"].add(o)
                    else: owner_filter["selected"].discard(o)
                    select_all_var.set(len(owner_filter["selected"]) == len(owners))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=o, variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            owner_filter["window"] = dropdown
            owner_filter["btn"].configure(text="👤 Owner ✓")
        
        # Create filter buttons
        activity_filter["btn"] = ctk.CTkButton(filter_container, text="📋 Activity", font=(FONT_FAMILY, 10, "bold"),
                                               fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                               command=toggle_activity_filter)
        activity_filter["btn"].pack(side="left", padx=3)
        
        validation_filter["btn"] = ctk.CTkButton(filter_container, text="✓ Validation", font=(FONT_FAMILY, 10, "bold"),
                                                 fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                                 command=toggle_validation_filter)
        validation_filter["btn"].pack(side="left", padx=3)
        
        status_filter["btn"] = ctk.CTkButton(filter_container, text="⬜ Status", font=(FONT_FAMILY, 10, "bold"),
                                             fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                             command=toggle_status_filter)
        status_filter["btn"].pack(side="left", padx=3)
        
        owner_filter["btn"] = ctk.CTkButton(filter_container, text="👤 Owner", font=(FONT_FAMILY, 10, "bold"),
                                            fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                            command=toggle_owner_filter)
        owner_filter["btn"].pack(side="left", padx=3)
        
        render_activity_summary_box()
        render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
        _persist_filters()

    def fetch_all_team_data():
        active_team = config.get("selected_team", "All Teams")
        tracked_users = [u for u, status in config.get("members", {}).items() if status != "HIDDEN_NO_TICKETS"]
        # Clear before each run — prevents duplicates if window is reloaded
        for lane in raw_aggregated_tickets: raw_aggregated_tickets[lane].clear()
        available_activities.clear()
        for user in tracked_users:
            if cancelled["value"]: return
            try:
                result = get_jira_data(config, user, force_team_filter=active_team)
                if not result or not result[0]: continue
                for lane in ["todo", "in_progress", "approved", "blocked", "partially_blocked", "done"]:
                    for tick in result[0].get(lane, []):
                        extended = list(tick)
                        while len(extended) < 11: extended.append(0)
                        extended.append(user)
                        raw_aggregated_tickets[lane].append(extended)
                        if extended[5] != "N/A": available_activities.add(str(extended[5]).strip().upper())
            except Exception: continue
        if not cancelled["value"]:
            all_keys = [t[0] for lane in raw_aggregated_tickets.values() for t in lane]
            # Evict stale False entries before warming — catches mid-sprint
            # checklist additions and newly-added sprint tickets alike.
            with _checklist_cache_lock:
                stale = [k for k, v in _checklist_cache.items() if not v]
                for k in stale:
                    del _checklist_cache[k]
            _warm_checklist_cache(config, all_keys)
        if not cancelled["value"]:
            win.after(0, initialize_ui_views)

    def cleanup_window():
        cancelled["value"] = True
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", cleanup_window)
    threading.Thread(target=fetch_all_team_data, daemon=True).start()

def open_teams_overview_dashboard(config):
    win = ctk.CTkToplevel()
    win.geometry("1150x920")
    win.title("Teams Overview — Sprint Tickets by Team")
    win.configure(fg_color=APP_BG)
    win.attributes('-topmost', True)
    win.grid_columnconfigure(0, weight=1)
    win.grid_rowconfigure(0, weight=0)  # top bar
    win.grid_rowconfigure(1, weight=0)  # member logging section
    win.grid_rowconfigure(2, weight=0)  # activity summary
    win.grid_rowconfigure(3, weight=1)  # ticket lanes

    # ── shared state ──────────────────────────────────────────────────────
    cancelled            = {"value": False}
    raw_aggregated       = {"todo": [], "in_progress": [], "approved": [], "blocked": [], "partially_blocked": [], "done": []}
    available_activities = set()
    show_summary         = {"value": True}
    selectable_teams     = [t for t in get_teams_list(config) if t != "All Teams"]
    current_team         = {"value": selectable_teams[0] if selectable_teams else ""}

    # ── top bar ───────────────────────────────────────────────────────────
    top_bar = ctk.CTkFrame(win, fg_color=NAV_BG, height=60, corner_radius=8)
    top_bar.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 10))
    top_bar.pack_propagate(False)

    left_frame = ctk.CTkFrame(top_bar, fg_color="transparent")
    left_frame.pack(side="left", padx=10)
    ctk.CTkLabel(left_frame, text="🏢 Teams Overview",
                 font=(FONT_FAMILY, 16, "bold"), text_color=TEXT_MAIN).pack(side="left", padx=(10, 20))
    ctk.CTkLabel(left_frame, text="Team:", font=(FONT_FAMILY, 12, "bold"),
                 text_color=TEXT_MUTED).pack(side="left", padx=(0, 6))

    def on_team_select(team_name):
        current_team["value"] = team_name
        fetch_for_team(team_name)

    team_drop = ctk.CTkOptionMenu(
        left_frame,
        values=selectable_teams if selectable_teams else ["—"],
        width=160,
        fg_color=ACCENT_BLUE, button_color=ACCENT_BLUE, button_hover_color=ACCENT_HOVER,
        font=(FONT_FAMILY, 13, "bold"),
        command=on_team_select
    )
    team_drop.set(current_team["value"])
    team_drop.pack(side="left")

    filter_container = ctk.CTkFrame(top_bar, fg_color="transparent")
    filter_container.pack(side="right", padx=20)

    def toggle_summary():
        if show_summary["value"]:
            summary_container.grid_remove()
            toggle_btn.configure(text="📊 Show Summary")
        else:
            summary_container.grid()
            render_activity_summary_box()
            toggle_btn.configure(text="📊 Hide Summary")
        show_summary["value"] = not show_summary["value"]

    toggle_btn = ctk.CTkButton(top_bar, text="📊 Hide Summary", width=130, height=28,
                                font=(FONT_FAMILY, 12, "bold"),
                                fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER,
                                command=toggle_summary)
    toggle_btn.pack(side="right", padx=(0, 10))

    # ── row 1 : member logging cards ─────────────────────────────────────
    member_section = ctk.CTkFrame(win, fg_color="transparent")
    member_section.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 6))

    member_header = ctk.CTkFrame(member_section, fg_color="transparent")
    member_header.pack(fill="x", pady=(4, 4))
    ctk.CTkLabel(member_header, text="👥 Team Member Logging Status",
                 font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=4)
    member_loading_lbl = ctk.CTkLabel(member_header, text="",
                                       font=(FONT_FAMILY, 11, "italic"), text_color=TEXT_MUTED)
    member_loading_lbl.pack(side="left", padx=10)

    member_scroll = ctk.CTkScrollableFrame(member_section, height=160,
                                            orientation="horizontal", fg_color="transparent")
    member_scroll.pack(fill="x")

    # ── row 2 : activity summary ──────────────────────────────────────────
    summary_container = ctk.CTkFrame(win, fg_color="transparent")
    summary_container.grid(row=2, column=0, sticky="ew", padx=20)
    summary_frame = ctk.CTkFrame(summary_container, fg_color=CARD_BG,
                                  corner_radius=8, border_width=1, border_color=BORDER_COLOR)
    summary_frame.pack(fill="x")

    # ── row 3 : scrollable ticket lanes ──────────────────────────────────
    scroll_container = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll_container.grid(row=3, column=0, sticky="nsew", padx=20, pady=10)

    # ── helpers ───────────────────────────────────────────────────────────
    def parse_time_str_to_seconds(time_str):
        if not time_str or time_str in ("0m", "0h"): return 0
        try:
            seconds = 0
            for p in str(time_str).split():
                if 'd' in p: seconds += int(p.replace('d', '')) * HOURS_PER_DAY * 3600
                elif 'h' in p: seconds += int(p.replace('h', '')) * 3600
                elif 'm' in p: seconds += int(p.replace('m', '')) * 60
            return seconds
        except Exception: return 0

    # ── render member logging cards ───────────────────────────────────────
    def render_member_logging(member_logged, sprint):
        for child in member_scroll.winfo_children(): child.destroy()
        member_loading_lbl.configure(text="")

        if not member_logged:
            ctk.CTkLabel(member_scroll, text="No members found for this team.",
                         font=(FONT_FAMILY, 11, "italic"), text_color=TEXT_MUTED).pack(padx=10, pady=10)
            return

        holiday_seconds = calculate_holiday_subtraction_seconds(
            sprint["startDate"] if sprint else None,
            config.get("holiday_dates", [])
        )
        base_rt  = calculate_expected_hours(sprint["startDate"])  * 3600 if sprint else 0
        base_j1  = calculate_expected_hours_j1(sprint["startDate"]) * 3600 if sprint else 0
        expected_rt  = max(0, base_rt  - holiday_seconds)
        expected_j1  = max(0, base_j1  - holiday_seconds)

        for member_name, logged_sec in sorted(member_logged.items()):
            # ── Real-time badge ───────────────────────────────────────────
            if logged_sec >= expected_rt:
                rt_txt   = "⏱ Real-time: Great Job! 🚀"
                rt_color = "#065f46"
            else:
                missing_rt = expected_rt - logged_sec
                rt_txt   = f"⏱ Missing: {format_time(missing_rt)}"
                rt_color = "#7f1d1d"

            # ── J-1 badge ─────────────────────────────────────────────────
            if logged_sec >= expected_j1:
                j1_txt   = "📅 J-1: On Track ✅"
                j1_color = "#065f46"
            else:
                missing_j1 = expected_j1 - logged_sec
                j1_txt   = f"📅 J-1 Missing: {format_time(missing_j1)}"
                j1_color = "#78350f"

            # ── top strip colour ─────────────────────────────────────────
            strip_color = "#3DB060" if logged_sec >= expected_rt else (
                "#D9A520" if logged_sec >= expected_j1 else "#E5534B"
            )

            card = ctk.CTkFrame(member_scroll, width=210, height=148,
                                 fg_color=CARD_BG, border_width=1,
                                 border_color=BORDER_COLOR, corner_radius=10)
            card.pack(side="left", padx=8, pady=4)
            card.pack_propagate(False)

            # coloured top strip
            ctk.CTkFrame(card, height=4, fg_color=strip_color,
                          corner_radius=0).pack(fill="x")

            # name + logged time
            name_row = ctk.CTkFrame(card, fg_color="transparent")
            name_row.pack(fill="x", padx=12, pady=(8, 2))
            ctk.CTkLabel(name_row, text="👤", font=(FONT_FAMILY, 14)).pack(side="left", padx=(0, 6))
            name_col = ctk.CTkFrame(name_row, fg_color="transparent")
            name_col.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(name_col, text=member_name, font=(FONT_FAMILY, 12, "bold"),
                         text_color=TEXT_MAIN, anchor="w").pack(fill="x")
            ctk.CTkLabel(name_col, text=f"Logged: {format_time(logged_sec)}",
                         font=(FONT_FAMILY, 10), text_color=TEXT_MUTED, anchor="w").pack(fill="x")

            # real-time badge
            rt_badge = ctk.CTkFrame(card, fg_color=rt_color, corner_radius=5, height=26)
            rt_badge.pack(fill="x", padx=12, pady=(6, 3))
            rt_badge.pack_propagate(False)
            ctk.CTkLabel(rt_badge, text=rt_txt, font=(FONT_FAMILY, 10, "bold"),
                         text_color="white").pack(expand=True)

            # J-1 badge
            j1_badge = ctk.CTkFrame(card, fg_color=j1_color, corner_radius=5, height=26)
            j1_badge.pack(fill="x", padx=12, pady=(0, 6))
            j1_badge.pack_propagate(False)
            ctk.CTkLabel(j1_badge, text=j1_txt, font=(FONT_FAMILY, 10, "bold"),
                         text_color="white").pack(expand=True)

    # ── render activity summary ───────────────────────────────────────────
    def render_activity_summary_box():
        for child in summary_frame.winfo_children(): child.destroy()
        ctk.CTkLabel(summary_frame,
                     text=f"📊 Total Logged & Estimated Time per Activity & Validation  —  {current_team['value']}",
                     font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MUTED).pack(anchor="w", padx=15, pady=(8, 4))
        combined_totals = {}
        for lane in ["todo", "in_progress", "approved", "blocked", "partially_blocked", "done"]:
            for item in raw_aggregated[lane]:
                act_lbl = str(item[5]).strip().upper() if len(item) > 5 else "N/A"
                val_lbl = str(item[6]).strip()         if len(item) > 6 else "N/A"
                if act_lbl == "N/A": continue
                logged_sec = parse_time_str_to_seconds(item[3])
                est_sec    = parse_time_str_to_seconds(item[8])  # index 8 = est_time (not index 4 = remaining)
                # achieved fields at indices 9, 10, 11 (owner at 7, est_time at 8)
                ach_tcs     = item[9]  if len(item) > 9  and isinstance(item[9],  int) else 0
                ach_reqs    = item[10] if len(item) > 10 and isinstance(item[10], int) else 0
                ach_tickets = item[11] if len(item) > 11 and isinstance(item[11], int) else 0
                combo_key  = (act_lbl, val_lbl)
                if combo_key not in combined_totals:
                    combined_totals[combo_key] = {"logged": 0, "estimated": 0, "achieved": 0, "ach_reqs": 0, "ach_tcs": 0, "ach_tickets": 0}
                combined_totals[combo_key]["logged"]    += logged_sec
                combined_totals[combo_key]["estimated"] += est_sec
                combined_totals[combo_key]["achieved"]  += ach_tcs + ach_reqs + ach_tickets
                combined_totals[combo_key]["ach_reqs"]    += ach_reqs
                combined_totals[combo_key]["ach_tcs"]     += ach_tcs
                combined_totals[combo_key]["ach_tickets"] += ach_tickets
        if not combined_totals:
            ctk.CTkLabel(summary_frame, text="No activities found for this team.",
                         font=(FONT_FAMILY, 12, "italic"), text_color=TEXT_MUTED).pack(anchor="w", padx=15, pady=8)
            return
        badge_container = ctk.CTkScrollableFrame(summary_frame, fg_color="transparent", height=220)
        badge_container.pack(fill="x", padx=15, pady=(0, 10))
        for (act, val), tots in sorted(combined_totals.items()):
            badge = ctk.CTkFrame(badge_container, fg_color=BADGE_BG, corner_radius=6,
                                  border_width=1, border_color=BORDER_COLOR)
            badge.pack(fill="x", pady=3, anchor="w")
            display_name = f"{act} ({val})" if val and val != "N/A" else act
            ctk.CTkLabel(badge, text=f"{display_name}:", font=(FONT_FAMILY, 11, "bold"),
                         text_color=ACCENT_BLUE).pack(side="left", padx=(10, 2), pady=6)
            ctk.CTkLabel(badge, text=f"Estimated: {format_time(tots['estimated'])}", font=(FONT_FAMILY, 11, "bold"),
                         text_color=TEXT_MUTED).pack(side="left", padx=(2, 5), pady=6)
            ctk.CTkLabel(badge, text=f"Logged: {format_time(tots['logged'])}", font=(FONT_FAMILY, 11, "bold"),
                         text_color="#34d399").pack(side="left", padx=(2, 5), pady=6)
            ach_summary_text = f"Achieved: {tots['achieved']} (Req {tots['ach_reqs']} || TC {tots['ach_tcs']} || Ticket {tots['ach_tickets']})"
            ctk.CTkLabel(badge, text=ach_summary_text, font=(FONT_FAMILY, 11, "bold"),
                         text_color="#ecb80c").pack(side="left", padx=(2, 10), pady=6)

    def render_filtered_lanes(act_f, val_f, stat_f, own_f):
        for child in scroll_container.winfo_children(): child.destroy()
        swimlanes = [
            ("🟥 Team To Do / Open",   "todo",        "#f87171"),
            ("🟧 Team In Progress",    "in_progress", "#fb923c"),
            ("🟦 Team Approved",       "approved",    "#60a5fa"),
            ("⛔ Team Blocked",        "blocked",     "#dc2626"),
            ("🟡 Team Partially Blocked", "partially_blocked", "#eab308"),
            ("🟩 Team Done / Resolved","done",        "#34d399"),
        ]
        for title, key, heading_color in swimlanes:
            # Check status filter (handle both string and set)
            if isinstance(stat_f, set):
                stat_match = (("TODO" in stat_f and key == "todo") or
                             ("IN_PROGRESS" in stat_f and key == "in_progress") or
                             ("APPROVED" in stat_f and key == "approved") or
                             ("BLOCKED" in stat_f and key == "blocked") or
                             ("PARTIALLY_BLOCKED" in stat_f and key == "partially_blocked") or
                             ("DONE" in stat_f and key == "done"))
            else:
                stat_match = stat_f == "ALL STATUSES" or key == stat_f.lower()
            
            if not stat_match: continue
            
            # Filter items by activity, validation and owner
            lane_items = []
            for t in raw_aggregated[key]:
                act_match = (len(act_f) == 0 or str(t[5]).strip().upper() in act_f) if isinstance(act_f, set) else (act_f == "ALL ACTIVITIES" or str(t[5]).strip().upper() == act_f)
                val_match = (len(val_f) == 0 or str(t[6]).strip().upper() in val_f) if isinstance(val_f, set) else (val_f == "ALL VALIDATIONS" or str(t[6]).strip().upper() == val_f.upper())
                t_owner = str(t[7]).strip() if len(t) > 7 else "Unknown"
                own_match = (len(own_f) == 0 or t_owner in own_f) if isinstance(own_f, set) else (own_f == "ALL OWNERS" or t_owner == own_f)
                if act_match and val_match and own_match:
                    lane_items.append(t)
            
            section_frame = ctk.CTkFrame(scroll_container, fg_color=CARD_BG, corner_radius=10,
                                          border_width=1, border_color=BORDER_COLOR)
            section_frame.pack(fill="x", pady=10, ipady=5)
            ctk.CTkLabel(section_frame, text=f"{title} ({len(lane_items)} tickets)",
                         font=(FONT_FAMILY, 14, "bold"), text_color=heading_color).pack(anchor="w", padx=15, pady=12)
            if not lane_items: continue
            for item in lane_items:
                t_key, sumry, link, sp, rm, act_lbl, val_lbl, owner = \
                    item[0], item[1], item[2], item[3], item[4], item[5], item[6], item[7]
                estimated_time   = item[8]  if len(item) > 8  else "0h"
                achieved_tcs     = item[9]  if len(item) > 9  else 0
                achieved_reqs    = item[10] if len(item) > 10 else 0
                achieved_tickets = item[11] if len(item) > 11 else 0

                # Achievement = sum of Achieved TCs + Achieved Reqs + Achieved Tickets
                achievement_val = (achieved_tcs     if isinstance(achieved_tcs,     int) else 0) + \
                                  (achieved_reqs    if isinstance(achieved_reqs,    int) else 0) + \
                                  (achieved_tickets if isinstance(achieved_tickets, int) else 0)
                
                item_box = ctk.CTkFrame(section_frame, fg_color=ITEM_BG, corner_radius=6,
                                         border_width=1, border_color=BORDER_COLOR)
                item_box.pack(fill="x", padx=15, pady=4)
                
                # Right side packed FIRST: Owner | Achievement | Logged | Est | Activity
                ctk.CTkLabel(item_box, text=f"👤 {owner}", font=(FONT_FAMILY, 11, "bold"),
                             text_color=ACCENT_BLUE, width=100, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Achievement — only shown on the ticket row when it actually has a non-zero achievement;
                # the aggregated summary box above still counts every ticket regardless.
                if achievement_val != 0:
                    ach_color = "#34d399" if achievement_val > 0 else TEXT_MUTED
                    ach_text = f"✓ {achievement_val} (Req {achieved_reqs} TC {achieved_tcs} Ticket {achieved_tickets})"
                    ctk.CTkLabel(item_box, text=ach_text, font=(FONT_FAMILY, 11, "bold"),
                                 text_color=ach_color, width=220, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Logged Time
                ctk.CTkLabel(item_box, text=sp if sp else "0h", font=(FONT_FAMILY, 11, "bold"),
                             text_color="#34d399", width=60, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Est Time
                ctk.CTkLabel(item_box, text=estimated_time, font=(FONT_FAMILY, 11),
                             text_color=TEXT_MUTED, width=60, anchor="e").pack(side="right", padx=8, pady=8)
                
                # Activity
                badge_text = str(act_lbl).upper() if act_lbl != "N/A" else "—"
                if val_lbl and val_lbl != "N/A":
                    badge_text += f"|{val_lbl}"
                ctk.CTkLabel(item_box, text=badge_text, font=(FONT_FAMILY, 10, "bold"),
                             text_color=ACCENT_BLUE if act_lbl != "N/A" else TEXT_MUTED, width=80, anchor="e").pack(side="right", padx=8, pady=8)
                
                # To Do List button — opens this ticket's native description checklist
                # Only shown when the ticket actually has a to-do / checklist
                if _ticket_has_checklist(config, t_key):
                    ctk.CTkButton(item_box, text="📋", width=30, height=26, font=(FONT_FAMILY, 12, "bold"),
                                  fg_color=BTN_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN,
                                  command=lambda k=t_key, l=link: open_ticket_checklist_window(config, win, k, l)
                                  ).pack(side="right", padx=(8, 2), pady=8)
                
                # Title packed LAST so it fills remaining space without pushing right elements off screen
                lbl_title = ctk.CTkLabel(item_box, text=f"[{t_key}] {sumry}", text_color=TEXT_MAIN,
                                          font=(FONT_FAMILY, 12, "bold"), cursor="hand2", justify="left", anchor="w", wraplength=0)
                lbl_title.pack(side="left", fill="x", expand=True, padx=12, pady=8)
                lbl_title.bind("<Button-1>", lambda e, url=link: webbrowser.open(url))

    def initialize_ui_views():
        if cancelled["value"]: return
        activities  = sorted(list(available_activities))
        # Include N/A in validations list
        validations = sorted(list({
            str(t[6]).strip().upper()
            for lane in raw_aggregated.values()
            for t in lane if len(t) > 6
        }))
        owners = sorted(list({
            str(t[7]).strip()
            for lane in raw_aggregated.values()
            for t in lane if len(t) > 7
        }))
        for child in filter_container.winfo_children(): child.destroy()
        
        # Filter states — restore last-used selections if previously saved.
        # Owner is the exception: owners differ per team, so persisting a
        # selection across team switches can leave stale names checked that
        # don't exist on the newly-selected team (silently filtering out
        # everything). Owner therefore always starts fully selected on every
        # fresh load/team switch.
        activity_filter = {"selected": _load_saved_filter_selection(config, "teams_overview", "activity", activities), "btn": None, "window": None}
        validation_filter = {"selected": _load_saved_filter_selection(config, "teams_overview", "validation", validations), "btn": None, "window": None}
        status_filter = {"selected": _load_saved_filter_selection(config, "teams_overview", "status", ALL_STATUS_VALUES), "btn": None, "window": None}
        owner_filter = {"selected": set(owners), "btn": None, "window": None}
        
        def _persist_filters():
            _save_filter_selection(config, "teams_overview", activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
        
        def toggle_activity_filter():
            if activity_filter["window"] and activity_filter["window"].winfo_exists():
                activity_filter["window"].destroy()
                activity_filter["window"] = None
                activity_filter["btn"].configure(text="📋 Activity")
            else:
                open_activity_dropdown()
        
        def open_activity_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x400")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = activity_filter["btn"].winfo_rootx()
                btn_y = activity_filter["btn"].winfo_rooty()
                btn_height = activity_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            act_vars = {}
            select_all_var = ctk.BooleanVar(value=len(activity_filter["selected"]) == len(activities))
            
            def toggle_all_activities():
                if select_all_var.get():
                    activity_filter["selected"] = set(activities)
                else:
                    activity_filter["selected"] = set()
                for var in act_vars.values(): var.set(False)
                for act in activity_filter["selected"]:
                    if act in act_vars: act_vars[act].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_activities,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for act in activities:
                var = ctk.BooleanVar(value=act in activity_filter["selected"])
                act_vars[act] = var
                def on_change(a=act, v=var):
                    if v.get(): activity_filter["selected"].add(a)
                    else: activity_filter["selected"].discard(a)
                    select_all_var.set(len(activity_filter["selected"]) == len(activities))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=act, variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            activity_filter["window"] = dropdown
            activity_filter["btn"].configure(text="📋 Activity ✓")
        
        def toggle_validation_filter():
            if validation_filter["window"] and validation_filter["window"].winfo_exists():
                validation_filter["window"].destroy()
                validation_filter["window"] = None
                validation_filter["btn"].configure(text="✓ Validation")
            else:
                open_validation_dropdown()
        
        def open_validation_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x400")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = validation_filter["btn"].winfo_rootx()
                btn_y = validation_filter["btn"].winfo_rooty()
                btn_height = validation_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            val_vars = {}
            select_all_var = ctk.BooleanVar(value=len(validation_filter["selected"]) == len(validations))
            
            def toggle_all_validations():
                if select_all_var.get():
                    validation_filter["selected"] = set(validations)
                else:
                    validation_filter["selected"] = set()
                for var in val_vars.values(): var.set(False)
                for val in validation_filter["selected"]:
                    if val in val_vars: val_vars[val].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_validations,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for val in validations:
                var = ctk.BooleanVar(value=val in validation_filter["selected"])
                val_vars[val] = var
                def on_change(v=val, var=var):
                    if var.get(): validation_filter["selected"].add(v)
                    else: validation_filter["selected"].discard(v)
                    select_all_var.set(len(validation_filter["selected"]) == len(validations))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=val, variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            validation_filter["window"] = dropdown
            validation_filter["btn"].configure(text="✓ Validation ✓")
        
        def toggle_status_filter():
            if status_filter["window"] and status_filter["window"].winfo_exists():
                status_filter["window"].destroy()
                status_filter["window"] = None
                status_filter["btn"].configure(text="⬜ Status")
            else:
                open_status_dropdown()
        
        def open_status_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x200")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = status_filter["btn"].winfo_rootx()
                btn_y = status_filter["btn"].winfo_rooty()
                btn_height = status_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            stat_vars = {}
            all_statuses = {"TODO", "IN_PROGRESS", "APPROVED", "BLOCKED", "PARTIALLY_BLOCKED", "DONE"}
            select_all_var = ctk.BooleanVar(value=len(status_filter["selected"]) == len(all_statuses))
            
            def toggle_all_statuses():
                if select_all_var.get():
                    status_filter["selected"] = set(all_statuses)
                else:
                    status_filter["selected"] = set()
                for var in stat_vars.values(): var.set(False)
                for stat in status_filter["selected"]:
                    if stat in stat_vars: stat_vars[stat].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_statuses,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for stat in all_statuses:
                var = ctk.BooleanVar(value=stat in status_filter["selected"])
                stat_vars[stat] = var
                def on_change(s=stat, var=var):
                    if var.get(): status_filter["selected"].add(s)
                    else: status_filter["selected"].discard(s)
                    select_all_var.set(len(status_filter["selected"]) == len(all_statuses))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=stat.replace("_", " "), variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            status_filter["window"] = dropdown
            status_filter["btn"].configure(text="⬜ Status ✓")
        
        def toggle_owner_filter():
            if owner_filter["window"] and owner_filter["window"].winfo_exists():
                owner_filter["window"].destroy()
                owner_filter["window"] = None
                owner_filter["btn"].configure(text="👤 Owner")
            else:
                open_owner_dropdown()
        
        def open_owner_dropdown():
            dropdown = ctk.CTkToplevel(win)
            dropdown.transient(win)  # Make dropdown a child of current window
            dropdown.geometry("220x400")
            dropdown.configure(fg_color=APP_BG)
            dropdown.attributes('-topmost', True)
            dropdown.attributes('-toolwindow', True)
            dropdown.lift()
            dropdown.focus_force()
            try:
                btn_x = owner_filter["btn"].winfo_rootx()
                btn_y = owner_filter["btn"].winfo_rooty()
                btn_height = owner_filter["btn"].winfo_height()
                dropdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
            except: pass
            
            container = ctk.CTkFrame(dropdown, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER_COLOR)
            container.pack(fill="both", expand=True, padx=1, pady=1)
            scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG)
            scroll.pack(fill="both", expand=True)
            
            own_vars = {}
            select_all_var = ctk.BooleanVar(value=len(owner_filter["selected"]) == len(owners))
            
            def toggle_all_owners():
                if select_all_var.get():
                    owner_filter["selected"] = set(owners)
                else:
                    owner_filter["selected"] = set()
                for var in own_vars.values(): var.set(False)
                for o in owner_filter["selected"]:
                    if o in own_vars: own_vars[o].set(True)
                render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                _persist_filters()
            
            ctk.CTkCheckBox(scroll, text="All", variable=select_all_var, command=toggle_all_owners,
                           font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE,
                           checkbox_width=12, checkbox_height=12, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=10, pady=(6, 3))
            
            for o in owners:
                var = ctk.BooleanVar(value=o in owner_filter["selected"])
                own_vars[o] = var
                def on_change(o=o, var=var):
                    if var.get(): owner_filter["selected"].add(o)
                    else: owner_filter["selected"].discard(o)
                    select_all_var.set(len(owner_filter["selected"]) == len(owners))
                    render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
                    _persist_filters()
                ctk.CTkCheckBox(scroll, text=o, variable=var, command=on_change,
                               font=(FONT_FAMILY, 9), text_color=TEXT_MAIN,
                               checkbox_width=11, checkbox_height=11, border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=18, pady=0)
            
            owner_filter["window"] = dropdown
            owner_filter["btn"].configure(text="👤 Owner ✓")
        
        # Create filter buttons
        activity_filter["btn"] = ctk.CTkButton(filter_container, text="📋 Activity", font=(FONT_FAMILY, 10, "bold"),
                                               fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                               command=toggle_activity_filter)
        activity_filter["btn"].pack(side="left", padx=3)
        
        validation_filter["btn"] = ctk.CTkButton(filter_container, text="✓ Validation", font=(FONT_FAMILY, 10, "bold"),
                                                 fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                                 command=toggle_validation_filter)
        validation_filter["btn"].pack(side="left", padx=3)
        
        status_filter["btn"] = ctk.CTkButton(filter_container, text="⬜ Status", font=(FONT_FAMILY, 10, "bold"),
                                             fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                             command=toggle_status_filter)
        status_filter["btn"].pack(side="left", padx=3)
        
        owner_filter["btn"] = ctk.CTkButton(filter_container, text="👤 Owner", font=(FONT_FAMILY, 10, "bold"),
                                            fg_color=CARD_BG, hover_color=BTN_HOVER, height=24, width=100,
                                            command=toggle_owner_filter)
        owner_filter["btn"].pack(side="left", padx=3)
        
        render_activity_summary_box()
        render_filtered_lanes(activity_filter["selected"], validation_filter["selected"], status_filter["selected"], owner_filter["selected"])
        _persist_filters()

    def fetch_for_team(team_name):
        # reset ticket state
        for lane in raw_aggregated: raw_aggregated[lane].clear()
        available_activities.clear()

        # clear UI + show loading indicators
        for child in scroll_container.winfo_children(): child.destroy()
        for child in summary_frame.winfo_children(): child.destroy()
        for child in member_scroll.winfo_children(): child.destroy()
        member_loading_lbl.configure(text=f"⏳ Loading members for {team_name}…")
        ctk.CTkLabel(scroll_container,
                     text=f"⏳ Fetching Jira tickets for team: {team_name}…",
                     font=(FONT_FAMILY, 14, "italic"), text_color=TEXT_MUTED).pack(pady=40)

        def do_fetch():
            if cancelled["value"]: return
            try:
                # get_current_sprint and the team data fetch are independent
                # Jira calls, so run them in parallel rather than sequentially.
                sprint_result = [None]
                fetch_result  = [None]
                errors        = []

                def fetch_sprint():
                    try:
                        sprint_result[0] = get_current_sprint(config)
                    except Exception as e:
                        errors.append(e)

                def fetch_team_data():
                    try:
                        # Single combined fetch: one Jira search + one worklog
                        # resolve pass instead of two separate full fetches.
                        fetch_result[0] = get_team_overview_and_logging(config, team_name)
                    except Exception as e:
                        errors.append(e)

                t1 = threading.Thread(target=fetch_sprint,     daemon=True)
                t2 = threading.Thread(target=fetch_team_data,  daemon=True)
                t1.start(); t2.start()
                t1.join();  t2.join()

                if cancelled["value"]: return

                sprint = sprint_result[0]
                data, totals, total_logged, member_logged = fetch_result[0] or (None, None, None, {})

                # populate ticket data
                if data:
                    for lane in ["todo", "in_progress", "approved", "blocked", "partially_blocked", "done"]:
                        for tick in data.get(lane, []):
                            raw_aggregated[lane].append(list(tick))
                            if len(tick) > 5 and tick[5] != "N/A":
                                available_activities.add(str(tick[5]).strip().upper())
                    all_keys = [t[0] for lane in raw_aggregated.values() for t in lane]
                    # Evict stale False entries before warming — catches mid-sprint
                    # checklist additions and newly-added sprint tickets alike.
                    with _checklist_cache_lock:
                        stale = [k for k, v in _checklist_cache.items() if not v]
                        for k in stale:
                            del _checklist_cache[k]
                    _warm_checklist_cache(config, all_keys)
                    win.after(0, initialize_ui_views)
                else:
                    win.after(0, lambda: _show_no_results(team_name))

                # render member cards (even if ticket fetch failed)
                member_logged = member_logged or {}
                win.after(0, lambda ml=member_logged, sp=sprint: render_member_logging(ml, sp))

            except Exception as e:
                print(f"[fetch_for_team] {e}")
                if not cancelled["value"]:
                    win.after(0, lambda: _show_no_results(team_name))
                    win.after(0, lambda: member_loading_lbl.configure(text="⚠️ Could not load member data"))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _show_no_results(team_name):
        for child in scroll_container.winfo_children(): child.destroy()
        ctk.CTkLabel(scroll_container,
                     text=f"No tickets found for team: {team_name}",
                     font=(FONT_FAMILY, 14, "italic"), text_color=TEXT_MUTED).pack(pady=40)

    def cleanup_window():
        cancelled["value"] = True
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", cleanup_window)

    if current_team["value"]:
        fetch_for_team(current_team["value"])

    def _apply_refreshed_team_list():
        """Called once the background team-list refresh below finds a change."""
        if cancelled["value"]:
            return
        new_selectable = [t for t in get_teams_list(config) if t != "All Teams"]
        if not new_selectable:
            return
        team_drop.configure(values=new_selectable)
        # If the team we were viewing no longer exists under its old name,
        # fall back to the first available team instead of silently showing
        # a now-invalid selection.
        if current_team["value"] not in new_selectable:
            current_team["value"] = new_selectable[0]
            team_drop.set(current_team["value"])
            fetch_for_team(current_team["value"])

    # Re-pull the real team list every time this window opens, so renamed/
    # added/removed teams in Jira show up here without restarting the app.
    _refresh_teams_list_async(config, on_done=lambda: win.after(0, _apply_refreshed_team_list))




def run_dashboard(config):
    ctk.set_appearance_mode(config.get("theme", "Dark"))
    app = ctk.CTk()
    app.geometry("1250x920") 
    app.title("Logging Reminder Dashboard")
    app.configure(fg_color=APP_BG)
    start_scheduler(app)

    # Refresh the dynamic team list in the background as soon as the app opens,
    # so by the time the user opens any team dropdown it already reflects
    # whatever team names Jira actually has right now (not a stale snapshot).
    _refresh_teams_list_async(config)
    
    session_status = {"logout": False}
    sprint_visible = {"value": True}

    def toggle_theme():
        current_mode = ctk.get_appearance_mode()
        new_mode = "Light" if current_mode == "Dark" else "Dark"
        ctk.set_appearance_mode(new_mode)
        config["theme"] = new_mode
        save_config(config)

    app.active_calendar_instance = None
    app.active_calendar_target = None
    active_card_widgets = {}
    
    # Navigation Bar
    navbar = ctk.CTkFrame(app, fg_color=NAV_BG, height=70, corner_radius=0)
    navbar.pack(fill="x", side="top", pady=(0, 20))
    navbar.pack_propagate(False)
    
    ctk.CTkLabel(navbar, text="🕒 Logging Monitor", font=(FONT_FAMILY, 22, "bold"), text_color=TEXT_MAIN).pack(side="left", padx=25)

    sprint_name_lbl = ctk.CTkLabel(navbar, text="⏳ Loading sprint...", font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MUTED)
    sprint_name_lbl.pack(side="left", padx=(0, 20))

    def load_sprint_name():
        sprint = get_current_sprint(config)
        name = sprint.get("name", "Unknown Sprint") if sprint else "No Active Sprint"
        app.after(0, lambda: sprint_name_lbl.configure(
            text=f"🏃 {name}",
            text_color=TEXT_MAIN if sprint else TEXT_MUTED
        ))
    threading.Thread(target=load_sprint_name, daemon=True).start()

    controls_frame = ctk.CTkFrame(navbar, fg_color="transparent")
    controls_frame.pack(side="right", padx=25)
    ctk.CTkButton(controls_frame, text="DARK-LIGHT", width=40, fg_color=CARD_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN, border_width=1, border_color=BORDER_COLOR, font=(FONT_FAMILY, 12, "bold"), command=toggle_theme).pack(side="right", padx=(15, 0))
    
    def perform_logout():
        clear_config()
        session_status["logout"] = True
        app.destroy()

    ctk.CTkButton(controls_frame, text="Logout", fg_color="transparent", text_color="#ef4444", border_width=1, border_color="#ef4444", hover_color="#311212", width=80, font=(FONT_FAMILY, 12, "bold"), command=perform_logout).pack(side="right", padx=(15, 0))

    # --- SPRINT CONTAINER ---
    sprint_container = ctk.CTkFrame(app, fg_color="transparent")
    sprint_container.pack(fill="x", padx=20, pady=5)

    def toggle_sprint_section():
        if sprint_visible["value"]:
            saved_scroll.pack_forget()
            toggle_sprint_btn.configure(text="Show")
        else:
            saved_scroll.pack(fill="x", padx=0, pady=(0, 5))
            toggle_sprint_btn.configure(text="Hide")
        sprint_visible["value"] = not sprint_visible["value"]
    
    header_row = ctk.CTkFrame(sprint_container, fg_color="transparent")
    header_row.pack(fill="x", anchor="w", padx=10)
    ctk.CTkLabel(header_row, text="👥 MY TEAM", font=(FONT_FAMILY, 12, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=10)
    toggle_sprint_btn = ctk.CTkButton(header_row, text="Hide", width=60, height=25, font=(FONT_FAMILY, 10, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, command=toggle_sprint_section)
    toggle_sprint_btn.pack(side="left", padx=5)

    entry = ctk.CTkEntry(header_row, placeholder_text="🔍 Workspace username...", width=180, height=25, fg_color=ITEM_BG, border_color=BORDER_COLOR, font=(FONT_FAMILY, 12), text_color=TEXT_MAIN)
    entry.pack(side="left", padx=10)

    def search_and_add():
        u = entry.get().strip()
        if not u: return
        
        pop = ctk.CTkToplevel(app)
        pop.geometry("320x180")
        pop.title("Capacity Metric")
        pop.configure(fg_color=CARD_BG)
        pop.attributes('-topmost', True)
        
        ctk.CTkLabel(pop, text=f"Dedication Load for {u}:", font=(FONT_FAMILY, 14, "bold"), text_color=TEXT_MAIN).pack(pady=15)
        ded_entry = ctk.CTkEntry(pop, width=140, fg_color=ITEM_BG, border_color=BORDER_COLOR, justify="center", text_color=TEXT_MAIN)
        ded_entry.insert(0, "100")
        ded_entry.pack(pady=5)
        
        def process_add():
            try: m_ded = float(ded_entry.get() or 100) / 100
            except: m_ded = 1.0
            pop.destroy()
            active_team = config.get("selected_team", "All Teams")
            config.setdefault("members", {})[u] = "Syncing..."
            if "team_member_dedications" not in config: config["team_member_dedications"] = {}
            if active_team not in config["team_member_dedications"]: config["team_member_dedications"][active_team] = {}
            config["team_member_dedications"][active_team][u] = m_ded
            save_config(config)
            refresh_saved_list()
            
            def fetch():
                config.setdefault("member_avatars", {})
                tickets, _, logged, avatar_url = get_jira_data(config, u, force_team_filter=active_team)
                sprint = get_current_sprint(config)
                if avatar_url and isinstance(avatar_url, str): config["member_avatars"][u] = avatar_url
                total_tickets = sum(len(tickets[cat]) for cat in tickets) if tickets else 0
                if active_team != "All Teams" and total_tickets == 0: config["members"][u] = "HIDDEN_NO_TICKETS"
                else:
                    base_expected = calculate_expected_hours(sprint["startDate"]) * 3600 if sprint else 0
                    holiday_seconds = calculate_holiday_subtraction_seconds(sprint["startDate"] if sprint else None, config.get("holiday_dates", []))
                    expected = max(0, (base_expected - holiday_seconds) * m_ded)
                    res_str = "Great Job! 🚀" if (logged is not None and logged >= expected) else format_time(expected - (logged or 0))
                    config["members"][u] = res_str
                    base_expected_j1 = calculate_expected_hours_j1(sprint["startDate"]) * 3600 if sprint else 0
                    expected_j1 = max(0, (base_expected_j1 - holiday_seconds) * m_ded)
                    config.setdefault("members_j1", {})[u] = "J-1: On Track ✅" if (logged is not None and logged >= expected_j1) else f"J-1 Missing: {format_time(expected_j1 - (logged or 0))}"
                save_config(config)
                app.after(0, lambda: refresh_saved_list())
                app.after(0, lambda: entry.delete(0, 'end'))
                
            threading.Thread(target=fetch, daemon=True).start()
            
        ctk.CTkButton(pop, text="Confirm Tracking", fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, font=(FONT_FAMILY, 13, "bold"), command=process_add, width=150).pack(pady=15)

    ctk.CTkButton(header_row, text="➕ Add", font=(FONT_FAMILY, 11, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, height=25, width=60, command=search_and_add).pack(side="left", padx=5)

    # ── TEAM FILTER (dropdown below header) - Multi-select ────────────────────
    selected_teams = {"value": set(get_teams_list(config))}  # Start with all teams selected
    team_filter_frame = {"visible": False, "frame": None}
    
    def toggle_team_filter():
        """Toggle team filter dropdown below button"""
        if team_filter_frame["visible"] and team_filter_frame["frame"] and team_filter_frame["frame"].winfo_exists():
            team_filter_frame["frame"].destroy()
            team_filter_frame["frame"] = None
            team_filter_frame["visible"] = False
            team_filter_btn.configure(text="🔍 Teams")
        else:
            open_team_filter_popdown()
    
    def open_team_filter_popdown():
        """Create dropdown below Teams button for team selection"""
        # Create a floating window positioned below the Teams button
        popdown = ctk.CTkToplevel(app)
        popdown.transient(app)  # Make dropdown a child of main window
        popdown.geometry("200x350")
        popdown.configure(fg_color=APP_BG)
        popdown.attributes('-topmost', True)  # Always on top
        popdown.attributes('-toolwindow', True)
        popdown.lift()
        popdown.focus_force()
        
        # Position directly below the Teams button
        try:
            btn_x = team_filter_btn.winfo_rootx()
            btn_y = team_filter_btn.winfo_rooty()
            btn_height = team_filter_btn.winfo_height()
            popdown.geometry(f"+{btn_x}+{btn_y + btn_height + 5}")
        except:
            pass
        
        # Container with border
        container = ctk.CTkFrame(popdown, fg_color=CARD_BG, corner_radius=8,
                                 border_width=1, border_color=BORDER_COLOR)
        container.pack(fill="both", expand=True, padx=1, pady=1)
        
        scroll = ctk.CTkScrollableFrame(container, fg_color=CARD_BG, corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        
        team_vars = {}
        
        # "Select All" checkbox
        select_all_var = ctk.BooleanVar(value=len(selected_teams["value"]) == len(get_teams_list(config)))
        
        def toggle_all():
            if select_all_var.get():
                selected_teams["value"] = set(get_teams_list(config))
            else:
                selected_teams["value"] = set()
            for team, var in team_vars.items():
                var.set(team in selected_teams["value"])
            on_teams_change()
        
        ctk.CTkCheckBox(scroll, text="Select All Teams", variable=select_all_var,
                       command=toggle_all, font=(FONT_FAMILY, 10, "bold"),
                       text_color=ACCENT_BLUE, checkbox_width=14, checkbox_height=14,
                       border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=12, pady=(8, 4))
        
        # Individual team checkboxes
        teams_to_show = sorted([t for t in get_teams_list(config) if t != "All Teams"])
        for team in teams_to_show:
            var = ctk.BooleanVar(value=team in selected_teams["value"])
            team_vars[team] = var
            
            def on_team_change(t=team, v=var):
                if v.get():
                    selected_teams["value"].add(t)
                else:
                    selected_teams["value"].discard(t)
                select_all_var.set(len(selected_teams["value"]) == len(get_teams_list(config)) - 1)
                on_teams_change()
            
            ctk.CTkCheckBox(scroll, text=team, variable=var, command=on_team_change,
                           font=(FONT_FAMILY, 10), text_color=TEXT_MAIN,
                           checkbox_width=13, checkbox_height=13, border_width=1,
                           fg_color=ACCENT_BLUE).pack(anchor="w", padx=24, pady=2)
        
        team_filter_frame["frame"] = popdown
        team_filter_frame["visible"] = True
        team_filter_btn.configure(text="🔍 Teams ✓")
        
        # Bring window to front after creation
        popdown.lift()
        popdown.focus()
    
    def on_teams_change():
        """Called when team selection changes"""
        refresh_saved_list()
        refresh_main()
    
    team_filter_btn = ctk.CTkButton(header_row, text="🔍 Teams", font=(FONT_FAMILY, 11, "bold"),
                                     fg_color=CARD_BG, hover_color=BTN_HOVER,
                                     height=25, width=85, command=toggle_team_filter)
    team_filter_btn.pack(side="left", padx=5)

    # ── ADD TEAMS BUTTON ──────────────────────────────────────────────────────
    def open_add_teams_modal():
        """Open modal to select team and add members"""
        modal = ctk.CTkToplevel(app)
        modal.geometry("500x500")
        modal.title("Add Team Members")
        modal.configure(fg_color=APP_BG)
        modal.attributes('-topmost', True)
        
        # Content frame
        content = ctk.CTkFrame(modal, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=20)
        
        ctk.CTkLabel(content, text="Select Team and Members", font=(FONT_FAMILY, 14, "bold"), text_color=TEXT_MAIN).pack(pady=(0, 16))
        
        # Team selector
        team_frame = ctk.CTkFrame(content, fg_color="transparent")
        team_frame.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(team_frame, text="Team:", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=(0, 8))
        
        selectable_teams = [t for t in get_teams_list(config) if t != "All Teams"]
        team_var = ctk.StringVar(value=selectable_teams[0] if selectable_teams else "")
        team_menu = ctk.CTkOptionMenu(team_frame, variable=team_var, values=selectable_teams,
                                       fg_color=ACCENT_BLUE, button_color=ACCENT_BLUE, button_hover_color=ACCENT_HOVER,
                                       font=(FONT_FAMILY, 11, "bold"), width=200)
        team_menu.pack(side="left")
        
        # Members list frame
        members_label = ctk.CTkLabel(content, text="Team Members:", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED)
        members_label.pack(anchor="w", pady=(8, 4))
        
        members_scroll = ctk.CTkScrollableFrame(content, fg_color=ITEM_BG, corner_radius=8)
        members_scroll.pack(fill="both", expand=True, pady=(0, 16))
        
        members_state = {"checkboxes": {}, "data": {}}
        
        def load_team_members():
            """Load and display members from selected team"""
            # Clear previous members
            for child in members_scroll.winfo_children():
                child.destroy()
            members_state["checkboxes"].clear()
            
            team_name = team_var.get()
            if not team_name:
                ctk.CTkLabel(members_scroll, text="Please select a team", 
                            font=(FONT_FAMILY, 10, "italic"), text_color=TEXT_MUTED).pack(padx=12, pady=12)
                return
            
            ctk.CTkLabel(members_scroll, text="⏳ Loading members...", 
                        font=(FONT_FAMILY, 10, "italic"), text_color=TEXT_MUTED).pack(padx=12, pady=12)
            
            def fetch_members():
                try:
                    members_logging = get_team_member_logging(config, team_name)
                    modal.after(0, lambda ml=members_logging: display_members(ml))
                except Exception as e:
                    modal.after(0, lambda: display_error(str(e)))
            
            def display_members(members):
                for child in members_scroll.winfo_children():
                    child.destroy()
                
                if not members:
                    ctk.CTkLabel(members_scroll, text=f"No members found in {team_name}", 
                                font=(FONT_FAMILY, 10, "italic"), text_color=TEXT_MUTED).pack(padx=12, pady=12)
                    return
                
                # "Add All" option
                add_all_var = ctk.BooleanVar(value=False)
                ctk.CTkCheckBox(members_scroll, text=f"Add All Members from {team_name}", 
                               variable=add_all_var, font=(FONT_FAMILY, 11, "bold"),
                               text_color=ACCENT_BLUE, checkbox_width=16, checkbox_height=16,
                               border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=12, pady=(8, 12))
                
                # Individual members
                for member_name in sorted(members.keys()):
                    if member_name == "Unassigned":
                        continue
                    
                    logged = members[member_name]
                    logged_text = f" ({format_time(logged)} logged)" if logged > 0 else ""
                    
                    var = ctk.BooleanVar(value=False)
                    members_state["checkboxes"][member_name] = var
                    
                    ctk.CTkCheckBox(members_scroll, text=f"👤 {member_name}{logged_text}", 
                                   variable=var, font=(FONT_FAMILY, 10),
                                   text_color=TEXT_MAIN, checkbox_width=14, checkbox_height=14,
                                   border_width=1, fg_color=ACCENT_BLUE).pack(anchor="w", padx=24, pady=3)
                
                members_state["data"]["add_all_var"] = add_all_var
                members_state["data"]["members"] = members
            
            def display_error(error):
                for child in members_scroll.winfo_children():
                    child.destroy()
                ctk.CTkLabel(members_scroll, text=f"Error: {error}", 
                            font=(FONT_FAMILY, 10, "italic"), text_color="#ef4444").pack(padx=12, pady=12)
            
            threading.Thread(target=fetch_members, daemon=True).start()
        
        # Load members when team changes
        team_var.trace("w", lambda *args: load_team_members())
        load_team_members()
        
        # Buttons
        btn_frame = ctk.CTkFrame(content, fg_color="transparent")
        btn_frame.pack(fill="x", pady=0)
        
        def add_members():
            """Add selected members"""
            selected_team = team_var.get()
            
            if members_state["data"].get("add_all_var") and members_state["data"]["add_all_var"].get():
                # Add all members
                for member_name in members_state["data"]["members"].keys():
                    if member_name != "Unassigned" and member_name not in config["members"]:
                        config["members"][member_name] = "Syncing..."
                        config.setdefault("members_j1", {})[member_name] = "Syncing..."
            else:
                # Add selected members
                for member_name, var in members_state["checkboxes"].items():
                    if var.get() and member_name not in config["members"]:
                        config["members"][member_name] = "Syncing..."
                        config.setdefault("members_j1", {})[member_name] = "Syncing..."
            
            # Get count of newly added members
            added_count = sum(1 for m in config["members"].keys() if config["members"][m] == "Syncing...")
            
            if added_count > 0:
                save_config(config)
                # Don't auto-switch filter - let user control it manually
                refresh_saved_list()
                refresh_all_members()
                messagebox.showinfo("Success", f"Added members from {selected_team}")
                modal.destroy()
            else:
                messagebox.showinfo("Info", "No new members to add")
        
        ctk.CTkButton(btn_frame, text="Add Selected Members →", 
                     fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER,
                     font=(FONT_FAMILY, 11, "bold"), command=add_members,
                     height=36).pack(fill="x")
    
    ctk.CTkButton(header_row, text="👥 Add Teams", font=(FONT_FAMILY, 11, "bold"), 
                  fg_color="#10b981", hover_color="#059669",
                  height=25, width=85, command=open_add_teams_modal).pack(side="left", padx=5)

    ctk.CTkLabel(header_row, text="🏝️ Holidays:", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=(15, 5))
    holiday_display_lbl = ctk.CTkLabel(header_row, text="", font=(FONT_FAMILY, 11, "bold"), text_color="#fb923c")
    holiday_display_lbl.pack(side="left", padx=5)

    select_dates_btn = ctk.CTkButton(header_row, text="📅 Dates", font=(FONT_FAMILY, 10, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, width=60, height=25)
    select_dates_btn.pack(side="left", padx=5)

    saved_scroll = ctk.CTkScrollableFrame(sprint_container, height=250, orientation="horizontal", fg_color="transparent")
    saved_scroll.pack(fill="x", padx=0, pady=(0, 5))

    master_layout_panel = ctk.CTkFrame(app, fg_color="transparent")

    def update_holiday_display():
        dates = config.get("holiday_dates", [])
        if not dates: holiday_display_lbl.configure(text="None")
        elif len(dates) <= 2: holiday_display_lbl.configure(text=", ".join(dates))
        else: holiday_display_lbl.configure(text=f"{len(dates)} days selected")

    def open_calendar_picker(trigger_button):
        if app.active_calendar_instance is not None:
            try: app.active_calendar_instance.destroy()
            except: pass
            was_same_target = app.active_calendar_target == trigger_button
            app.active_calendar_instance = None
            app.active_calendar_target = None
            master_layout_panel.pack_forget()
            if was_same_target: return
            
        def on_calendar_save(new_dates):
            config["holiday_dates"] = new_dates
            save_config(config)
            update_holiday_display()
            refresh_all_members()
            refresh_main()
            if app.active_calendar_instance is not None:
                app.active_calendar_instance.destroy()
                app.active_calendar_instance = None
                app.active_calendar_target = None
                master_layout_panel.pack_forget()
            messagebox.showinfo("Target Adjusted", f"Recalculating expected hour matrix based on {len(new_dates)} custom holiday date exclusions.")
        
        master_layout_panel.pack(fill="x", padx=25, pady=0, before=summary)
        cal_view = EmbeddedCalendarSelector(master_layout_panel, config.get("holiday_dates", []), on_calendar_save)
        cal_view.pack(side="top", pady=0, anchor="ne", padx=20)
        app.active_calendar_instance = cal_view
        app.active_calendar_target = trigger_button

    select_dates_btn.configure(command=lambda: open_calendar_picker(select_dates_btn))
    update_holiday_display()

    def change_member_dedication(u):
        pop = ctk.CTkToplevel(app)
        pop.geometry("340x200")
        pop.title("Capacity Metric")
        pop.configure(fg_color=CARD_BG)
        pop.attributes('-topmost', True)
        
        active_team = config.get("selected_team", "All Teams")
        team_dedications = config.get("team_member_dedications", {}).get(active_team, {})
        old_fallback = config.get("member_dedications", {}).get(u, 1.0)
        current_val = int(team_dedications.get(u, old_fallback) * 100)
        
        ctk.CTkLabel(pop, text=f"Adjust Dedication Ratio:\n{u} ({active_team})", font=(FONT_FAMILY, 14, "bold"), text_color=TEXT_MAIN).pack(pady=(20, 10))
        ded_entry = ctk.CTkEntry(pop, width=140, fg_color=ITEM_BG, border_color=BORDER_COLOR, justify="center", text_color=TEXT_MAIN)
        ded_entry.insert(0, str(current_val))
        ded_entry.pack()
        
        def save_dedication():
            try: m_ded = float(ded_entry.get() or 100) / 100
            except: m_ded = 1.0
            
            if "team_member_dedications" not in config: config["team_member_dedications"] = {}
            if active_team not in config["team_member_dedications"]: config["team_member_dedications"][active_team] = {}
            config["team_member_dedications"][active_team][u] = m_ded
            config["members"][u] = "Syncing..."
            save_config(config)
            pop.destroy()
            refresh_saved_list()
            
            def recalculate():
                sprint = get_current_sprint(config)
                config.setdefault("member_avatars", {})
                tickets, _, logged, avatar_url = get_jira_data(config, u, force_team_filter=active_team)
                if avatar_url and isinstance(avatar_url, str): config["member_avatars"][u] = avatar_url
                total_tickets = sum(len(tickets[cat]) for cat in tickets) if tickets else 0
                if active_team != "All Teams" and total_tickets == 0: config["members"][u] = "HIDDEN_NO_TICKETS"
                else:
                    base_expected = calculate_expected_hours(sprint["startDate"]) * 3600 if sprint else 0
                    holiday_seconds = calculate_holiday_subtraction_seconds(sprint["startDate"] if sprint else None, config.get("holiday_dates", []))
                    expected = max(0, (base_expected - holiday_seconds) * m_ded)
                    res_str = "Great Job! 🚀" if (logged is not None and logged >= expected) else format_time(expected - (logged or 0))
                    config["members"][u] = res_str
                    base_expected_j1 = calculate_expected_hours_j1(sprint["startDate"]) * 3600 if sprint else 0
                    expected_j1 = max(0, (base_expected_j1 - holiday_seconds) * m_ded)
                    config.setdefault("members_j1", {})[u] = "J-1: On Track ✅" if (logged is not None and logged >= expected_j1) else f"J-1 Missing: {format_time(expected_j1 - (logged or 0))}"
                save_config(config)
                app.after(0, lambda: refresh_saved_list())
                
            threading.Thread(target=recalculate, daemon=True).start()
            
        ctk.CTkButton(pop, text="Apply Changes", fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, font=(FONT_FAMILY, 13, "bold"), command=save_dedication, width=150).pack(pady=20)

    def delete_member(u):
        if u in config["members"]: 
            del config["members"][u]
            if "member_dedications" in config and u in config["member_dedications"]: del config["member_dedications"][u]
            if "team_member_dedications" in config:
                for team in config["team_member_dedications"]:
                    if u in config["team_member_dedications"][team]: del config["team_member_dedications"][team][u]
            if "member_avatars" in config and u in config["member_avatars"]: del config["member_avatars"][u]
            if u in active_card_widgets:
                active_card_widgets[u]["frame"].destroy()
                del active_card_widgets[u]
            save_config(config)
            refresh_saved_list()

    def refresh_saved_list():
        if "members" not in config: config["members"] = {}
        config.setdefault("member_avatars", {})
        config.setdefault("members_j1", {})
        
        team_dedications = config.get("team_member_dedications", {}).get("All Teams", {})
        old_fallback_dict = config.get("member_dedications", {})
        
        # Filter members by selected teams
        visible_users = set()
        for user, status in config["members"].items():
            if status == "HIDDEN_NO_TICKETS":
                continue
            
            # If all teams selected, show all members
            if selected_teams["value"] == set(get_teams_list(config)):
                visible_users.add(user)
            else:
                # Check if member has tickets in ANY of the selected teams
                has_ticket = False
                for team in selected_teams["value"]:
                    try:
                        tickets, _, _, _ = get_jira_data(config, user, force_team_filter=team)
                        if tickets and sum(len(tickets[cat]) for cat in tickets) > 0:
                            has_ticket = True
                            break
                    except:
                        pass
                if has_ticket:
                    visible_users.add(user)
        
        for existing_user in list(active_card_widgets.keys()):
            if existing_user not in visible_users:
                active_card_widgets[existing_user]["frame"].destroy()
                del active_card_widgets[existing_user]
                
        for user, status_text in config["members"].items():
            if user not in visible_users: continue
            m_dedication_value = team_dedications.get(user, old_fallback_dict.get(user, 1.0))
            cur_ded_pct = int(m_dedication_value * 100)

            # ── Real-time badge ──────────────────────────────────────────────
            is_syncing  = status_text == "Syncing..."
            is_great_rt = "Great" in status_text
            rt_color    = "#4b5563" if is_syncing else ("#065f46" if is_great_rt else "#7f1d1d")
            rt_display  = status_text if is_syncing else (
                "⏱ Real-time: Great Job! 🚀" if is_great_rt
                else f"⏱ Real-time Missing: {status_text}"
            )

            # ── J-1 badge ────────────────────────────────────────────────────
            j1_raw   = config["members_j1"].get(user, "")
            is_great_j1 = "On Track" in j1_raw
            j1_color = "#4b5563" if not j1_raw or j1_raw == "Syncing..." else (
                "#065f46" if is_great_j1 else "#78350f"
            )
            j1_display = "📅 J-1: Syncing..." if not j1_raw or j1_raw == "Syncing..." else (
                "📅 J-1: Great Job! ✅" if is_great_j1
                else f"📅 J-1 Missing: {j1_raw.replace('J-1 Missing: ', '').replace('J-1: On Track ✅', '')}"
            )

            if user in active_card_widgets:
                widgets = active_card_widgets[user]
                widgets["dedication_lbl"].configure(text=f"Dedication: {cur_ded_pct}%")
                widgets["badge_frame"].configure(fg_color=rt_color)
                widgets["status_lbl"].configure(text=rt_display)
                if "j1_badge_frame" in widgets:
                    widgets["j1_badge_frame"].configure(fg_color=j1_color)
                    widgets["j1_lbl"].configure(text=j1_display)
                continue
                
            card = ctk.CTkFrame(saved_scroll, width=240, height=230, fg_color=CARD_BG, border_width=1, border_color=BORDER_COLOR, corner_radius=10)
            card.pack(side="left", padx=10, pady=5)
            card.pack_propagate(False)
            
            ctk.CTkButton(card, text="✕", width=20, height=20, corner_radius=10, fg_color=BTN_BG, hover_color="#ef4444", text_color=TEXT_MUTED, font=(FONT_FAMILY, 10, "bold"), border_width=1, border_color=BORDER_COLOR, command=lambda u=user: delete_member(u)).place(relx=0.93, rely=0.12, anchor="center")
            
            header_frame = ctk.CTkFrame(card, fg_color="transparent")
            header_frame.pack(pady=(12, 0), fill="x", padx=15)
            
            def load_avatar_async(label_widget, username_key):
                url = config["member_avatars"].get(username_key)
                if not url or not isinstance(url, str): return
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Authorization': f"Bearer = {config.get('token')}"})
                    with urllib.request.urlopen(req, timeout=5) as response: img_data = response.read()
                    pil_img = Image.open(io.BytesIO(img_data)).resize((28, 28), Image.Resampling.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(28, 28))
                    app.after(0, lambda label=label_widget, img=ctk_img: label.configure(image=img, text=""))
                except Exception: pass

            avatar_label = ctk.CTkLabel(header_frame, text="👤", font=(FONT_FAMILY, 16))
            avatar_label.pack(side="left", padx=(0, 8))
            threading.Thread(target=load_avatar_async, args=(avatar_label, user), daemon=True).start()
            
            name_block = ctk.CTkFrame(header_frame, fg_color="transparent")
            name_block.pack(side="left", fill="both", expand=True)
            ctk.CTkLabel(name_block, text=user, font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MAIN, anchor="w").pack(fill="x")
            dedication_lbl = ctk.CTkLabel(name_block, text=f"Dedication: {cur_ded_pct}%", font=(FONT_FAMILY, 11), text_color=TEXT_MUTED, anchor="w")
            dedication_lbl.pack(fill="x")

            # ── Real-time badge ──────────────────────────────────────────────
            badge = ctk.CTkFrame(card, fg_color=rt_color, corner_radius=4, height=26)
            badge.pack(fill="x", padx=15, pady=(8, 2))
            badge.pack_propagate(False)
            status_lbl = ctk.CTkLabel(badge, text=rt_display, font=(FONT_FAMILY, 11, "bold"), text_color="white")
            status_lbl.pack(expand=True)

            # ── J-1 badge ────────────────────────────────────────────────────
            j1_badge = ctk.CTkFrame(card, fg_color=j1_color, corner_radius=4, height=26)
            j1_badge.pack(fill="x", padx=15, pady=(0, 6))
            j1_badge.pack_propagate(False)
            j1_lbl = ctk.CTkLabel(j1_badge, text=j1_display, font=(FONT_FAMILY, 11, "bold"), text_color="white")
            j1_lbl.pack(expand=True)
            
            btn_row = ctk.CTkFrame(card, fg_color="transparent")
            btn_row.pack(fill="x", padx=15, side="bottom", pady=(0, 8))
            ctk.CTkButton(btn_row, text="View Tickets", height=24, font=(FONT_FAMILY, 11, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, command=lambda u=user: open_member_dashboard(config, u)).pack(fill="x", pady=(0, 4))
            ctk.CTkButton(btn_row, text="Change dedication", height=24, font=(FONT_FAMILY, 11, "bold"), fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, command=lambda u=user: change_member_dedication(u)).pack(fill="x")
            
            active_card_widgets[user] = {"frame": card, "dedication_lbl": dedication_lbl, "badge_frame": badge, "status_lbl": status_lbl, "j1_badge_frame": j1_badge, "j1_lbl": j1_lbl}

    def refresh_all_members():
        if "members" not in config or not config["members"]: return
        sprint = get_current_sprint(config)
        def sync():
            active_team = config.get("selected_team", "All Teams")
            if "members_j1" not in config: config["members_j1"] = {}
            for user in list(config["members"].keys()):
                tickets, _, logged, avatar_url = get_jira_data(config, user, force_team_filter=active_team)
                if avatar_url and isinstance(avatar_url, str): config["member_avatars"][user] = avatar_url
                total_tickets = sum(len(tickets[cat]) for cat in tickets) if tickets else 0
                if active_team != "All Teams" and total_tickets == 0:
                    config["members"][user] = "HIDDEN_NO_TICKETS"
                    config["members_j1"][user] = "HIDDEN_NO_TICKETS"
                elif sprint and sprint.get("startDate"):
                    m_ded = config.get("team_member_dedications", {}).get(active_team, {}).get(user, config.get("member_dedications", {}).get(user, 1.0))
                    holiday_sec = calculate_holiday_subtraction_seconds(sprint["startDate"], config.get("holiday_dates", []))
                    logged = logged or 0

                    # Real-time
                    base_exp = calculate_expected_hours(sprint["startDate"]) * 3600
                    expected = max(0, (base_exp - holiday_sec) * m_ded)
                    config["members"][user] = "Great Job! 🚀" if logged >= expected else format_time(expected - logged)

                    # J-1
                    base_exp_j1 = calculate_expected_hours_j1(sprint["startDate"]) * 3600
                    expected_j1 = max(0, (base_exp_j1 - holiday_sec) * m_ded)
                    config["members_j1"][user] = "J-1: On Track ✅" if logged >= expected_j1 else f"J-1 Missing: {format_time(expected_j1 - logged)}"
                else:
                    config["members"][user] = "No sprint"
                    config["members_j1"][user] = "No sprint"
            save_config(config)
            app.after(0, refresh_saved_list)
        threading.Thread(target=sync, daemon=True).start()

    def on_team_change(choice):
        config["selected_team"] = choice
        save_config(config)
        refresh_all_members()
        refresh_main()

    def export_team_data():
        try:
            file_path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")], title="Export Workspace Team Data")
            if not file_path: return
            export_payload = {"members": config.get("members", {}), "member_dedications": config.get("member_dedications", {}), "team_member_dedications": config.get("team_member_dedications", {}), "selected_team": config.get("selected_team", "All Teams"), "holiday_dates": config.get("holiday_dates", [])}
            with open(file_path, "w", encoding="utf-8") as f: json.dump(export_payload, f, indent=4)
            messagebox.showinfo("Export Successful", "Team profiles backup exported securely!")
        except Exception as e: messagebox.showerror("Export Error", f"Failed to export: {str(e)}")

    def import_team_data():
        try:
            file_path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")], title="Import Workspace Team Data")
            if not file_path: return
            with open(file_path, "r", encoding="utf-8") as f: imported_data = json.load(f)
            for existing_user in list(active_card_widgets.keys()): active_card_widgets[existing_user]["frame"].destroy()
            active_card_widgets.clear()
            config["members"] = imported_data.get("members", {})
            config["member_dedications"] = imported_data.get("member_dedications", {})
            config["team_member_dedications"] = imported_data.get("team_member_dedications", {})
            config["holiday_dates"] = imported_data.get("holiday_dates", [])
            if "selected_team" in imported_data:
                config["selected_team"] = imported_data["selected_team"]
            update_holiday_display()
            save_config(config)
            refresh_saved_list(); refresh_all_members(); refresh_main()
            messagebox.showinfo("Import Success", "Team infrastructure sync definitions imported!")
        except Exception as e: messagebox.showerror("Import Error", f"Failed to parse: {str(e)}")

    
    ctk.CTkButton(controls_frame, text="🔄 Refresh Team", width=120, height=28, font=(FONT_FAMILY, 12, "bold"), fg_color=CARD_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN, border_width=1, border_color=BORDER_COLOR, command=lambda: (_refresh_teams_list_async(config), refresh_all_members(), refresh_main())).pack(side="right", padx=(15, 0))
    ctk.CTkButton(controls_frame, text="📋 Team Tickets", width=120, height=28, font=(FONT_FAMILY, 12, "bold"), fg_color=CARD_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN, border_width=1, border_color=BORDER_COLOR, command=lambda: open_team_tickets_dashboard(config)).pack(side="right", padx=(15, 0))
    ctk.CTkButton(controls_frame, text="🏢 Teams Overview", width=135, height=28, font=(FONT_FAMILY, 12, "bold"), fg_color=CARD_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN, border_width=1, border_color=BORDER_COLOR, command=lambda: open_teams_overview_dashboard(config)).pack(side="right", padx=(15, 0))
    ctk.CTkButton(controls_frame, text="📤 Export Team", width=110, height=28, font=(FONT_FAMILY, 12, "bold"), fg_color=CARD_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN, border_width=1, border_color=BORDER_COLOR, command=export_team_data).pack(side="right", padx=(15, 0))
    ctk.CTkButton(controls_frame, text="📥 Import Team", width=110, height=28, font=(FONT_FAMILY, 12, "bold"), fg_color=CARD_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN, border_width=1, border_color=BORDER_COLOR, command=import_team_data).pack(side="right", padx=(15, 0))

    summary = ctk.CTkFrame(app, fg_color=CARD_BG, corner_radius=12, border_width=1, border_color=BORDER_COLOR)
    summary.pack(fill="x", padx=25, pady=(0, 10))
    summary_row = ctk.CTkFrame(summary, fg_color="transparent")
    summary_row.pack(pady=10, padx=15, fill="x")
    summary_label = ctk.CTkLabel(summary_row, text="Establishing API sync matrix...", font=(FONT_FAMILY, 15, "bold"), text_color=TEXT_MUTED)
    summary_label.pack(side="left", padx=(5, 20))
    summary_j1_label = ctk.CTkLabel(summary_row, text="", font=(FONT_FAMILY, 13, "bold"), text_color=TEXT_MUTED)
    summary_j1_label.pack(side="left", padx=(0, 5))
    
    main_scroll = ctk.CTkScrollableFrame(app, fg_color="transparent")
    main_scroll.pack(fill="both", expand=True, padx=25, pady=10)

    def refresh_main():
        def run_fetch():
            active_team = config.get("selected_team", "All Teams")
            try:
                tickets, totals, logged, _ = get_jira_data(config, force_team_filter=active_team)
            except:
                tickets, totals, logged = None, None, 0

            if tickets:
                all_keys = [t[0] for lane in tickets.values() for t in lane]
                # Evict cached False entries so tickets that gained a checklist
                # mid-sprint (or were added to the sprint after initial load)
                # are re-checked on this cycle instead of being silently skipped.
                with _checklist_cache_lock:
                    stale = [k for k, v in _checklist_cache.items() if not v]
                    for k in stale:
                        del _checklist_cache[k]
                # Warm synchronously — we are already on a background thread,
                # so this blocks only this thread, not the UI. The render is
                # scheduled only after every key is resolved, guaranteeing the
                # 📋 button appears for any ticket that has a checklist.
                _warm_checklist_cache(config, all_keys)
                app.after(0, lambda: render_main_ui(tickets, totals, logged or 0))
        def render_main_ui(tickets, totals, logged):
            for w in main_scroll.winfo_children(): w.destroy()
            sprint = get_current_sprint(config)
            logged = logged or 0
            my_dedication = config.get("dedication", 1.0)
            holiday_seconds = calculate_holiday_subtraction_seconds(sprint["startDate"] if sprint else None, config.get("holiday_dates", []))

            # Real-time expected (includes today up to now)
            base_expected = calculate_expected_hours(sprint["startDate"]) * 3600 if sprint else 0
            expected = max(0, (base_expected - holiday_seconds) * my_dedication)

            # J-1 expected (only fully elapsed days, today excluded)
            base_expected_j1 = calculate_expected_hours_j1(sprint["startDate"]) * 3600 if sprint else 0
            expected_j1 = max(0, (base_expected_j1 - holiday_seconds) * my_dedication)

            # Real-time status text
            if logged >= expected:
                rt_txt = f"MY Tickets: {format_time(logged)} Logged  •  🚀 On Track / Great Job!"
                rt_color = "#059669"
            else:
                rt_txt = f"MY Tickets: {format_time(logged)} Logged  •  Missing: {format_time(expected - logged)}"
                rt_color = "#ef4444"

            # J-1 status text
            if logged >= expected_j1:
                j1_txt = f"J-1: 🟢 {format_time(logged)} / {format_time(expected_j1)} — On Track"
                j1_color = "#059669"
            else:
                j1_txt = f"J-1 Missing: {format_time(expected_j1 - logged)}"
                j1_color = "#f59e0b"

            summary_label.configure(text=rt_txt, text_color=rt_color)
            summary_j1_label.configure(text=f"  |  {j1_txt}", text_color=j1_color)
            swimlanes = [("🟥 To Do", "todo", "#f87171"), ("🟧 In Progress", "in_progress", "#fb923c"), ("🟦 Approved", "approved", "#60a5fa"), ("⛔ Blocked", "blocked", "#dc2626"), ("🟡 Partially Blocked", "partially_blocked", "#eab308"), ("🟩 Done", "done", "#34d399")]
            
            lane_bg = CARD_BG[1] if isinstance(CARD_BG, tuple) else CARD_BG
            
            for title, key, heading_color in swimlanes:
                lane_frame = ctk.CTkFrame(main_scroll, fg_color=lane_bg, corner_radius=10, border_width=1, border_color=BORDER_COLOR)
                lane_frame.pack(fill="x", pady=10, ipady=5)
                ctk.CTkLabel(lane_frame, text=f"{title} ({format_time(totals[key])})", font=(FONT_FAMILY, 14, "bold"), text_color=heading_color).pack(anchor="w", padx=15, pady=12)
                
                if not tickets[key]:
                    ctk.CTkLabel(lane_frame, text="Empty queue lane.", font=(FONT_FAMILY, 12, "italic"), text_color=TEXT_MUTED).pack(anchor="w", padx=30, pady=5)
                    continue
                    
                th_row = ctk.CTkFrame(lane_frame, fg_color="transparent")
                th_row.pack(fill="x", padx=15, pady=(2, 5))
                ctk.CTkLabel(th_row, text="Ticket Title", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=5)
                ctk.CTkLabel(th_row, text="Achievement", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=220, anchor="e").pack(side="right", padx=(0, 25))
                ctk.CTkLabel(th_row, text="Logged Time", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=110, anchor="e").pack(side="right", padx=(0, 25))
                ctk.CTkLabel(th_row, text="Estimated Time", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=120, anchor="e").pack(side="right", padx=10)
                ctk.CTkLabel(th_row, text="Activity", font=(FONT_FAMILY, 11, "bold"), text_color=TEXT_MUTED, width=140, anchor="w").pack(side="right", padx=15)
                
                for item in tickets[key]:
                    t_key, sumry, link, sp, rm, activity_lbl, validation_lbl, est_time, *extra = item
                    activity_lbl = activity_lbl if activity_lbl else "N/A"
                    achieved_tcs     = extra[0] if len(extra) > 0 else 0
                    achieved_reqs    = extra[1] if len(extra) > 1 else 0
                    achieved_tickets = extra[2] if len(extra) > 2 else 0
                    achievement_val = (achieved_tcs if isinstance(achieved_tcs, int) else 0) + \
                                      (achieved_reqs if isinstance(achieved_reqs, int) else 0) + \
                                      (achieved_tickets if isinstance(achieved_tickets, int) else 0)
                    item_box = ctk.CTkFrame(lane_frame, fg_color=ITEM_BG, corner_radius=6, border_width=1, border_color=BORDER_COLOR)
                    item_box.pack(fill="x", padx=15, pady=4)
                    # Achievement — only shown on the ticket row when it actually has a non-zero achievement
                    if achievement_val != 0:
                        ach_color = "#34d399" if achievement_val > 0 else TEXT_MUTED
                        ach_text = f"✓ {achievement_val} (Req {achieved_reqs} TC {achieved_tcs} Ticket {achieved_tickets})"
                        ctk.CTkLabel(item_box, text=ach_text, font=(FONT_FAMILY, 11, "bold"), text_color=ach_color, width=220, anchor="e").pack(side="right", padx=20, pady=8)
                    ctk.CTkLabel(item_box, text=sp if sp else "0h", font=(FONT_FAMILY, 12, "bold"), text_color="#34d399", width=110, anchor="e").pack(side="right", padx=20, pady=8)
                    ctk.CTkLabel(item_box, text=est_time if est_time else "0h", font=(FONT_FAMILY, 12), text_color=TEXT_MUTED, width=120, anchor="e").pack(side="right", padx=10, pady=8)
                    act_badge = ctk.CTkFrame(item_box, fg_color=BTN_BG if activity_lbl != "N/A" else "transparent", corner_radius=4)
                    act_badge.pack(side="right", padx=15, pady=6)
                    ctk.CTkLabel(act_badge, text=str(activity_lbl).upper(), font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT_BLUE if activity_lbl != "N/A" else TEXT_MUTED, width=120, anchor="center").pack(padx=8, pady=2)
                    # To Do List button — opens this ticket's native description checklist
                    # Only shown when the ticket actually has a to-do / checklist
                    if _ticket_has_checklist(config, t_key):
                        ctk.CTkButton(item_box, text="📋", width=30, height=26, font=(FONT_FAMILY, 12, "bold"),
                                      fg_color=BTN_BG, hover_color=BTN_HOVER, text_color=TEXT_MAIN,
                                      command=lambda k=t_key, l=link: open_ticket_checklist_window(config, app, k, l)
                                      ).pack(side="right", padx=(8, 2), pady=8)
                    # Title packed LAST so it fills remaining space without pushing right elements off screen
                    l = ctk.CTkLabel(item_box, text=f"[{t_key}] {sumry}", font=(FONT_FAMILY, 12), text_color=TEXT_MAIN, cursor="hand2", justify="left", anchor="w", wraplength=0)
                    l.pack(side="left", fill="x", expand=True, padx=15, pady=8)
                    l.bind("<Button-1>", lambda e, url=link: webbrowser.open(url))
            app.after(REFRESH_INTERVAL, refresh_main)
        threading.Thread(target=run_fetch, daemon=True).start()

    refresh_saved_list()
    refresh_all_members()
    refresh_main()
    app.mainloop()
    return session_status["logout"]

def show_login():
    app = ctk.CTk()
    app.geometry("440x550") 
    app.title("Access Node Gateway")
    app.configure(fg_color=APP_BG)
    
    frame = ctk.CTkFrame(app, fg_color=CARD_BG, border_width=1, border_color=BORDER_COLOR, corner_radius=12)
    frame.pack(fill="both", expand=True, padx=25, pady=25)
    
    ctk.CTkLabel(frame, text="Jira Sync Portal", font=(FONT_FAMILY, 24, "bold"), text_color=TEXT_MAIN).pack(pady=(35, 5))
    ctk.CTkLabel(frame, text="Enter jira technica credentials", font=(FONT_FAMILY, 12), text_color=TEXT_MUTED).pack(pady=(0, 25))
    
    u_e = ctk.CTkEntry(frame, placeholder_text="Jira User ID", width=280, height=38, fg_color=ITEM_BG, border_color=BORDER_COLOR, font=(FONT_FAMILY, 13), text_color=TEXT_MAIN)
    u_e.pack(pady=10)
    
    p_e = ctk.CTkEntry(frame, placeholder_text="Password", show="*", width=280, height=38, fg_color=ITEM_BG, border_color=BORDER_COLOR, font=(FONT_FAMILY, 13), text_color=TEXT_MAIN)
    p_e.pack(pady=10)
    
    def toggle_password_visibility():
        p_e.configure(show="" if show_pass_var.get() else "*")

    show_pass_var = ctk.BooleanVar(value=False)
    show_pass_cb = ctk.CTkCheckBox(
        frame, text="Show Token", variable=show_pass_var, command=toggle_password_visibility, 
        font=(FONT_FAMILY, 11), text_color=TEXT_MUTED, checkbox_width=16, checkbox_height=16, 
        border_width=1, hover_color=BTN_HOVER, fg_color=ACCENT_BLUE
    )
    show_pass_cb.pack(pady=(0, 10), anchor="w", padx=55)
    
    d_e = ctk.CTkEntry(frame, placeholder_text="Dedication % (e.g. 100)", width=280, height=38, fg_color=ITEM_BG, border_color=BORDER_COLOR, font=(FONT_FAMILY, 13), text_color=TEXT_MAIN)
    d_e.pack(pady=10)
    
    login_status = {"cfg": None}

    def do_login():
        submit_btn.configure(state="disabled", text="Verifying Credentials...")
        
        def verify_credentials_thread():
            try: dedication_val = float(d_e.get() or 100)/100
            except: dedication_val = 1.0
            
            cfg = {
                "user": u_e.get().strip(), 
                "token": p_e.get().strip(), 
                "dedication": dedication_val, 
                "members": {}, 
                "member_dedications": {}, 
                "team_member_dedications": {}, 
                "selected_team": "All Teams",
                "holiday_dates": [] 
            }
            
            identity = get_user_identity(cfg)
            
            if identity and "error" not in identity:
                save_config(cfg)
                login_status["cfg"] = cfg
                app.after(0, app.destroy)
            elif identity and identity.get("error") == "CAPTCHA_CHALLENGE":
                app.after(0, lambda: messagebox.showerror(
                    "Jira Security Lockout", 
                    "Jira has locked basic API connections due to too many failed attempts.\n\n"
                    "Even if your token is now correct, Jira will reject it.\n\n"
                    "To fix this:\n1. Open Jira in your web browser.\n2. Log out and log back in manually.\n3. Pass the CAPTCHA check in your browser, then come back here to connect."
                ))
                app.after(0, lambda: submit_btn.configure(state="normal", text="Initialize Gateway Connection"))
            else:
                app.after(0, lambda: messagebox.showerror("Authentication Failed", "Wrong credentials! Please verify your Jira User ID and Password."))
                app.after(0, lambda: submit_btn.configure(state="normal", text="Initialize Gateway Connection"))

        threading.Thread(target=verify_credentials_thread, daemon=True).start()
        
    submit_btn = ctk.CTkButton(frame, text="Confirm", font=(FONT_FAMILY, 13, "bold"), width=280, height=40, fg_color=ACCENT_BLUE, hover_color=ACCENT_HOVER, command=do_login)
    submit_btn.pack(pady=25)
    
    app.mainloop()
    return login_status["cfg"]

if __name__ == "__main__":
    ctk.deactivate_automatic_dpi_awareness()
    
    while True:
        conf = load_config()
        if not conf:
            conf = show_login()
            if not conf:
                break  
        
        if "holiday_dates" not in conf:
            conf["holiday_dates"] = []
            save_config(conf)
            
        logged_out = run_dashboard(conf)
        if not logged_out:
            break