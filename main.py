"""
Telegram bot: notify store staff on shift open/close and (optionally) each sale/return.
Recipients stored in dir_telegram_recipient; settings in dir_extensions_settings 10001/10002.
"""
import asyncio
import logging
import math
from datetime import datetime

from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)

from helper import translations, config, write_log_file
from db import MySQLConnectionManager
from settings_service import ensure_settings, is_send_sales_enabled
from activation_service import is_activated, activate_user
from recipient_service import (
    STORES_PER_PAGE,
    list_stores,
    get_store_name,
    display_name,
    upsert_recipient,
    delete_recipient,
    list_user_subscriptions,
    list_subscribed_object_ids,
    subscribe_all_stores,
    unsubscribe_all_stores,
)
from shift_service import (
    load_shift_state,
    save_shift_state,
    get_max_sft_id,
    get_max_closed_sft_id,
    get_new_opened_shifts,
    get_newly_closed_shifts,
    format_shift_opened_message,
    format_shift_closed_message,
    resolve_shift_recipients,
    STATE_LAST_SFT,
    STATE_LAST_CLOSED,
)
from receipt_service import (
    load_last_doc_sales_checked_time,
    save_last_doc_sales_checked_time,
    get_new_sales,
    get_recipients_for_sale,
    get_card_scan,
    get_receipt_lines_for_document,
    get_payments_for_sale,
    format_receipt_message,
    should_notify_sale,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

CHECK_TIME_SECONDS = max(1, int((config or {}).get("check_time", 10)))
EXTENSION_ID = int((config or {}).get("extension_id", 10001))
ACTIVATION_CODE = str((config or {}).get("activation_code") or "").strip()

db = MySQLConnectionManager(
    host=(config or {}).get("host", "localhost"),
    port=(config or {}).get("port", 3306),
    user=(config or {}).get("user", "easytrade"),
    password=(config or {}).get("password", "masterkey"),
    db=(config or {}).get("database", "easytrade_db"),
)

bot: AsyncTeleBot | None = None


def t(key: str, default: str = "", **kwargs) -> str:
    text = translations.get(key, default or key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


def build_stores_keyboard(
    stores: list[dict],
    subscribed: set[int],
    page: int = 0,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    total_pages = max(1, math.ceil(len(stores) / STORES_PER_PAGE) if stores else 1)
    page = max(0, min(page, total_pages - 1))
    start = page * STORES_PER_PAGE
    chunk = stores[start : start + STORES_PER_PAGE]
    for s in chunk:
        obj_id = int(s["obj_id"])
        name = (s.get("obj_name") or str(obj_id)).strip()
        mark = "✓ " if obj_id in subscribed else ""
        markup.add(
            InlineKeyboardButton(f"{mark}{name}", callback_data=f"toggle:{obj_id}:{page}")
        )
    markup.row(
        InlineKeyboardButton(
            t("bot.subscribe_all", "Subscribe all"), callback_data=f"sub_all:{page}"
        ),
        InlineKeyboardButton(
            t("bot.unsubscribe_all", "Unsubscribe all"),
            callback_data=f"unsub_all:{page}",
        ),
    )
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                t("bot.prev_page", "◀ Prev"), callback_data=f"stores_page:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                t("bot.next_page", "Next ▶"), callback_data=f"stores_page:{page + 1}"
            )
        )
    if nav:
        markup.row(*nav)
    if total_pages > 1:
        markup.add(
            InlineKeyboardButton(
                t("bot.page", "Page {page}/{total}", page=page + 1, total=total_pages),
                callback_data="noop",
            )
        )
    return markup


def build_mystores_keyboard(subs: list[dict]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for s in subs:
        obj_id = s["obj_id"]
        name = (s.get("obj_name") or str(obj_id)).strip()
        label = f"{t('bot.unsubscribe', 'Unsubscribe')}: {name}"
        markup.add(
            InlineKeyboardButton(label, callback_data=f"unsub:{obj_id}")
        )
    return markup


async def send_store_picker(
    chat_id: int,
    user_id: int,
    page: int = 0,
    edit_message_id: int | None = None,
):
    stores = await list_stores(db)
    if not stores:
        text = t("bot.no_stores", "No stores available.")
        if edit_message_id:
            await bot.edit_message_text(text, chat_id, edit_message_id)
        else:
            await bot.send_message(chat_id, text)
        return
    subscribed = await list_subscribed_object_ids(db, user_id)
    count = len(subscribed)
    text = t(
        "bot.choose_store",
        "Tap stores to subscribe/unsubscribe (you can select several):\nSubscribed: {count}",
        count=count,
    )
    kb = build_stores_keyboard(stores, subscribed, page)
    if edit_message_id:
        try:
            await bot.edit_message_text(
                text, chat_id, edit_message_id, reply_markup=kb
            )
        except Exception:
            # Ignore "message is not modified"
            pass
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)


def register_handlers(bot_instance: AsyncTeleBot):
    async def require_activation(uid: int, chat_id: int) -> bool:
        if await is_activated(db, uid):
            return True
        await bot_instance.send_message(
            chat_id,
            t("bot.enter_code", "Send the activation code to unlock notifications."),
        )
        return False

    async def require_activation_callback(call: CallbackQuery) -> bool:
        uid = call.from_user.id
        if await is_activated(db, uid):
            return True
        await bot_instance.answer_callback_query(
            call.id,
            text=t("bot.not_activated", "Send the activation code first."),
            show_alert=True,
        )
        return False

    @bot_instance.message_handler(commands=["start"])
    async def cmd_start(message: Message):
        try:
            if not await require_activation(message.from_user.id, message.chat.id):
                return
            await send_store_picker(message.chat.id, message.from_user.id, page=0)
        except Exception as e:
            logger.exception("cmd_start error: %s", e)
            write_log_file(f"cmd_start error: {e}")
            await bot_instance.send_message(
                message.chat.id, t("bot.error", "Service unavailable.")
            )

    @bot_instance.message_handler(commands=["mystores"])
    async def cmd_mystores(message: Message):
        try:
            uid = message.from_user.id
            if not await require_activation(uid, message.chat.id):
                return
            subs = await list_user_subscriptions(db, uid)
            if not subs:
                await bot_instance.send_message(
                    message.chat.id,
                    t("bot.mystores_empty", "No subscriptions. Use /start."),
                )
                return
            lines = [t("bot.mystores_title", "Your subscriptions:")]
            for s in subs:
                name = (s.get("obj_name") or str(s.get("obj_id"))).replace("*", "\\*")
                lines.append(f"• {name}")
            await bot_instance.send_message(
                message.chat.id,
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=build_mystores_keyboard(subs),
            )
        except Exception as e:
            logger.exception("cmd_mystores error: %s", e)
            write_log_file(f"cmd_mystores error: {e}")
            await bot_instance.send_message(
                message.chat.id, t("bot.error", "Service unavailable.")
            )

    @bot_instance.message_handler(
        func=lambda m: bool(m.text) and not (m.text or "").startswith("/"),
        content_types=["text"],
    )
    async def cmd_activation_code(message: Message):
        try:
            uid = message.from_user.id
            if await is_activated(db, uid):
                return
            code = (message.text or "").strip()
            if ACTIVATION_CODE and code == ACTIVATION_CODE:
                activate_user(uid)
                await bot_instance.send_message(
                    message.chat.id,
                    t("bot.code_ok", "Activated. You can subscribe to stores."),
                )
                await send_store_picker(message.chat.id, uid, page=0)
            else:
                await bot_instance.send_message(
                    message.chat.id,
                    t("bot.code_bad", "Invalid activation code."),
                )
        except Exception as e:
            logger.exception("cmd_activation_code error: %s", e)
            write_log_file(f"cmd_activation_code error: {e}")
            await bot_instance.send_message(
                message.chat.id, t("bot.error", "Service unavailable.")
            )

    @bot_instance.callback_query_handler(func=lambda c: c.data == "noop")
    async def cb_noop(call: CallbackQuery):
        await bot_instance.answer_callback_query(call.id)

    @bot_instance.callback_query_handler(
        func=lambda c: c.data and c.data.startswith("stores_page:")
    )
    async def cb_stores_page(call: CallbackQuery):
        try:
            if not await require_activation_callback(call):
                return
            page = int(call.data.split(":", 1)[1])
            await send_store_picker(
                call.message.chat.id,
                call.from_user.id,
                page=page,
                edit_message_id=call.message.message_id,
            )
            await bot_instance.answer_callback_query(call.id)
        except Exception as e:
            logger.exception("cb_stores_page error: %s", e)
            await bot_instance.answer_callback_query(call.id, text="Error")

    @bot_instance.callback_query_handler(
        func=lambda c: c.data and c.data.startswith("toggle:")
    )
    async def cb_toggle_store(call: CallbackQuery):
        try:
            if not await require_activation_callback(call):
                return
            parts = call.data.split(":")
            obj_id = int(parts[1])
            page = int(parts[2]) if len(parts) > 2 else 0
            uid = call.from_user.id
            uname = display_name(call.from_user)
            name = await get_store_name(db, obj_id) or str(obj_id)
            existing = await list_subscribed_object_ids(db, uid)
            if obj_id in existing:
                await delete_recipient(db, uid, obj_id)
                toast = t("bot.unsubscribed_short", "Unsubscribed: {name}", name=name)
            else:
                await upsert_recipient(db, uid, uname, obj_id)
                toast = t("bot.subscribed_short", "Subscribed: {name}", name=name)
            await bot_instance.answer_callback_query(call.id, text=toast)
            await send_store_picker(
                call.message.chat.id,
                uid,
                page=page,
                edit_message_id=call.message.message_id,
            )
        except Exception as e:
            logger.exception("cb_toggle_store error: %s", e)
            write_log_file(f"cb_toggle_store error: {e}")
            await bot_instance.answer_callback_query(call.id, text="Error")

    @bot_instance.callback_query_handler(
        func=lambda c: c.data and c.data.startswith("sub_all:")
    )
    async def cb_subscribe_all(call: CallbackQuery):
        try:
            if not await require_activation_callback(call):
                return
            page = int(call.data.split(":", 1)[1])
            uid = call.from_user.id
            uname = display_name(call.from_user)
            added = await subscribe_all_stores(db, uid, uname)
            await bot_instance.answer_callback_query(
                call.id,
                text=t("bot.subscribed_all", "Subscribed to all ({n})", n=added),
            )
            await send_store_picker(
                call.message.chat.id,
                uid,
                page=page,
                edit_message_id=call.message.message_id,
            )
        except Exception as e:
            logger.exception("cb_subscribe_all error: %s", e)
            await bot_instance.answer_callback_query(call.id, text="Error")

    @bot_instance.callback_query_handler(
        func=lambda c: c.data and c.data.startswith("unsub_all:")
    )
    async def cb_unsubscribe_all(call: CallbackQuery):
        try:
            if not await require_activation_callback(call):
                return
            page = int(call.data.split(":", 1)[1])
            uid = call.from_user.id
            removed = await unsubscribe_all_stores(db, uid)
            await bot_instance.answer_callback_query(
                call.id,
                text=t("bot.unsubscribed_all", "Unsubscribed from all ({n})", n=removed),
            )
            await send_store_picker(
                call.message.chat.id,
                uid,
                page=page,
                edit_message_id=call.message.message_id,
            )
        except Exception as e:
            logger.exception("cb_unsubscribe_all error: %s", e)
            await bot_instance.answer_callback_query(call.id, text="Error")

    @bot_instance.callback_query_handler(
        func=lambda c: c.data and c.data.startswith("unsub:")
    )
    async def cb_unsubscribe(call: CallbackQuery):
        try:
            if not await require_activation_callback(call):
                return
            obj_id = int(call.data.split(":", 1)[1])
            uid = call.from_user.id
            name = await get_store_name(db, obj_id) or str(obj_id)
            deleted = await delete_recipient(db, uid, obj_id)
            await bot_instance.answer_callback_query(call.id)
            if deleted:
                await bot_instance.send_message(
                    call.message.chat.id,
                    t(
                        "bot.unsubscribed",
                        "Unsubscribed from: *{name}*",
                        name=name.replace("*", "\\*"),
                    ),
                    parse_mode="Markdown",
                )
            else:
                await bot_instance.send_message(
                    call.message.chat.id,
                    t("bot.mystores_empty", "No subscriptions. Use /start."),
                )
        except Exception as e:
            logger.exception("cb_unsubscribe error: %s", e)
            write_log_file(f"cb_unsubscribe error: {e}")
            await bot_instance.answer_callback_query(call.id, text="Error")


async def shift_check_loop():
    state = load_shift_state()
    last_sft_id = state.get(STATE_LAST_SFT)
    last_closed_sft_id = state.get(STATE_LAST_CLOSED)

    if last_sft_id is None:
        last_sft_id = await get_max_sft_id(db)
        logger.info("Shift check: first run, last_sft_id=%s (no backlog)", last_sft_id)
    else:
        last_sft_id = int(last_sft_id)

    if last_closed_sft_id is None:
        last_closed_sft_id = await get_max_closed_sft_id(db)
        logger.info(
            "Shift check: first run, last_closed_sft_id=%s (no backlog)",
            last_closed_sft_id,
        )
    else:
        last_closed_sft_id = int(last_closed_sft_id)

    save_shift_state(last_sft_id, last_closed_sft_id)

    while True:
        try:
            opened = await get_new_opened_shifts(db, last_sft_id)
            for shift in opened:
                sft_id = int(shift["sft_id"])
                recipients = await resolve_shift_recipients(db, shift)
                text = format_shift_opened_message(shift)
                for uid in recipients:
                    try:
                        await bot.send_message(uid, text, parse_mode="Markdown")
                        logger.info(
                            "Shift open notified uid=%s sft_id=%s", uid, sft_id
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed shift open notify uid=%s sft_id=%s: %s",
                            uid,
                            sft_id,
                            e,
                        )
                        write_log_file(f"Shift open send failed {uid}: {e}")
                if sft_id > last_sft_id:
                    last_sft_id = sft_id

            closed = await get_newly_closed_shifts(db, last_closed_sft_id, last_sft_id)
            for shift in closed:
                sft_id = int(shift["sft_id"])
                recipients = await resolve_shift_recipients(db, shift)
                text = format_shift_closed_message(shift)
                for uid in recipients:
                    try:
                        await bot.send_message(uid, text, parse_mode="Markdown")
                        logger.info(
                            "Shift close notified uid=%s sft_id=%s", uid, sft_id
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed shift close notify uid=%s sft_id=%s: %s",
                            uid,
                            sft_id,
                            e,
                        )
                        write_log_file(f"Shift close send failed {uid}: {e}")
                if sft_id > last_closed_sft_id:
                    last_closed_sft_id = sft_id

            save_shift_state(last_sft_id, last_closed_sft_id)
            await asyncio.sleep(CHECK_TIME_SECONDS)
        except asyncio.CancelledError:
            logger.info("Shift check loop cancelled")
            break
        except Exception as e:
            logger.exception("Shift check loop error: %s", e)
            write_log_file(f"Shift check error: {e}")
            await asyncio.sleep(CHECK_TIME_SECONDS)


async def receipt_check_loop():
    while True:
        try:
            if not await is_send_sales_enabled(db, EXTENSION_ID):
                logger.info(
                    "Receipt check: send-sales flag=0, skipping (sleep %ss)",
                    CHECK_TIME_SECONDS,
                )
                await asyncio.sleep(CHECK_TIME_SECONDS)
                continue

            last_checked = load_last_doc_sales_checked_time()
            new_sales = await get_new_sales(db, last_checked)
            max_sls_last_update = None

            for sale in new_sales:
                sls_id = sale.get("sls_id")
                sls_type = sale.get("sls_type")
                sls_status = sale.get("sls_status")
                sls_datetime = sale.get("sls_datetime")
                sls_discount_card = sale.get("sls_discount_card")
                sls_object = sale.get("sls_object")
                sls_last_update = sale.get("sls_last_update")
                obj_name = (sale.get("obj_name") or "").strip()
                customer_name = (sale.get("customer_name") or "").strip()
                cash_name = (sale.get("cash_name") or "").strip()

                if sls_last_update is not None:
                    if max_sls_last_update is None or sls_last_update > max_sls_last_update:
                        max_sls_last_update = sls_last_update

                lines, total_amount = await get_receipt_lines_for_document(db, sls_id)
                if not should_notify_sale(sls_status, lines):
                    logger.info(
                        "Receipt check: sls_id=%s status=%s skipped",
                        sls_id,
                        sls_status,
                    )
                    continue

                recipients = await get_recipients_for_sale(db, sls_object)
                if not recipients:
                    logger.debug(
                        "Receipt check: sls_id=%s object=%s no recipients",
                        sls_id,
                        sls_object,
                    )
                    continue

                payment_lines = await get_payments_for_sale(db, sls_id)
                card_scan = (
                    await get_card_scan(db, sls_discount_card)
                    if sls_discount_card
                    else ""
                )
                chunks = format_receipt_message(
                    lines,
                    total_amount,
                    payment_lines,
                    sls_type,
                    sls_datetime,
                    card_scan,
                    sls_status,
                    obj_name=obj_name,
                    customer_name=customer_name,
                    cash_name=cash_name,
                )
                for uid in recipients:
                    try:
                        for chunk in chunks:
                            if chunk.strip():
                                await bot.send_message(
                                    uid, chunk, parse_mode="Markdown"
                                )
                        logger.info(
                            "Receipt sent uid=%s sls_id=%s", uid, sls_id
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed receipt uid=%s sls_id=%s: %s", uid, sls_id, e
                        )
                        write_log_file(f"Receipt send failed {uid}: {e}")

            if max_sls_last_update is not None:
                try:
                    ts = (
                        max_sls_last_update.strftime("%Y-%m-%d %H:%M:%S")
                        if hasattr(max_sls_last_update, "strftime")
                        else str(max_sls_last_update)
                    )
                except Exception:
                    ts = str(max_sls_last_update)
                save_last_doc_sales_checked_time(ts)
            elif last_checked is None:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_last_doc_sales_checked_time(ts)
                logger.info(
                    "Receipt check: first run, checkpoint=%s (no backlog)", ts
                )

            await asyncio.sleep(CHECK_TIME_SECONDS)
        except asyncio.CancelledError:
            logger.info("Receipt check loop cancelled")
            break
        except Exception as e:
            logger.exception("Receipt check loop error: %s", e)
            write_log_file(f"Receipt check error: {e}")
            await asyncio.sleep(CHECK_TIME_SECONDS)


async def main():
    global bot

    await db.connect()
    token = await ensure_settings(db, EXTENSION_ID)
    if not token:
        await db.disconnect()
        raise ValueError(
            "Telegram bot token is empty. Set it in EasyTrade UI "
            "(dir_extensions_settings extss_id=10001)."
        )

    bot = AsyncTeleBot(token)
    register_handlers(bot)

    try:
        await bot.set_my_commands(
            [
                BotCommand("start", "Subscribe to stores"),
                BotCommand("mystores", "My subscriptions"),
            ]
        )
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

    shift_task = asyncio.create_task(shift_check_loop())
    receipt_task = asyncio.create_task(receipt_check_loop())
    logger.info(
        "Starting bot (check_time=%ss, extension_id=%s)",
        CHECK_TIME_SECONDS,
        EXTENSION_ID,
    )
    write_log_file("Bot started")

    try:
        await bot.polling(non_stop=True)
    finally:
        for task in (shift_task, receipt_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await db.disconnect()
        write_log_file("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
