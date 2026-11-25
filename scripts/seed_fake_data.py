"""Seed realistic fake data into TimeTracker database for testing dashboards and reports."""
from __future__ import annotations

import datetime
import logging
import random
import shutil
import sys
import time
from pathlib import Path

# Add parent directory to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from time_tracker.core.config import Config, load_config
from time_tracker.core.database import Database, get_default_db_path
from time_tracker.core.retention import aggregate_day, aggregate_missing_days

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_data")

APPS_BY_PROJECT = {
    "Bussma Mobile App": [
        ("Code.exe", 0.45),
        ("chrome.exe", 0.25),
        ("Figma.exe", 0.15),
        ("WindowsTerminal.exe", 0.10),
        ("Postman.exe", 0.05),
    ],
    "ERP Backend Integration": [
        ("Code.exe", 0.50),
        ("Postman.exe", 0.25),
        ("WindowsTerminal.exe", 0.15),
        ("chrome.exe", 0.10),
    ],
    "Admin Portal UI": [
        ("Code.exe", 0.40),
        ("chrome.exe", 0.35),
        ("Figma.exe", 0.15),
        ("WindowsTerminal.exe", 0.10),
    ],
    "Customer Support & Bugs": [
        ("chrome.exe", 0.40),
        ("Teams.exe", 0.30),
        ("Code.exe", 0.20),
        ("Notion.exe", 0.10),
    ],
    "DevOps & Cloud Infra": [
        ("WindowsTerminal.exe", 0.50),
        ("chrome.exe", 0.30),
        ("Code.exe", 0.20),
    ],
}


def pick_app_for_project(proj_name: str) -> str:
    apps_weights = APPS_BY_PROJECT.get(proj_name)
    if not apps_weights:
        return "Code.exe"
    apps, weights = zip(*apps_weights)
    return random.choices(apps, weights=weights, k=1)[0]


def seed_database(days_back: int = 14, backup: bool = True) -> None:
    db_path = get_default_db_path()
    logger.info("Target database: %s", db_path)

    if backup and db_path.exists():
        backup_path = db_path.parent / f"data_backup_before_fake_{int(time.time())}.db"
        shutil.copy2(db_path, backup_path)
        logger.info("Backup created at: %s", backup_path)

    config = load_config()
    db = Database(db_path)

    # 1. Ensure projects
    project_defs = [
        ("Bussma Mobile App", "#2563EB"),
        ("ERP Backend Integration", "#10B981"),
        ("Admin Portal UI", "#F59E0B"),
        ("Customer Support & Bugs", "#EC4899"),
        ("DevOps & Cloud Infra", "#8B5CF6"),
    ]

    project_map: dict[str, int] = {}
    existing_projects = {p["name"].lower(): p["id"] for p in db.list_projects()}

    for name, color in project_defs:
        if name.lower() in existing_projects:
            project_map[name] = existing_projects[name.lower()]
            logger.info("Project '%s' already exists (ID: %d)", name, project_map[name])
        else:
            pid = db.add_project(name=name, color=color)
            project_map[name] = pid
            logger.info("Created project '%s' (ID: %d, Color: %s)", name, pid, color)

    project_names = list(project_map.keys())

    # 2. Date range: last `days_back` days up to today
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=days_back)
    logger.info("Generating data from %s to %s (%d days)...", start_date, today, days_back + 1)

    total_events_created = 0
    total_sessions_created = 0

    curr = start_date
    while curr <= today:
        is_today = (curr == today)
        weekday_name = curr.strftime("%a")  # Sun, Mon, etc.
        is_work_day = weekday_name in config.work_days

        # Base datetime for the day
        day_base = datetime.datetime.combine(curr, datetime.time(0, 0, 0))

        if not is_work_day:
            # Weekend day (e.g. Fri or Sat)
            # Add an occasional overtime on one Saturday
            if weekday_name == "Sat" and curr == today - datetime.timedelta(days=8):
                logger.info("Adding weekend overtime for %s (Saturday)", curr)
                ot_start = int(day_base.replace(hour=11, minute=0).timestamp())
                ot_end = int(day_base.replace(hour=13, minute=15).timestamp())
                ot_pid = project_map["DevOps & Cloud Infra"]
                db.add_past_session(
                    session_type="overtime",
                    start_ts=ot_start,
                    end_ts=ot_end,
                    source="manual",
                    note="Production server migration and DB indexing",
                    project_id=ot_pid,
                )
                total_sessions_created += 1

                # Events during overtime
                ev_list = []
                for ts in range(ot_start, ot_end, 30):
                    app = pick_app_for_project("DevOps & Cloud Infra")
                    ev_list.append((ts, app, "input", ot_pid))
                db.record_input_events_batch(ev_list)
                total_events_created += len(ev_list)
                aggregate_day(db, config, curr, recompute=True)

            curr += datetime.timedelta(days=1)
            continue

        # Normal Work Day (Sun-Thu)
        logger.info("Generating realistic data for %s (%s)...", curr, weekday_name)

        # Work schedule:
        # Start: 09:30
        # Standup / Meeting: 10:00 - 10:45
        # Lunch Break: 13:00 - 13:45
        # Optional afternoon meeting: 15:30 - 16:15 (on Mon, Wed)
        # End: 17:30 (or overtime until 18:30 on Tue, Thu)
        # For today: up to current hour (approx 14:15)

        end_limit_hour = 14 if is_today else 18

        # Morning start around 09:25 - 09:35
        work_start_dt = day_base.replace(hour=9, minute=random.randint(25, 35))
        work_end_hour = 17 if not is_today else min(14, end_limit_hour)
        work_end_dt = day_base.replace(hour=work_end_hour, minute=random.randint(25, 35))

        # 1. Morning standup meeting
        m1_start = int(day_base.replace(hour=10, minute=0).timestamp())
        m1_end = int(day_base.replace(hour=10, minute=random.choice([30, 45])).timestamp())
        m1_proj = project_map[random.choice(["Bussma Mobile App", "ERP Backend Integration"])]
        db.add_past_session(
            session_type="meeting",
            start_ts=m1_start,
            end_ts=m1_end,
            source="manual",
            note=random.choice([
                "Daily Standup & Sprint Progress",
                "Morning Team Sync",
                "Sprint Backlog Grooming",
                "Task Alignment Meeting",
            ]),
            project_id=m1_proj,
        )
        total_sessions_created += 1

        # 2. Lunch break
        b_start = int(day_base.replace(hour=13, minute=0).timestamp())
        b_end = int(day_base.replace(hour=13, minute=random.choice([40, 50])).timestamp())
        db.add_past_session(
            session_type="break",
            start_ts=b_start,
            end_ts=b_end,
            source="manual",
            note="Lunch Break",
        )
        total_sessions_created += 1

        # 3. Afternoon meeting (on some days, if not today or past 15:00)
        has_afternoon_meeting = (weekday_name in ("Mon", "Wed", "Sun")) and (not is_today)
        m2_start, m2_end = 0, 0
        if has_afternoon_meeting:
            m2_start = int(day_base.replace(hour=15, minute=30).timestamp())
            m2_end = int(day_base.replace(hour=16, minute=15).timestamp())
            m2_proj = project_map[random.choice(["Admin Portal UI", "Customer Support & Bugs"])]
            db.add_past_session(
                session_type="meeting",
                start_ts=m2_start,
                end_ts=m2_end,
                source="manual",
                note=random.choice([
                    "Client Feature Review",
                    "UI/UX Design Alignment",
                    "Customer Issue Escalation Sync",
                    "Architecture Deep Dive",
                ]),
                project_id=m2_proj,
            )
            total_sessions_created += 1

        # 4. Overtime session (on select days, e.g. Tue and Thu when not today)
        has_overtime = (weekday_name in ("Tue", "Thu")) and (not is_today)
        if has_overtime:
            ot_start = int(day_base.replace(hour=17, minute=30).timestamp())
            ot_end = int(day_base.replace(hour=18, minute=random.randint(30, 50)).timestamp())
            ot_proj = project_map[random.choice(["Bussma Mobile App", "ERP Backend Integration"])]
            db.add_past_session(
                session_type="overtime",
                start_ts=ot_start,
                end_ts=ot_end,
                source="manual",
                note=random.choice([
                    "Urgent release deployment & smoke testing",
                    "Critical bug fixing for client demo",
                    "Sprint milestone delivery completion",
                ]),
                project_id=ot_proj,
            )
            total_sessions_created += 1

            # Overtime events
            ot_events = []
            for ts in range(ot_start, ot_end, random.choice([25, 35])):
                proj_name = [k for k, v in project_map.items() if v == ot_proj][0]
                app = pick_app_for_project(proj_name)
                ot_events.append((ts, app, "input", ot_proj))
            db.record_input_events_batch(ot_events)
            total_events_created += len(ot_events)

        # 5. Regular input events throughout the day
        # Working chunks:
        # chunk 1: work_start to m1_start (09:30 - 10:00)
        # chunk 2: m1_end to b_start (10:45 - 13:00)
        # chunk 3: b_end to (m2_start or 17:30)
        # chunk 4: m2_end to 17:30 (if afternoon meeting)
        working_chunks = []
        c1_start = int(work_start_dt.timestamp())
        if c1_start < m1_start:
            working_chunks.append((c1_start, m1_start, random.choice(project_names)))

        if is_today:
            now_ts = int(time.time())
            c2_end = min(b_start, now_ts)
            if m1_end < c2_end:
                working_chunks.append((m1_end, c2_end, random.choice(project_names)))
            if b_end < now_ts:
                working_chunks.append((b_end, now_ts, random.choice(project_names)))
        else:
            working_chunks.append((m1_end, b_start, random.choice(project_names)))
            if has_afternoon_meeting:
                working_chunks.append((b_end, m2_start, random.choice(project_names)))
                working_chunks.append((m2_end, int(work_end_dt.timestamp()), random.choice(project_names)))
            else:
                working_chunks.append((b_end, int(work_end_dt.timestamp()), random.choice(project_names)))

        day_events = []
        for start_ts, end_ts, proj_name in working_chunks:
            pid = project_map[proj_name]
            curr_ts = start_ts
            while curr_ts < end_ts:
                # Event every 20-45 seconds
                app = pick_app_for_project(proj_name)
                day_events.append((curr_ts, app, "input", pid))
                step = random.randint(20, 45)
                # occasional micro-pause
                if random.random() < 0.05:
                    step += random.randint(60, 120)
                curr_ts += step

        db.record_input_events_batch(day_events)
        total_events_created += len(day_events)

        # Aggregate day into daily_summary
        summary_res = aggregate_day(db, config, curr, recompute=True)
        worked_h = round(summary_res["worked_sec"] / 3600.0, 1)
        ot_h = round(summary_res["overtime_sec"] / 3600.0, 1)
        m_h = round(summary_res["meeting_sec"] / 3600.0, 1)
        logger.info(
            "--> %s summary: Worked: %sh | Overtime: %sh | Meetings: %sh | Events: %d",
            curr, worked_h, ot_h, m_h, len(day_events)
        )

        curr += datetime.timedelta(days=1)

    logger.info("========================================================")
    logger.info("SUCCESS! Seeded %d input events and %d sessions.", total_events_created, total_sessions_created)
    logger.info("All daily summaries updated.")
    logger.info("========================================================")


if __name__ == "__main__":
    days = 14
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    seed_database(days_back=days)
