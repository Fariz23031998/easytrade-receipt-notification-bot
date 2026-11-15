"""Read/write dir_extensions_settings for bot token (10001) and send-sales flag (10002)."""
import logging
from typing import Optional

from db import MySQLConnectionManager

logger = logging.getLogger(__name__)

EXTSS_BOT_TOKEN = 10001
EXTSS_SEND_SALES = 10002


async def get_setting(
    db: MySQLConnectionManager, extss_id: int, extension_id: int
) -> Optional[str]:
    row = await db.execute_one(
        """
        SELECT extss_value FROM dir_extensions_settings
        WHERE extss_id = %s AND extss_extension = %s
        """,
        (extss_id, extension_id),
    )
    if not row:
        return None
    return str(row.get("extss_value") or "")


async def set_setting(
    db: MySQLConnectionManager, extss_id: int, extension_id: int, value: str
) -> None:
    await db.execute(
        """
        INSERT INTO dir_extensions_settings (extss_id, extss_extension, extss_value)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE extss_value = VALUES(extss_value)
        """,
        (extss_id, extension_id, value),
    )


async def ensure_settings(
    db: MySQLConnectionManager,
    extension_id: int,
) -> str:
    """
    Ensure setting rows exist; load bot token from EasyTrade UI (extss_id=10001).
    - Token is never written from config — only read from DB.
    - Create 10001 with empty value if missing (UI will set it).
    - Create 10002 with default '1' if missing; never overwrite existing 0/1.
    Returns resolved bot token (may be empty if not set in UI yet).
    """
    existing_token = await get_setting(db, EXTSS_BOT_TOKEN, extension_id)
    existing_flag = await get_setting(db, EXTSS_SEND_SALES, extension_id)

    if existing_token is None:
        await set_setting(db, EXTSS_BOT_TOKEN, extension_id, "")
        token = ""
        logger.warning(
            "No telegram token row yet (extss_id=%s); set it in EasyTrade UI",
            EXTSS_BOT_TOKEN,
        )
    else:
        token = (existing_token or "").strip()
        if token and token not in ("0",):
            logger.info(
                "Using telegram token from dir_extensions_settings extss_id=%s",
                EXTSS_BOT_TOKEN,
            )
        else:
            token = ""
            logger.warning(
                "Telegram token empty in dir_extensions_settings extss_id=%s; set it in EasyTrade UI",
                EXTSS_BOT_TOKEN,
            )

    if existing_flag is None:
        await set_setting(db, EXTSS_SEND_SALES, extension_id, "1")
        logger.info(
            "Created send-sales flag extss_id=%s with default value=1", EXTSS_SEND_SALES
        )
    else:
        logger.info(
            "Send-sales flag extss_id=%s value=%s", EXTSS_SEND_SALES, existing_flag
        )

    return token


async def is_send_sales_enabled(
    db: MySQLConnectionManager, extension_id: int
) -> bool:
    value = await get_setting(db, EXTSS_SEND_SALES, extension_id)
    return str(value or "").strip() == "1"
