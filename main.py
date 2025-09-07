import os
import json
import asyncio
import logging
import uuid
import re
import csv
import io
from datetime import datetime
from typing import Dict, List, Optional, Set
import aiofiles
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence,
)
from telegram.helpers import escape_markdown
from functools import wraps
import time

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_GROUP_ID_STR = os.getenv("ADMIN_GROUP_ID")
ADMINS_STR = os.getenv("ADMINS")
CARD_NUMBER = os.getenv("CARD_NUMBER")
CARD_NAME = os.getenv("CARD_NAME")

CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
BLACKLIST_FILE = "blacklist.txt"

# Global counters and caches
users_cache: Set[int] = set()
orders: Dict[str, Dict] = {}
configs: Dict[int, Dict] = {}
blacklist: Set[int] = set()
config_id_counter = 1  # Simple int counter, reset on load

# Simple rate limiter: user_id -> last_action_time
rate_limiter: Dict[int, float] = {}

# Pagination settings
ORDERS_PER_PAGE = 5  # For pagination in list_orders

# ===== Logging =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Conversation States =====
ADD_CONFIG_VOLUME, ADD_CONFIG_DURATION, ADD_CONFIG_PRICE, ADD_CONFIG_LINK = range(4)
REMOVE_CONFIG_ID = 0
BULK_APPROVE_IDS = 1  # For bulk actions

# ===== Data Manager Class =====
class DataManager:
    @staticmethod
    async def check_env():
        missing = []
        if not TOKEN:
            missing.append("TOKEN")
        if not WEBHOOK_URL:
            missing.append("WEBHOOK_URL")
        else:
            if not WEBHOOK_URL.startswith("https://"):
                raise ValueError("WEBHOOK_URL باید با HTTPS شروع شود.")
            from urllib.parse import urlparse
            parsed = urlparse(WEBHOOK_URL)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("WEBHOOK_URL فرمت نامعتبر دارد!")
        if not ADMIN_GROUP_ID_STR:
            missing.append("ADMIN_GROUP_ID")
        if not ADMINS_STR:
            missing.append("ADMINS")
        if not CARD_NUMBER:
            missing.append("CARD_NUMBER")
        if not CARD_NAME:
            missing.append("CARD_NAME")
        if missing:
            raise ValueError(f"❌ متغیرهای محیطی زیر ست نشده‌اند: {', '.join(missing)}")

    @staticmethod
    async def save_user(user_id: int) -> int:
        global users_cache
        if user_id not in users_cache and isinstance(user_id, int) and user_id > 0:
            async with aiofiles.open(USERS_FILE, "a", encoding="utf-8") as f:
                await f.write(f"{user_id}\n")
            users_cache.add(user_id)
        return len(users_cache)

    @staticmethod
    async def load_configs():
        global configs, config_id_counter
        if os.path.exists(CONFIG_FILE):
            try:
                async with aiofiles.open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                loaded = json.loads(content)
                configs = {cfg["id"]: cfg for cfg in loaded if "id" in cfg}
                config_id_counter = (max(configs.keys()) + 1) if configs else 1
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"خطا در بارگذاری configs: {e}")
                configs = {}
                config_id_counter = 1
        else:
            configs = {}
            config_id_counter = 1

    @staticmethod
    async def save_configs():
        async with aiofiles.open(CONFIG_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(list(configs.values()), ensure_ascii=False, indent=2))

    @staticmethod
    async def load_orders():
        global orders
        if os.path.exists(ORDERS_FILE):
            try:
                async with aiofiles.open(ORDERS_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                orders = json.loads(content)
                for order_id, order in orders.items():
                    if "timestamp" not in order:
                        orders[order_id]["timestamp"] = datetime.now().isoformat()
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"خطا در بارگذاری orders: {e}")
                orders = {}
        else:
            orders = {}

    @staticmethod
    async def save_orders():
        async with aiofiles.open(ORDERS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(orders, ensure_ascii=False, indent=2, default=str))

    @staticmethod
    async def load_blacklist():
        global blacklist
        if os.path.exists(BLACKLIST_FILE):
            try:
                async with aiofiles.open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                blacklist = {int(line) for line in lines if line.isdigit()}
            except ValueError:
                logger.error("خطا در بارگذاری blacklist: ID نامعتبر")
                blacklist = set()
        else:
            blacklist = set()

    @staticmethod
    async def save_blacklist():
        async with aiofiles.open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for user_id in sorted(blacklist):
                await f.write(f"{user_id}\n")

    @staticmethod
    async def load_users_cache():
        global users_cache
        if os.path.exists(USERS_FILE):
            try:
                async with aiofiles.open(USERS_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                users_cache = {int(line) for line in lines if line.isdigit()}
            except ValueError:
                logger.error("خطا در بارگذاری users_cache")
                users_cache = set()
        else:
            users_cache = set()

    @staticmethod
    def get_stats() -> str:
        total_configs = len(configs)
        total_orders = len(orders)
        pending_orders = sum(1 for order in orders.values() if order.get('status') == 'pending')
        return f"📊 آمار:\nکاربران: {len(users_cache)}\nکانفیگ‌ها: {total_configs}\nسفارش‌ها: {total_orders}\nسفارش‌های در انتظار: {pending_orders}"

    @staticmethod
    def group_configs() -> Dict[str, List[Dict]]:
        grouped: Dict[str, List[Dict]] = {}
        for config in configs.values():
            key = f"{config['volume']} - {config['duration']}"
            grouped.setdefault(key, []).append(config)
        return grouped

    @staticmethod
    def export_orders_csv() -> bytes:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=['order_id', 'user_id', 'username', 'config_id', 'status', 'timestamp'])
        writer.writeheader()
        for order_id, order in orders.items():
            row = order.copy()
            row['order_id'] = order_id
            writer.writerow(row)
        return output.getvalue().encode('utf-8')

    @staticmethod
    def export_stats_csv() -> bytes:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['نوع آمار', 'مقدار'])
        writer.writerow(['کاربران', len(users_cache)])
        writer.writerow(['کانفیگ‌ها', len(configs)])
        writer.writerow(['سفارش‌ها', len(orders)])
        writer.writerow(['سفارش‌های در انتظار', sum(1 for o in orders.values() if o.get('status') == 'pending')])
        return output.getvalue().encode('utf-8')

# Global admins and group_id after check
ADMINS: List[int] = []
ADMIN_GROUP_ID: int = 0

# Rate limit helper
def is_rate_limited(user_id: int, window: int = 5) -> bool:
    """ساده: هر کاربر هر 5 ثانیه یک عمل."""
    now = time.monotonic()
    last = rate_limiter.get(user_id, 0)
    if now - last < window:
        return True
    rate_limiter[user_id] = now
    return False

# Blacklist check decorator
def check_blacklist(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id if user else None
        if user_id is not None and user_id in blacklist:
            try:
                if update.message:
                    await update.message.reply_text("⛔ شما مسدود شده‌اید.")
                elif update.callback_query:
                    await update.callback_query.answer("⛔ شما مسدود شده‌اید.")
            except Exception:
                pass
            return
        return await func(update, context)
    return wrapper

# ===== Handlers =====
@check_blacklist
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید.")
        return
    await DataManager.save_user(user_id)
    keyboard = [
        [InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
        [InlineKeyboardButton("📞تماس با پشتیبانی", callback_data="support")],
    ]
    if user_id in ADMINS:
        keyboard.append([InlineKeyboardButton("🔧 پنل ادمین", callback_data="admin_panel")])
    await update.message.reply_text(
        "سلام 👋\nبه ماناوا خوش آمدید.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if is_rate_limited(user_id):
        await query.answer("⏳ لطفاً کمی صبر کنید.")
        return

    if user_id in blacklist:
        await query.answer("⛔ شما مسدود شده‌اید.")
        return

    data = query.data or ""

    if data == "buy":
        if not configs:
            await query.edit_message_text(" موجودی سرور ها تمام شده،جهت ثبت سفارش به پشتیبانی مراجعه کنید.")
            return
        grouped = DataManager.group_configs()
        keyboard = []
        for key, cfgs in grouped.items():
            if cfgs:
                keyboard.append([InlineKeyboardButton(f"{key} (موجود: {len(cfgs)})", callback_data=f"buy_config_{cfgs[0]['id']}")])
        keyboard.append([InlineKeyboardButton("لغو", callback_data="cancel")])
        await query.edit_message_text("لطفاً یک کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "support":
        await query.edit_message_text("پشتیبانی: @manava_vpn")

    elif data == "admin_panel":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        admin_keyboard = [
            [InlineKeyboardButton("📊 آمار", callback_data="admin_stats")],
            [InlineKeyboardButton("📋 لیست سفارش‌ها", callback_data="admin_list_orders")],
            [InlineKeyboardButton("➕ اضافه کانفیگ", callback_data="admin_add_config")],
            [InlineKeyboardButton("➖ حذف کانفیگ", callback_data="admin_remove_config")],
            [InlineKeyboardButton("📤 اکسپورت داده‌ها", callback_data="admin_export")],
            [InlineKeyboardButton("🚫 Bulk Actions", callback_data="admin_bulk")],
            [InlineKeyboardButton("❌ بستن", callback_data="admin_close")],
        ]
        await query.edit_message_text("🔧 پنل ادمین:", reply_markup=InlineKeyboardMarkup(admin_keyboard))

    elif data == "admin_stats":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        stats_text = DataManager.get_stats()
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "admin_list_orders":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        await show_orders_page(query, context, page=1)

    elif data.startswith("orders_page_"):
        try:
            page = int(data.split("_")[2])
        except Exception:
            page = 1
        await show_orders_page(query, context, page)

    elif data.startswith("order_approve_") or data.startswith("order_reject_"):
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        action = "approve" if data.startswith("order_approve_") else "reject"
        order_id = data.split("_")[2]
        await process_order_action(query, context, order_id, action)

    elif data == "admin_add_config":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        await query.edit_message_text("برای اضافه کانفیگ، از دستور /add_config استفاده کنید.")
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]
        await query.edit_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "admin_remove_config":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        await query.edit_message_text("برای حذف کانفیگ، از دستور /remove_config استفاده کنید.")
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]
        await query.edit_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "admin_export":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        export_keyboard = [
            [InlineKeyboardButton("📋 اکسپورت سفارش‌ها", callback_data="export_orders")],
            [InlineKeyboardButton("📊 اکسپورت آمار", callback_data="export_stats")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")],
        ]
        await query.edit_message_text("انتخاب کنید چه چیزی را اکسپورت کنید:", reply_markup=InlineKeyboardMarkup(export_keyboard))

    elif data == "export_orders":
        csv_data = DataManager.export_orders_csv()
        await query.message.reply_document(
            document=("orders.csv", csv_data),
            caption="فایل CSV سفارش‌ها",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]),
        )
        with contextlib_suppress():
            await query.delete_message()

    elif data == "export_stats":
        csv_data = DataManager.export_stats_csv()
        await query.message.reply_document(
            document=("stats.csv", csv_data),
            caption="فایل CSV آمار",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]),
        )
        with contextlib_suppress():
            await query.delete_message()

    elif data == "admin_bulk":
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        bulk_keyboard = [
            [InlineKeyboardButton("✅ تأیید گروهی", callback_data="bulk_approve")],
            [InlineKeyboardButton("❌ رد گروهی", callback_data="bulk_reject")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")],
        ]
        await query.edit_message_text(
            "برای Bulk Actions، IDهای سفارش را با کاما جدا کنید (مثل id1,id2):",
            reply_markup=InlineKeyboardMarkup(bulk_keyboard),
        )

    elif data in ["bulk_approve", "bulk_reject"]:
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        action = "approve" if data == "bulk_approve" else "reject"
        await query.edit_message_text(f"IDهای سفارش برای {action} گروهی را وارد کنید (با کاما جدا):")
        context.user_data['bulk_action'] = action
        return BULK_APPROVE_IDS

    elif data == "admin_close":
        await query.edit_message_text("پنل ادمین بسته شد.")
        return

    elif data.startswith("buy_config_"):
        try:
            config_id = int(data.split("_")[2])
            config = configs.get(config_id)
            if not config:
                await query.edit_message_text("کانفیگ یافت نشد.")
                return

            order_id = str(uuid.uuid4())
            orders[order_id] = {
                'user_id': user_id,
                'username': query.from_user.username or "بدون یوزرنیم",
                'config_id': config_id,
                'status': 'pending',
                'timestamp': datetime.now().isoformat(),
            }
            await DataManager.save_orders()

            price_md = escape_markdown(str(config['price']), version=2)
            cn_md = escape_markdown(CARD_NUMBER, version=2)
            nm_md = escape_markdown(CARD_NAME, version=2)
            oid_md = escape_markdown(order_id, version=2)

            text = (
                f"لطفاً مبلغ `{price_md}` تومان به شماره کارت زیر واریز کنید:\n"
                f"`{cn_md}`\nنام: {nm_md}\nID سفارش: `{oid_md}`\n"
                "لطفاً عکس رسید پرداخت خود را همینجا ارسال کنید.\n\n💡 برای کپی ID سفارش، روی آن لمس کنید و کپی کنید."
            )
            await query.edit_message_text(text=text, parse_mode='MarkdownV2')
            context.user_data['pending_order_id'] = order_id
        except ValueError:
            await query.edit_message_text("خطا در انتخاب کانفیگ.")
        except Exception as e:
            logger.error(f"خطا در buy_config: {e}")
            await query.edit_message_text("خطا در ثبت سفارش. لطفاً دوباره تلاش کنید.")

    elif data.startswith("approve_") or data.startswith("reject_"):
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        action = "approve" if data.startswith("approve_") else "reject"
        order_id = data.split("_")[1]
        await process_order_action(query, context, order_id, action)

    elif data == "cancel":
        await query.edit_message_text("عملیات لغو شد.")
        if 'pending_order_id' in context.user_data:
            del context.user_data['pending_order_id']

# Helper for approve/reject (used in multiple places)
async def process_order_action(query, context, order_id: str, action: str):
    if order_id not in orders:
        await query.answer("سفارش یافت نشد!")
        return

    order = orders[order_id]
    if order['status'] != 'pending':
        await query.answer("این سفارش قبلاً پردازش شده است!")
        return

    config = configs.get(order['config_id'])
    if action == "approve" and not config:
        await query.answer("کانفیگ یافت نشد!")
        return

    try:
        user_id = order['user_id']
        if action == "approve":
            link_md = escape_markdown(config['link'], version=2)
            oid_md = escape_markdown(order_id, version=2)
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ پرداخت شما تأیید شد!\n🎉 کانفیگ شما:\n`{link_md}`\n\n"
                    f"ID سفارش: `{oid_md}`\n💡 برای کپی ID، روی آن لمس کنید."
                ),
                parse_mode='MarkdownV2',
            )
            orders[order_id]['status'] = 'approved'
            configs.pop(order['config_id'], None)
            await DataManager.save_configs()
            status_text = "✅ پرداخت تأیید شد"
        else:
            oid_md = escape_markdown(order_id, version=2)
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ پرداخت شما رد شد!\n⚠️ لطفاً به پشتیبانی مراجعه کنید: @manava_vpn\n\n"
                    f"ID سفارش: `{oid_md}`\n💡 برای کپی ID، روی آن لمس کنید."
                ),
                parse_mode='MarkdownV2',
            )
            orders[order_id]['status'] = 'rejected'
            status_text = "❌ پرداخت رد شد"

        await DataManager.save_orders()

        oid_md2 = escape_markdown(order_id, version=2)
        await query.edit_message_text(
            text=f"{status_text}:\n👤 کاربر: {order['user_id']}\n📋 ID سفارش: `{oid_md2}`\n💡 برای کپی ID، روی آن لمس کنید.",
            reply_markup=None,
            parse_mode='MarkdownV2',
        )

    except Exception as e:
        logger.error(f"خطا در {action}: {e}")
        await query.answer("خطا در پردازش!")

# Idea 2: Pagination for list_orders
async def show_orders_page(target, context, page: int):
    pending = [(oid, o) for oid, o in orders.items() if o.get('status') == 'pending']
    pending_orders = sorted(pending, key=lambda x: x[1].get('timestamp', ''), reverse=True)
    total = len(pending_orders)
    total_pages = max(1, (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * ORDERS_PER_PAGE
    end_idx = start_idx + ORDERS_PER_PAGE
    page_orders = pending_orders[start_idx:end_idx]

    if total == 0:
        text = "هیچ سفارش در انتظاری وجود ندارد."
    else:
        text = f"📋 سفارش‌های در انتظار (صفحه {page}/{total_pages}):\n\n"

    keyboard_rows = []

    for oid, o in page_orders:
        config_id = o['config_id']
        config = configs.get(config_id)
        config_info = f"{config['volume']} - {config['duration']}" if config else "نامشخص (حذف شده)"
        oid_md = escape_markdown(oid, version=2)
        username = o.get('username') or "—"
        text += (
            f"🆔 ID سفارش: `{oid_md}`\n"
            f"👤 کاربر: {o['user_id']} (@{username})\n"
            f"⚙️ کانفیگ: {config_info}\n"
            f"⏰ زمان: {o.get('timestamp', 'نامشخص')}\n\n"
        )
        keyboard_rows.append([
            InlineKeyboardButton("✅ تأیید", callback_data=f"order_approve_{oid}"),
            InlineKeyboardButton("❌ رد", callback_data=f"order_reject_{oid}"),
        ])

    # Pagination buttons
    pag_buttons = []
    if page > 1:
        pag_buttons.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"orders_page_{page-1}"))
    if page < total_pages:
        pag_buttons.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"orders_page_{page+1}"))
    if pag_buttons:
        keyboard_rows.append(pag_buttons)
    keyboard_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    # target می‌تواند Update.callback_query یا Update باشد
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')
    else:
        await target.message.reply_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')

    context.user_data['orders_page'] = page

@check_blacklist
async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMINS:
        return  # Ignore for admins
    if is_rate_limited(user_id):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید.")
        return
    if 'pending_order_id' not in context.user_data:
        await update.message.reply_text("لطفاً ابتدا سفارش ثبت کنید.")
        return

    order_id = context.user_data.pop('pending_order_id')
    if order_id not in orders or orders[order_id]['status'] != 'pending':
        await update.message.reply_text("سفارش نامعتبر است.")
        return

    if not update.message.photo:
        await update.message.reply_text("لطفاً عکس رسید ارسال کنید.")
        context.user_data['pending_order_id'] = order_id
        return

    photo_id = update.message.photo[-1].file_id
    orders[order_id]['receipt_photo'] = photo_id
    await DataManager.save_orders()

    await update.message.reply_text("✅ رسید دریافت شد. منتظر تایید ادمین باشید.")

    order = orders[order_id]
    config = configs.get(order['config_id'])
    if not config:
        logger.error(f"کانفیگ یافت نشد برای سفارش: {order_id}")
        return

    user_mention = update.effective_user.mention_html()
    caption_html = (
        f"📨 سفارش جدید با رسید:\n"
        f"👤 کاربر: {user_mention}\n"
        f"🆔 ID کاربر: {order['user_id']}\n"
        f"📋 ID سفارش: <code>{order_id}</code>\n"
        f"⚙️ کانفیگ: {config['volume']} - {config['duration']}\n"
        f"💰 قیمت: {config['price']} تومان\n"
        "🔔 نوتیفیکیشن جدید: لطفاً رسید را بررسی کنید!"
    )

    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأیید پرداخت", callback_data=f"approve_{order_id}"),
            InlineKeyboardButton("❌ رد پرداخت", callback_data=f"reject_{order_id}"),
        ]
    ])

    admin_messages: Dict[int, int] = {}
    for admin in ADMINS:
        try:
            admin_message = await context.bot.send_photo(
                chat_id=admin,
                photo=photo_id,
                caption=caption_html,
                reply_markup=admin_keyboard,
                parse_mode='HTML',
            )
            admin_messages[admin] = admin_message.message_id
        except Exception as e:
            logger.error(f"خطا در ارسال به ادمین {admin}: {e}")

    orders[order_id]['admin_messages'] = admin_messages
    await DataManager.save_orders()

    # Send to group as well
    try:
        group_message = await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=photo_id,
            caption=caption_html.replace("نوتیفیکیشن جدید", "نوتیفیکیشن گروهی"),
            reply_markup=admin_keyboard,
            parse_mode='HTML',
        )
        orders[order_id]['group_chat_id'] = group_message.chat.id
        orders[order_id]['group_message_id'] = group_message.message_id
        await DataManager.save_orders()
    except Exception as e:
        logger.error(f"خطا در ارسال به گروه: {e}")

# Admin Handlers
async def add_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    if is_rate_limited(user_id):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید.")
        return ConversationHandler.END
    await update.message.reply_text("حجم کانفیگ را وارد کنید (مثل 10GB):")
    return ADD_CONFIG_VOLUME

async def add_config_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    volume = update.message.text.strip()
    if not volume:
        await update.message.reply_text("حجم نمی‌تواند خالی باشد. لطفاً دوباره وارد کنید:")
        return ADD_CONFIG_VOLUME
    context.user_data['volume'] = volume
    await update.message.reply_text("مدت زمان (مثل 30 روز):")
    return ADD_CONFIG_DURATION

async def add_config_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    duration = update.message.text.strip()
    if not duration:
        await update.message.reply_text("مدت زمان نمی‌تواند خالی باشد. لطفاً دوباره وارد کنید:")
        return ADD_CONFIG_DURATION
    context.user_data['duration'] = duration
    await update.message.reply_text("قیمت (به تومان، فقط عدد مثبت):")
    return ADD_CONFIG_PRICE

async def add_config_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price_str = update.message.text.strip()
    if not price_str.isdigit() or int(price_str) <= 0:
        await update.message.reply_text("قیمت باید عدد مثبت باشد. لطفاً دوباره وارد کنید:")
        return ADD_CONFIG_PRICE
    context.user_data['price'] = int(price_str)
    await update.message.reply_text("لینک کانفیگ را وارد کنید (باید URL معتبر باشد):")
    return ADD_CONFIG_LINK

async def add_config_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    if not re.match(r'^https?://', link):
        await update.message.reply_text("لینک باید URL معتبر (http/https) باشد. لطفاً دوباره وارد کنید:")
        return ADD_CONFIG_LINK
    global config_id_counter
    new_id = config_id_counter
    config_id_counter += 1
    new_config = {
        'volume': context.user_data['volume'],
        'duration': context.user_data['duration'],
        'price': context.user_data['price'],
        'link': link,
        'id': new_id,
    }
    configs[new_id] = new_config
    await DataManager.save_configs()
    await update.message.reply_text(f"کانفیگ جدید اضافه شد: {new_config}")
    context.user_data.clear()
    return ConversationHandler.END

async def remove_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    await update.message.reply_text("ID کانفیگ را برای حذف وارد کنید:")
    return REMOVE_CONFIG_ID

async def remove_config_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        config_id = int(update.message.text.strip())
        if config_id in configs:
            del configs[config_id]
            await DataManager.save_configs()
            await update.message.reply_text("✅ کانفیگ حذف شد.")
        else:
            await update.message.reply_text("❌ کانفیگ با این ID یافت نشد.")
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر. لطفاً عدد وارد کنید.")
    return ConversationHandler.END

# Bulk actions
async def bulk_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'bulk_action' not in context.user_data:
        return ConversationHandler.END
    action = context.user_data['bulk_action']
    ids_text = update.message.text.strip()
    order_ids = [oid.strip() for oid in ids_text.split(',') if oid.strip()]
    success_count = 0
    for order_id in order_ids:
        if order_id in orders and orders[order_id]['status'] == 'pending':
            order = orders[order_id]
            config = configs.get(order['config_id'])
            if action == 'approve' and config:
                try:
                    link_md = escape_markdown(config['link'], version=2)
                    await context.bot.send_message(
                        chat_id=order['user_id'],
                        text=f"✅ پرداخت شما تأیید شد!\n🎉 کانفیگ شما:\n`{link_md}`",
                        parse_mode='MarkdownV2',
                    )
                    orders[order_id]['status'] = 'approved'
                    configs.pop(order['config_id'], None)
                    success_count += 1
                except Exception as e:
                    logger.error(f"خطا در bulk {action}: {e}")
            elif action == 'reject':
                try:
                    await context.bot.send_message(
                        chat_id=order['user_id'],
                        text="❌ پرداخت شما رد شد!\n⚠️ لطفاً به پشتیبانی مراجعه کنید: @manava_vpn",
                    )
                    orders[order_id]['status'] = 'rejected'
                    success_count += 1
                except Exception as e:
                    logger.error(f"خطا در bulk {action}: {e}")
    await DataManager.save_orders()
    await DataManager.save_configs()
    await update.message.reply_text(f"✅ {success_count} سفارش {action} شد.")
    del context.user_data['bulk_action']
    return ConversationHandler.END

# Command for list_orders
async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    await show_orders_page(update, context, page=1)

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    await update.message.reply_text(DataManager.get_stats())

# Export commands
async def export_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    csv_data = DataManager.export_orders_csv()
    await update.message.reply_document(document=("orders.csv", csv_data), caption="فایل CSV سفارش‌ها")

async def export_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    csv_data = DataManager.export_stats_csv()
    await update.message.reply_document(document=("stats.csv", csv_data), caption="فایل CSV آمار")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    context.user_data.clear()
    return ConversationHandler.END

# Ping route
async def handle_ping(request):
    return web.Response(text="OK")

# Error handler
async def error_handler(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"خطا: {context.error}", exc_info=True)
    try:
        if update and update.message:
            await update.message.reply_text("خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        elif update and update.callback_query:
            await update.callback_query.answer("خطایی رخ داد.")
    except Exception:
        pass

# ===== Utilities =====
class contextlib_suppress:
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc, tb):
        return True

# ===== Main =====
async def main():
    global ADMINS, ADMIN_GROUP_ID
    try:
        await DataManager.check_env()
        ADMIN_GROUP_ID = int(ADMIN_GROUP_ID_STR)
        ADMINS = [int(x.strip()) for x in ADMINS_STR.split(',') if x.strip().isdigit()]
    except (ValueError, AttributeError) as e:
        logger.error(f"خطای محیط: {e}")
        return

    await DataManager.load_users_cache()
    await DataManager.load_orders()
    await DataManager.load_blacklist()
    await DataManager.load_configs()

    application = Application.builder().token(TOKEN).persistence(PicklePersistence(filepath="bot_data.pkl")).build()

    # Conversation Handlers
    add_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add_config", add_config)],
        states={
            ADD_CONFIG_VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_volume)],
            ADD_CONFIG_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_duration)],
            ADD_CONFIG_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_price)],
            ADD_CONFIG_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    remove_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("remove_config", remove_config)],
        states={
            REMOVE_CONFIG_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_config_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    bulk_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^(bulk_approve|bulk_reject)$")],
        states={
            BULK_APPROVE_IDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_action)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(add_conv_handler)
    application.add_handler(remove_conv_handler)
    application.add_handler(bulk_conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list_orders", list_orders))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CommandHandler("export_orders", export_orders))
    application.add_handler(CommandHandler("export_stats", export_stats))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_receipt))
    application.add_error_handler(error_handler)

    await application.initialize()
    await application.start()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")

    app = web.Application()

    async def webhook_handler(request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return web.Response(status=200)
        except Exception as e:
            logger.error(f"خطا در webhook: {e}")
            return web.Response(status=400)

    app.router.add_post(f"/{TOKEN}", webhook_handler)
    app.router.add_get("/ping", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"ربات شروع به کار کرد. پورت: {PORT}")

    # Graceful shutdown
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        logger.info("درخواست خاموشی...")
    finally:
        await application.stop()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
