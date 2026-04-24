"""DB package facade with backward compatibility.

`import db` keeps working, while domain modules are available as:
`db.team`, `db.problem`, etc.
"""

from . import audit, cguard, connection, problem, relay, team, ticket
from .schema import init_db
from .cguard import (
    create_cguard_log,
    create_cguard_onoff_log,
    get_cguard_user_status,
    is_cguard_user_blocked,
    set_cguard_user_status,
)
from .problem import (
    create_problem,
    ensure_problem_table,
    get_problem_by_no,
    list_problems,
)
from .audit import create_audit_log, create_message_log
from .team import (
    create_team_announcement,
    create_team_countdown_schedule,
    create_team_hint,
    create_team_notification_event,
    get_team_announcement_by_number,
    get_team_by_id,
    get_team_by_role,
    get_team_countdown_by_number,
    get_team_detail,
    get_team_hint_by_number,
    get_team_role_mapping,
    get_user_team,
    list_due_team_announcements,
    list_due_team_countdowns,
    list_due_team_hints,
    list_team_announcements,
    list_team_countdown_schedules,
    list_team_hints,
    list_teams,
    mark_countdown_ended,
    mark_countdown_started,
    mark_schedule_failed,
    mark_schedule_sent,
    soft_delete_schedule,
    update_team_notification_event,
    upsert_team_discord_role,
)
from .ticket import (
    allocate_ticket_identity,
    assign_ticket_number_for_category,
    close_ticket,
    get_latest_ticket_by_thread_id,
    get_ticket_notifier,
    get_open_ticket_by_thread_id,
    get_open_tickets_by_thread_id,
    get_open_tickets_by_user,
    get_open_ticket_by_user,
    get_ticket,
    next_ticket_sequence,
    set_ticket_notifier,
    set_ticket_approval,
    store_ticket,
    update_ticket_submission,
    update_ticket_category,
)
from .connection import _connect, _now_kst_naive
from .relay import create_dispatch_history, create_relay_event, mark_dispatch_result


__all__ = [
    "_connect",
    "_now_kst_naive",
    "init_db",
    "audit",
    "cguard",
    "connection",
    "problem",
    "relay",
    "team",
    "ticket",
]
