"""Poll doc_shifts for open/close and format notification messages."""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from db import MySQLConnectionManager
from helper import translations
from recipient_service import get_recipients_for_object

logger = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_SCRIPT_DIR, "shift_state.json")
STATE_LAST_SFT = "last_sft_id"
STATE_LAST_CLOSED = "last_closed_sft_id"


def load_shift_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load shift state from %s: %s", STATE_FILE, e)
        return {}


def save_shift_state(last_sft_id: int, last_closed_sft_id: int) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    STATE_LAST_SFT: int(last_sft_id),
                    STATE_LAST_CLOSED: int(last_closed_sft_id),
                },
                f,
                indent=2,
            )
        logger.debug(
            "Saved shift state last_sft_id=%s last_closed_sft_id=%s",
            last_sft_id,
            last_closed_sft_id,
        )
    except Exception as e:
        logger.warning("Failed to save shift state to %s: %s", STATE_FILE, e)


async def get_max_sft_id(db: MySQLConnectionManager) -> int:
    row = await db.execute_one("SELECT COALESCE(MAX(sft_id), 0) AS mx FROM doc_shifts")
    return int(row["mx"]) if row and row.get("mx") is not None else 0


async def get_max_closed_sft_id(db: MySQLConnectionManager) -> int:
    row = await db.execute_one(
        "SELECT COALESCE(MAX(sft_id), 0) AS mx FROM doc_shifts WHERE sft_closed = 1"
    )
    return int(row["mx"]) if row and row.get("mx") is not None else 0


async def get_new_opened_shifts(
    db: MySQLConnectionManager, after_sft_id: int
) -> list[dict]:
    rows = await db.execute(
        """
        SELECT s.sft_id, s.sft_datetime0, s.sft_datetime1, s.sft_closed, s.sft_cash,
               s.sft_autor0, s.sft_autor1,
               COALESCE(c.ocs_object, 0) AS obj_id,
               COALESCE(c.ocs_sn, '') AS ocs_sn,
               COALESCE(o.obj_name, '') AS obj_name
        FROM doc_shifts s
        LEFT JOIN dir_operating_cashes c ON c.ocs_id = s.sft_cash
        LEFT JOIN dir_objects o ON o.obj_id = c.ocs_object
        WHERE s.sft_id > %s
        ORDER BY s.sft_id
        """,
        (after_sft_id,),
    )
    return list(rows) if rows else []


async def get_newly_closed_shifts(
    db: MySQLConnectionManager, after_closed_sft_id: int, up_to_sft_id: int
) -> list[dict]:
    rows = await db.execute(
        """
        SELECT s.sft_id, s.sft_datetime0, s.sft_datetime1, s.sft_closed, s.sft_cash,
               s.sft_autor0, s.sft_autor1,
               COALESCE(c.ocs_object, 0) AS obj_id,
               COALESCE(c.ocs_sn, '') AS ocs_sn,
               COALESCE(o.obj_name, '') AS obj_name
        FROM doc_shifts s
        LEFT JOIN dir_operating_cashes c ON c.ocs_id = s.sft_cash
        LEFT JOIN dir_objects o ON o.obj_id = c.ocs_object
        WHERE s.sft_closed = 1
          AND s.sft_id > %s
          AND s.sft_id <= %s
        ORDER BY s.sft_id
        """,
        (after_closed_sft_id, up_to_sft_id),
    )
    return list(rows) if rows else []


def _fmt_dt(value) -> str:
    if value is None:
        return ""
    try:
        if hasattr(value, "strftime"):
            # Skip zero/default MySQL dates
            if isinstance(value, datetime) and value.year < 1900:
                return ""
            return value.strftime("%d.%m.%Y %H:%M")
        s = str(value)
        if s.startswith("0000") or s.startswith("1900-01-01"):
            return ""
        return s
    except Exception:
        return str(value) if value else ""


def format_shift_opened_message(shift: dict) -> str:
    obj_name = (shift.get("obj_name") or "").replace("*", "\\*") or "—"
    cash = (shift.get("ocs_sn") or "").strip() or str(shift.get("sft_cash") or "")
    parts = [
        translations.get("shift.opened", "*Shift opened*"),
        f"{translations.get('shift.object', 'Store')}: {obj_name}",
        f"{translations.get('shift.cash', 'Cash register')}: {cash}",
    ]
    dt = _fmt_dt(shift.get("sft_datetime0"))
    if dt:
        parts.append(f"{translations.get('shift.time', 'Time')}: {dt}")
    parts.append(f"{translations.get('shift.id', 'Shift')}: {shift.get('sft_id')}")
    return "\n".join(parts)


def format_shift_closed_message(shift: dict) -> str:
    obj_name = (shift.get("obj_name") or "").replace("*", "\\*") or "—"
    cash = (shift.get("ocs_sn") or "").strip() or str(shift.get("sft_cash") or "")
    parts = [
        translations.get("shift.closed", "*Shift closed*"),
        f"{translations.get('shift.object', 'Store')}: {obj_name}",
        f"{translations.get('shift.cash', 'Cash register')}: {cash}",
    ]
    opened = _fmt_dt(shift.get("sft_datetime0"))
    closed = _fmt_dt(shift.get("sft_datetime1"))
    if opened:
        parts.append(f"{translations.get('shift.opened_at', 'Opened')}: {opened}")
    if closed:
        parts.append(f"{translations.get('shift.closed_at', 'Closed')}: {closed}")
    parts.append(f"{translations.get('shift.id', 'Shift')}: {shift.get('sft_id')}")
    return "\n".join(parts)


async def resolve_shift_recipients(
    db: MySQLConnectionManager, shift: dict
) -> list[int]:
    obj_id = shift.get("obj_id")
    try:
        obj_id = int(obj_id) if obj_id else 0
    except (TypeError, ValueError):
        obj_id = 0
    if obj_id <= 0:
        logger.debug("Shift sft_id=%s has no object, skipping recipients", shift.get("sft_id"))
        return []
    return await get_recipients_for_object(db, obj_id)
