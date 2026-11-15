"""Persist and check Telegram user activation via shared code from config."""
import json
import logging
import os
from typing import Set

from db import MySQLConnectionManager
from recipient_service import list_subscribed_object_ids

logger = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_SCRIPT_DIR, "activated_users.json")
STATE_KEY = "uids"


def load_activated_uids() -> Set[int]:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get(STATE_KEY, [])
        out: Set[int] = set()
        for item in raw:
            try:
                out.add(int(item))
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.warning("Failed to load activated users from %s: %s", STATE_FILE, e)
        return set()


def save_activated_uids(uids: Set[int]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {STATE_KEY: sorted(uids)},
                f,
                indent=2,
            )
        logger.info("Saved %d activated uid(s) to %s", len(uids), STATE_FILE)
    except Exception as e:
        logger.warning("Failed to save activated users to %s: %s", STATE_FILE, e)


def activate_user(telegram_uid: int) -> None:
    uids = load_activated_uids()
    uid = int(telegram_uid)
    if uid in uids:
        return
    uids.add(uid)
    save_activated_uids(uids)
    logger.info("Activated telegram uid=%s", uid)


async def is_activated(db: MySQLConnectionManager, telegram_uid: int) -> bool:
    uid = int(telegram_uid)
    if uid in load_activated_uids():
        return True
    try:
        subs = await list_subscribed_object_ids(db, uid)
    except Exception as e:
        logger.warning("is_activated DB check failed uid=%s: %s", uid, e)
        return False
    if subs:
        # Grandfather existing subscribers into the activated set
        activate_user(uid)
        return True
    return False
