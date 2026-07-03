import schedule
import time
import threading
from plyer import notification
from config import ALERT_TIMES

def trigger_alert(app):
    try:
        notification.notify(
            title="Tickets Logging Alert 🚀",
            message="Time to check your Jira logs and sprint progress!",
            app_name="Jira Scrum Tracker",
            timeout=10
        )
    except Exception as e:
        print(f"[trigger_alert] notification failed: {e}")

    try:
        app.after(0, lambda: (
            app.deiconify(),
            app.focus_force(),
            app.attributes('-topmost', True),
            app.attributes('-topmost', False)
        ))
    except Exception as e:
        print(f"[trigger_alert] window focus failed: {e}")

def start_scheduler(app):
    def run_scheduler():
        for alert_time in ALERT_TIMES:
            schedule.every().day.at(alert_time).do(trigger_alert, app)
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                print(f"[scheduler] run_pending error: {e}")
            time.sleep(30)
    threading.Thread(target=run_scheduler, daemon=True).start()
