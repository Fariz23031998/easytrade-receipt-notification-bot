"""Poll doc_sales for new sales and format receipt messages for store staff."""
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
STATE_FILE = os.path.join(_SCRIPT_DIR, "receipt_state.json")
STATE_KEY = "doc_sales_last_update_time"

NOTIFY_STATUSES = {2, 5, 6, 7}


def load_last_doc_sales_checked_time() -> Optional[str]:
    if not os.path.exists(STATE_FILE):
        logger.info(
            "Receipt state file missing (%s), first run will skip backlog", STATE_FILE
        )
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            ts = data.get(STATE_KEY)
            logger.info("Loaded %s=%s from %s", STATE_KEY, ts, STATE_FILE)
            return ts
    except Exception as e:
        logger.warning("Failed to load receipt state from %s: %s", STATE_FILE, e)
        return None


def save_last_doc_sales_checked_time(ts: str) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({STATE_KEY: ts}, f, indent=2)
        logger.info("Saved %s=%s to %s", STATE_KEY, ts, STATE_FILE)
    except Exception as e:
        logger.warning("Failed to save receipt state to %s: %s", STATE_FILE, e)


async def get_new_sales(
    db: MySQLConnectionManager, doc_sales_last_update_time: Optional[str]
) -> list[dict]:
    """
    Fetch sales with sls_last_update > checkpoint.
    Returns list of sale rows including sls_object (no customer telegram filter).
    """
    if doc_sales_last_update_time is None:
        doc_sales_last_update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            "Receipt check: no previous state, using now (%s) — no backlog",
            doc_sales_last_update_time,
        )
    rows = await db.execute(
        """
        SELECT s.sls_id, s.sls_type, s.sls_status, s.sls_object, s.sls_customer,
               s.sls_operating_cash, s.sls_datetime, s.sls_discount_card, s.sls_last_update,
               COALESCE(o.obj_name, '') AS obj_name,
               CASE
                 WHEN s.sls_customer > 0 THEN COALESCE(NULLIF(TRIM(c.cstm_name), ''), '')
                 ELSE ''
               END AS customer_name,
               COALESCE(
                 NULLIF(TRIM(cash.ocs_description), ''),
                 NULLIF(TRIM(cash.ocs_sn), ''),
                 ''
               ) AS cash_name
        FROM doc_sales s
        LEFT JOIN dir_objects o ON o.obj_id = s.sls_object
        LEFT JOIN dir_customers c
          ON c.cstm_id = s.sls_customer AND c.cstm_deleted = 0
        LEFT JOIN dir_operating_cashes cash ON cash.ocs_id = s.sls_operating_cash
        WHERE s.sls_last_update > %s
          AND s.sls_deleted = 0 AND s.sls_deleted_mark = 0
        ORDER BY s.sls_last_update
        """,
        (doc_sales_last_update_time,),
    )
    logger.info("Receipt check: doc_sales returned %d row(s)", len(rows) if rows else 0)
    return list(rows) if rows else []


async def get_recipients_for_sale(
    db: MySQLConnectionManager, sls_object: int
) -> list[int]:
    try:
        obj_id = int(sls_object) if sls_object else 0
    except (TypeError, ValueError):
        obj_id = 0
    if obj_id <= 0:
        return []
    return await get_recipients_for_object(db, obj_id)


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _format_number(x: float) -> str:
    return f"{float(x):,.0f}".replace(",", " ")


async def get_card_scan(db: MySQLConnectionManager, card_id: int) -> str:
    if not card_id:
        return ""
    row = await db.execute_one(
        "SELECT dscc_scan FROM dir_discount_cards WHERE dscc_id = %s",
        (card_id,),
    )
    return (row.get("dscc_scan") or "").strip() if row else ""


async def get_receipt_lines_for_document(
    db: MySQLConnectionManager, document_id: int
) -> tuple[list[dict], float]:
    try:
        rows = await db.execute(
            """
            SELECT o.opr_id, o.opr_good, o.opr_quantity,
                   COALESCE(g.gd_name, '') AS gd_name,
                   COALESCE(oap.oap_price1, 0) AS price,
                   COALESCE(oap.oap_price2, 0) AS price2
            FROM operations o
            LEFT JOIN dir_goods g ON g.gd_id = o.opr_good
            LEFT JOIN operations_additional_prop oap ON oap.oap_operation = o.opr_id
            WHERE o.opr_document = %s
            ORDER BY o.opr_id
            """,
            (document_id,),
        )
        if not rows:
            return [], 0.0
        result = []
        total = 0.0
        for r in rows:
            name = (r.get("gd_name") or "").strip()
            qty = _safe_float(r.get("opr_quantity"))
            price = _safe_float(r.get("price"))
            price2 = _safe_float(r.get("price2"))
            line_sum = qty * price
            total += line_sum
            discount = qty * (price2 - price) if price < price2 else 0.0
            result.append(
                {
                    "name": name or "—",
                    "quantity": qty,
                    "price": price,
                    "sum": line_sum,
                    "discount": discount,
                }
            )
        return result, total
    except Exception as e:
        logger.warning(
            "get_receipt_lines_for_document(document_id=%s) failed: %s", document_id, e
        )
        return [], 0.0


async def get_payments_for_sale(
    db: MySQLConnectionManager, sls_id: int
) -> list[dict]:
    try:
        rows = await db.execute(
            """
            SELECT t.pmtt_name, p.pmt_value
            FROM doc_payments p
            JOIN dir_payments_types t ON t.pmtt_id = p.pmt_type
                AND (t.pmtt_deleted = 0 OR t.pmtt_deleted IS NULL)
            WHERE p.pmt_document = %s AND p.pmt_value > 0
            ORDER BY p.pmt_id
            """,
            (sls_id,),
        )
        if not rows:
            return []
        out = []
        for r in rows:
            name = (r.get("pmtt_name") or "").strip()
            val = _safe_float(r.get("pmt_value"))
            if name and val > 0:
                out.append({"name": name, "value": val})
        return out
    except Exception as e:
        logger.warning("get_payments_for_sale(sls_id=%s) failed: %s", sls_id, e)
        return []


def format_receipt_message(
    lines: list[dict],
    total_amount: float,
    payment_lines: list[dict],
    sls_type: Optional[int],
    sls_datetime: Optional[datetime],
    card_scan: str = "",
    sls_status: Optional[int] = None,
    obj_name: str = "",
    customer_name: str = "",
    cash_name: str = "",
) -> list[str]:
    if sls_status == 6:
        header = translations.get("receipt.cancelled", "*Cancelled*")
    elif sls_status == 2 and lines:
        header = translations.get("receipt.cancelled", "*Cancelled*")
    else:
        header = (
            translations.get("receipt.sales", "*Sales*")
            if sls_type == 2
            else translations.get("receipt.return", "*Return*")
        )
    parts = [header]
    if obj_name:
        safe_name = obj_name.replace("*", "\\*")
        parts.append(
            f"{translations.get('receipt.object', 'Store')}: {safe_name}"
        )
    if cash_name:
        parts.append(
            f"{translations.get('receipt.cash', 'Cash register')}: "
            f"{cash_name.replace('*', '\\*')}"
        )
    if customer_name:
        parts.append(
            f"{translations.get('receipt.customer', 'Customer')}: "
            f"{customer_name.replace('*', '\\*')}"
        )
    try:
        dt_str = (
            sls_datetime.strftime("%d.%m.%Y %H:%M")
            if hasattr(sls_datetime, "strftime") and sls_datetime
            else (str(sls_datetime) if sls_datetime else "")
        )
    except Exception:
        dt_str = str(sls_datetime) if sls_datetime else ""
    if dt_str:
        parts.append(f"{translations.get('receipt.date', 'Date')}: {dt_str}")
    if card_scan:
        parts.append(
            f"{translations.get('receipt.card', 'Card')}: `{card_scan}`"
        )
    parts.append("")
    total_discount = 0.0
    for i, line in enumerate(lines, 1):
        name = (line.get("name") or "—").replace("*", "\\*")
        qty = line.get("quantity", 0)
        price = line.get("price", 0)
        s = line.get("sum", qty * price)
        discount = _safe_float(line.get("discount"))
        total_discount += discount
        parts.append(f"{i}. {name}")
        parts.append(
            f"{_format_number(qty)} × {_format_number(price)} = {_format_number(s)}"
        )
        if discount > 0:
            parts.append(
                f"{translations.get('receipt.discount', 'Discount')}: "
                f"{_format_number(discount)}"
            )
        parts.append("")
    if total_discount > 0:
        parts.append(
            translations.get(
                "receipt.has_discount",
                "Includes a discount: {amount}",
            ).format(amount=_format_number(total_discount))
        )
        parts.append("")
    parts.append(
        f"*{translations.get('receipt.total', 'Total')}: {_format_number(total_amount)}*"
    )
    sum_payments = sum(_safe_float(p.get("value")) for p in payment_lines)
    for p in payment_lines:
        val = _safe_float(p.get("value"))
        if val > 0:
            pname = (p.get("name") or "").replace("*", "\\*")
            parts.append(f"{pname}: {_format_number(val)}")
    if total_amount > sum_payments:
        dept = total_amount - sum_payments
        parts.append(
            f"{translations.get('receipt.dept', 'Dept')}: {_format_number(dept)}"
        )

    chunks = []
    current_chunk = ""
    for part in parts:
        if len(current_chunk) + len(part) + 1 > 4000:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
            while len(part) > 4000:
                chunks.append(part[:4000])
                part = part[4000:]
            current_chunk = part
        else:
            current_chunk = current_chunk + "\n" + part if current_chunk else part

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def should_notify_sale(sls_status, lines: list[dict]) -> bool:
    try:
        status = int(sls_status) if sls_status is not None else None
    except (TypeError, ValueError):
        return False
    if status not in NOTIFY_STATUSES:
        return False
    if status == 2 and not lines:
        return False
    return True
