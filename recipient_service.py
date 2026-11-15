"""Store list and dir_telegram_recipient upsert/delete."""
import logging
from typing import Optional

from db import MySQLConnectionManager

logger = logging.getLogger(__name__)

STORES_PER_PAGE = 8


async def list_stores(db: MySQLConnectionManager) -> list[dict]:
    rows = await db.execute(
        """
        SELECT obj_id, obj_name FROM dir_objects
        WHERE obj_deleted = 0 AND obj_deleted_mark = 0
        ORDER BY obj_name
        """
    )
    return list(rows) if rows else []


async def get_store_name(db: MySQLConnectionManager, obj_id: int) -> str:
    row = await db.execute_one(
        """
        SELECT obj_name FROM dir_objects
        WHERE obj_id = %s AND obj_deleted = 0 AND obj_deleted_mark = 0
        """,
        (obj_id,),
    )
    return (row.get("obj_name") or "").strip() if row else ""


def display_name(user) -> str:
    """Build tlgr_uname from Telegram user (max 100 chars)."""
    if user is None:
        return ""
    uname = (getattr(user, "username", None) or "").strip()
    if uname:
        return uname[:100]
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = f"{first} {last}".strip() or str(getattr(user, "id", ""))
    return name[:100]


async def get_recipient(
    db: MySQLConnectionManager, telegram_uid: int, object_id: int
) -> Optional[dict]:
    return await db.execute_one(
        """
        SELECT tlgr_id, tlgr_uid, tlgr_uname, tlgr_object
        FROM dir_telegram_recipient
        WHERE tlgr_uid = %s AND tlgr_object = %s
        """,
        (telegram_uid, object_id),
    )


async def upsert_recipient(
    db: MySQLConnectionManager,
    telegram_uid: int,
    uname: str,
    object_id: int,
) -> bool:
    """
    Insert or update recipient for (uid, object).
    Returns True if newly inserted, False if updated existing.
    """
    existing = await get_recipient(db, telegram_uid, object_id)
    uname = (uname or "")[:100]
    if existing:
        await db.execute(
            """
            UPDATE dir_telegram_recipient
            SET tlgr_uname = %s
            WHERE tlgr_uid = %s AND tlgr_object = %s
            """,
            (uname, telegram_uid, object_id),
        )
        logger.info(
            "Updated recipient uid=%s object=%s uname=%s", telegram_uid, object_id, uname
        )
        return False
    await db.execute_insert(
        """
        INSERT INTO dir_telegram_recipient (tlgr_uid, tlgr_uname, tlgr_object)
        VALUES (%s, %s, %s)
        """,
        (telegram_uid, uname, object_id),
    )
    logger.info(
        "Inserted recipient uid=%s object=%s uname=%s", telegram_uid, object_id, uname
    )
    return True


async def delete_recipient(
    db: MySQLConnectionManager, telegram_uid: int, object_id: int
) -> bool:
    row = await get_recipient(db, telegram_uid, object_id)
    if not row:
        return False
    await db.execute(
        """
        DELETE FROM dir_telegram_recipient
        WHERE tlgr_uid = %s AND tlgr_object = %s
        """,
        (telegram_uid, object_id),
    )
    logger.info("Deleted recipient uid=%s object=%s", telegram_uid, object_id)
    return True


async def list_user_subscriptions(
    db: MySQLConnectionManager, telegram_uid: int
) -> list[dict]:
    rows = await db.execute(
        """
        SELECT r.tlgr_object AS obj_id, COALESCE(o.obj_name, '') AS obj_name
        FROM dir_telegram_recipient r
        LEFT JOIN dir_objects o ON o.obj_id = r.tlgr_object
        WHERE r.tlgr_uid = %s
        ORDER BY o.obj_name
        """,
        (telegram_uid,),
    )
    return list(rows) if rows else []


async def list_subscribed_object_ids(
    db: MySQLConnectionManager, telegram_uid: int
) -> set[int]:
    rows = await db.execute(
        "SELECT tlgr_object FROM dir_telegram_recipient WHERE tlgr_uid = %s",
        (telegram_uid,),
    )
    out: set[int] = set()
    for r in rows or []:
        try:
            out.add(int(r["tlgr_object"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


async def subscribe_all_stores(
    db: MySQLConnectionManager, telegram_uid: int, uname: str
) -> int:
    """Subscribe user to every active store. Returns number of newly added rows."""
    stores = await list_stores(db)
    added = 0
    for s in stores:
        if await upsert_recipient(db, telegram_uid, uname, int(s["obj_id"])):
            added += 1
    return added


async def unsubscribe_all_stores(
    db: MySQLConnectionManager, telegram_uid: int
) -> int:
    """Remove all subscriptions for user. Returns deleted row count."""
    before = await list_subscribed_object_ids(db, telegram_uid)
    if not before:
        return 0
    await db.execute(
        "DELETE FROM dir_telegram_recipient WHERE tlgr_uid = %s",
        (telegram_uid,),
    )
    return len(before)


async def get_recipients_for_object(
    db: MySQLConnectionManager, object_id: int
) -> list[int]:
    rows = await db.execute(
        """
        SELECT tlgr_uid FROM dir_telegram_recipient
        WHERE tlgr_object = %s
        """,
        (object_id,),
    )
    if not rows:
        return []
    out = []
    for r in rows:
        try:
            out.append(int(r["tlgr_uid"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out
