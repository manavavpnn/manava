import os
import json
import asyncio
import logging
import uuid
import re
import csv
import io
from io import BytesIO, StringIO
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
import contextlib
import zipfile
import tempfile
import shutil

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_GROUP_ID_STR = os.getenv("ADMIN_GROUP_ID")
ADMINS_STR = os.getenv("ADMINS")
CARD_NUMBER = os.getenv("CARD_NUMBER")
CARD_NAME = os.getenv("CARD_NAME")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")

CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
BLACKLIST_FILE = "blacklist.txt"
PERSISTENCE_FILE = "bot_data.pkl"

# Global counters and caches
users_cache: Set[int] = set()
orders: Dict[str, Dict] = {}
configs: Dict[int, Dict] = {}
blacklist: Set[int] = set()
config_id_counter = 1

# Locks for concurrency
orders_lock = asyncio.Lock()
configs_lock = asyncio.Lock()
users_lock = asyncio.Lock()
blacklist_lock = asyncio.Lock()

# Simple rate limiter
rate_limiter: Dict[int, float] = {}

# Pagination settings
ORDERS_PER_PAGE = 5

# Backup schedule (seconds). Default: 24h
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL_SECONDS", 24 * 3600))

# ===== Logging =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Conversation States =====
ADD_CONFIG_VOLUME, ADD_CONFIG_DURATION, ADD_CONFIG_PRICE, ADD_CONFIG_LINK = range(4)
REMOVE_CONFIG_ID = 0
BULK_APPROVE_IDS = 1

# ===== Utilities =====
def md_escape(s: str) -> str:
    return escape_markdown(str(s), version=2)

def csv_safe(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s)
    if s and s[0] in ('=', '+', '-', '@'):
        return "'" + s
    return s

async def atomic_write(path: str, data: str):
    tmp = f"{path}.tmp"
    async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
        await f.write(data)
    os.replace(tmp, path)

def redact_card(num: Optional[str]) -> str:
    if not num:
        return ""
    if len(num) >= 4:
        return "**** **** **** " + num[-4:]
    return "****"

def is_rate_limited(user_id: int, window: int = 5) -> bool:
    now = time.monotonic()
    last = rate_limiter.get(user_id, 0)
    limited = (now - last) < window
    rate_limiter[user_id] = now
    if len(rate_limiter) > 10000:
        cutoff = now - 300
        for k, v in list(rate_limiter.items()):
            if v < cutoff:
                rate_limiter.pop(k, None)
    return limited

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
                raise ValueError("WEBHOOK_URL must start with https://")
            from urllib.parse import urlparse
            parsed = urlparse(WEBHOOK_URL)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("WEBHOOK_URL format invalid")
        if not ADMIN_GROUP_ID_STR:
            missing.append("ADMIN_GROUP_ID")
        if not ADMINS_STR:
            missing.append("ADMINS")
        if not CARD_NUMBER:
            missing.append("CARD_NUMBER")
        if not CARD_NAME:
            missing.append("CARD_NAME")
        if missing:
            raise ValueError(f"Missing env vars: {', '.join(missing)}")

    @staticmethod
    async def save_user(user_id: int) -> int:
        global users_cache
        if not isinstance(user_id, int) or user_id <= 0:
            return len(users_cache)
        async with users_lock:
            if user_id not in users_cache:
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
                configs = {int(cfg["id"]): cfg for cfg in loaded if "id" in cfg}
                config_id_counter = (max(configs.keys()) + 1) if configs else 1
            except Exception as e:
                logger.error(f"Error loading configs: {e}")
                configs = {}
                config_id_counter = 1
        else:
            configs = {}
            config_id_counter = 1

    @staticmethod
    async def save_configs():
        async with configs_lock:
            await atomic_write(CONFIG_FILE, json.dumps(list(configs.values()), ensure_ascii=False, indent=2))

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
            except Exception as e:
                logger.error(f"Error loading orders: {e}")
                orders = {}
        else:
            orders = {}

    @staticmethod
    async def save_orders():
        async with orders_lock:
            await atomic_write(ORDERS_FILE, json.dumps(orders, ensure_ascii=False, indent=2, default=str))

    @staticmethod
    async def load_blacklist():
        global blacklist
        if os.path.exists(BLACKLIST_FILE):
            try:
                async with aiofiles.open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                with contextlib.suppress(ValueError):
                    blacklist = {int(line) for line in lines if line.isdigit()}
            except Exception as e:
                logger.error(f"Error loading blacklist: {e}")
                blacklist = set()
        else:
            blacklist = set()

    @staticmethod
    async def save_blacklist():
        async with blacklist_lock:
            tmp = StringIO()
            for user_id in sorted(blacklist):
                tmp.write(f"{user_id}\n")
            await atomic_write(BLACKLIST_FILE, tmp.getvalue())

    @staticmethod
    async def load_users_cache():
        global users_cache
        if os.path.exists(USERS_FILE):
            try:
                async with aiofiles.open(USERS_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                with contextlib.suppress(ValueError):
                    users_cache = {int(line) for line in lines if line.isdigit()}
            except Exception as e:
                logger.error(f"Error loading users_cache: {e}")
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
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=['order_id', 'user_id', 'username', 'config_id', 'status', 'timestamp'])
        writer.writeheader()
        for order_id, order in orders.items():
            row = {
                'order_id': order_id,
                'user_id': order.get('user_id', ''),
                'username': csv_safe(order.get('username', '')),
                'config_id': order.get('config_id', ''),
                'status': order.get('status', ''),
                'timestamp': order.get('timestamp', ''),
            }
            writer.writerow(row)
        return output.getvalue().encode('utf-8')

    @staticmethod
    def export_stats_csv() -> bytes:
        output = StringIO()
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

# ===== Backup & Restore =====
async def create_backup_zip(path_list: List[str]) -> str:
    tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
    try:
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for p in path_list:
                if os.path.exists(p):
                    zf.write(p, arcname=os.path.basename(p))
        return zip_path
    except Exception:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

async def backup_data(context: ContextTypes.DEFAULT_TYPE):
    path_list = [CONFIG_FILE, ORDERS_FILE, USERS_FILE, BLACKLIST_FILE]
    try:
        zip_path = await create_backup_zip(path_list)
    except Exception as e:
        logger.error(f"Failed to create backup zip: {e}", exc_info=True)
        return

    try:
        for admin in ADMINS:
            try:
                with open(zip_path, "rb") as fh:
                    await context.bot.send_document(
                        chat_id=admin,
                        document=fh,
                        filename=os.path.basename(zip_path),
                        caption="📦 بکاپ داده‌ها — نگهدارید تا در زمان دیپلوی بعدی استفاده کنید."
                    )
            except Exception as e:
                logger.error(f"Error sending backup to admin {admin}: {e}", exc_info=True)
    finally:
        tmp_dir = os.path.dirname(zip_path)
        try:
            os.remove(zip_path)
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    await update.message.reply_text("⏳ در حال تهیه بکاپ و ارسال به ادمین‌ها...")
    await backup_data(context)
    await update.message.reply_text("✅ بکاپ ارسال شد (درصورت موفقیت به ادمین‌ها).")

async def restore_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    await update.message.reply_text("لطفاً فایل ZIP بکاپ را به صورت یک مستند (Document) برای من ارسال کنید تا بازیابی انجام شود.\nفرمت باید ZIP باشد و شامل فایل‌های configs.json, orders.json, users.txt, blacklist.txt باشد.")

async def restore_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        return
    if not update.message or not update.message.document:
        await update.message.reply_text("فایل دریافت نشد. لطفاً یک فایل ZIP ارسال کنید.")
        return

    doc = update.message.document
    fname = doc.file_name or ""
    if not fname.lower().endswith(".zip"):
        await update.message.reply_text("لطفاً فقط فایل ZIP ارسال کنید.")
        return

    await update.message.reply_text("⏳ فایل دریافت شد، در حال دانلود و بازیابی...")
    try:
        file = await doc.get_file()
        tmp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, fname)
        await file.download_to_drive(zip_path)

        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                if os.path.isabs(member) or ".." in member:
                    continue
            zf.extractall(extract_dir)

        restored_files = []
        for base_name in [CONFIG_FILE, ORDERS_FILE, USERS_FILE, BLACKLIST_FILE]:
            src = os.path.join(extract_dir, base_name)
            if os.path.exists(src):
                dst = os.path.join(os.getcwd(), base_name)
                shutil.copyfile(src, dst)
                restored_files.append(base_name)

        await DataManager.load_configs()
        await DataManager.load_orders()
        await DataManager.load_blacklist()
        await DataManager.load_users_cache()

        await update.message.reply_text(f"✅ بازیابی انجام شد. فایل‌های بازیابی‌شده: {', '.join(restored_files)}")
    except Exception as e:
        logger.error(f"Error restoring backup: {e}", exc_info=True)
        await update.message.reply_text("❌ خطا در بازیابی بکاپ. لاگ بررسی شود.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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
            await query.edit_message_text("موجودی سرور ها تمام شده، جهت ثبت سفارش به پشتیبانی مراجعه کنید.")
            return
        grouped = DataManager.group_configs()
        keyboard = []
        for key, cfgs in grouped.items():
            if cfgs:
                keyboard.append([InlineKeyboardButton(f"{key} (موجود: {len(cfgs)})", callback_data=f"buy_group_{md_escape(key)}")])
        keyboard.append([InlineKeyboardButton("لغو", callback_data="cancel")])
        await query.edit_message_text("لطفاً یک گروه کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("buy_group_"):
        key = data[len("buy_group_"):]
        grouped = DataManager.group_configs()
        cfgs = grouped.get(key, [])
        if not cfgs:
            matched = []
            for k, v in grouped.items():
                if k.startswith(key) or key.startswith(md_escape(k)):
                    matched = v
                    break
            cfgs = matched
        if not cfgs:
            await query.edit_message_text("کانفیگ یافت نشد.")
            return
        keyboard = []
        for cfg in cfgs:
            keyboard.append([InlineKeyboardButton(f"{cfg['volume']} {cfg['duration']} - {cfg['price']} تومان", callback_data=f"buy_config_{cfg['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="buy")])
        await query.edit_message_text("لطفاً یک کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

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
            document=BytesIO(csv_data),
            filename="orders.csv",
            caption="فایل CSV سفارش‌ها",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]),
        )
        with contextlib.suppress(Exception):
            await query.delete_message()

    elif data == "export_stats":
        csv_data = DataManager.export_stats_csv()
        await query.message.reply_document(
            document=BytesIO(csv_data),
            filename="stats.csv",
            caption="فایل CSV آمار",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]),
        )
        with contextlib.suppress(Exception):
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
        except ValueError:
            await query.edit_message_text("خطا در انتخاب کانفیگ.")
            return
        async with configs_lock, orders_lock:
            cfg = configs.pop(config_id, None)
            if not cfg:
                await query.edit_message_text("کانفیگ مورد نظر موجود نیست (ممکن است قبلاً خریداری شده باشد).")
                return
            order_id = str(uuid.uuid4())
            orders[order_id] = {
                'user_id': user_id,
                'username': query.from_user.username or "",
                'config_id': config_id,
                'status': 'pending',
                'timestamp': datetime.now().isoformat(),
                'config_snapshot': cfg,
            }
            await DataManager.save_orders()
            await DataManager.save_configs()

        price_md = md_escape(str(cfg['price']))
        cn_md = md_escape(CARD_NUMBER) if CARD_NUMBER else md_escape(redact_card(CARD_NUMBER))
        nm_safe = md_escape(CARD_NAME or "")
        oid_md = md_escape(order_id)
        text = (
            f"لطفاً مبلغ `{price_md}` تومان به شماره کارت زیر واریز کنید:\n"
            f"`{cn_md}`\nنام: {nm_safe}\nID سفارش: `{oid_md}`\n"
            "لطفاً عکس رسید پرداخت خود را همینجا ارسال کنید.\n\n💡 برای کپی ID سفارش، روی آن لمس کنید و کپی کنید."
        )
        await query.edit_message_text(text=text, parse_mode='MarkdownV2')
        context.user_data['pending_order_id'] = order_id

    elif data.startswith("approve_") or data.startswith("reject_"):
        if user_id not in ADMINS:
            await query.answer("❌ دسترسی ندارید.")
            return
        action = "approve" if data.startswith("approve_") else "reject"
        order_id = data.split("_", 1)[1]
        await process_order_action(query, context, order_id, action)

    elif data == "cancel":
        await query.edit_message_text("عملیات لغو شد.")
        if 'pending_order_id' in context.user_data:
            del context.user_data['pending_order_id']

async def process_order_action(query, context, order_id: str, action: str):
    async with orders_lock:
        if order_id not in orders:
            await query.answer("سفارش یافت نشد!")
            return
        order = orders[order_id]
        if order['status'] != 'pending':
            await query.answer("این سفارش قبلاً پردازش شده است!")
            return
        config_snapshot = order.get('config_snapshot')
        if action == "approve" and not config_snapshot:
            await query.answer("کانفیگ یافت نشد!")
            return
        try:
            user_id = order['user_id']
            if action == "approve":
                link_md = md_escape(config_snapshot.get('link', '')) if config_snapshot else md_escape("link_not_found")
                oid_md = md_escape(order_id)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ پرداخت شما تأیید شد!\n🎉 کانفیگ شما:\n`{link_md}`\n\n"
                        f"ID سفارش: `{oid_md}`\n💡 برای کپی ID، روی آن لمس کنید."
                    ),
                    parse_mode='MarkdownV2',
                )
                orders[order_id]['status'] = 'approved'
                status_text = "✅ پرداخت تأیید شد"
            else:
                oid_md = md_escape(order_id)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "❌ پرداخت شما رد شد!\n⚠️ لطفاً به پشتیبانی مراجعه کنید: @manava_vpn\n\n"
                        f"ID سفارش: `{oid_md}`\n💡 برای کپی ID، روی آن لمس کنید."
                    ),
                    parse_mode='MarkdownV2',
                )
                orders[order_id]['status'] = 'rejected'
                cfg_snapshot = order.get('config_snapshot')
                if cfg_snapshot:
                    async with configs_lock:
                        configs[cfg_snapshot['id']] = cfg_snapshot
                        await DataManager.save_configs()
                status_text = "❌ پرداخت رد شد"

            await DataManager.save_orders()

            oid_md2 = md_escape(order_id)
            display_text = f"{status_text}:\n👤 کاربر: {order['user_id']}\n📋 ID سفارش: `{oid_md2}`\n"
            admin_msgs = order.get('admin_messages', {})
            for admin_id, msg_id in admin_msgs.items():
                with contextlib.suppress(Exception):
                    await context.bot.edit_message_caption(
                        chat_id=admin_id,
                        message_id=msg_id,
                        caption=display_text,
                        reply_markup=None,
                        parse_mode='MarkdownV2'
                    )
            gid = order.get('group_chat_id')
            mid = order.get('group_message_id')
            if gid and mid:
                with contextlib.suppress(Exception):
                    await context.bot.edit_message_caption(
                        chat_id=gid,
                        message_id=mid,
                        caption=display_text,
                        reply_markup=None,
                        parse_mode='MarkdownV2'
                    )

            with contextlib.suppress(Exception):
                await query.edit_message_text(
                    text=display_text,
                    reply_markup=None,
                    parse_mode='MarkdownV2'
                )

        except Exception as e:
            logger.error(f"Error in {action}: {e}", exc_info=True)
            await query.answer("خطا در پردازش!")

async def show_orders_page(target, context, page: int):
    async with orders_lock:
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
        config_id = o.get('config_id')
        cfg = o.get('config_snapshot') or configs.get(config_id)
        config_info = f"{cfg['volume']} - {cfg['duration']}" if cfg else "نامشخص (حذف شده)"
        username = o.get('username') or "—"
        text += (
            f"🆔 ID سفارش: {oid}\n"
            f"👤 کاربر: {o.get('user_id')} (@{username if username else '—'})\n"
            f"⚙️ کانفیگ: {config_info}\n"
            f"⏰ زمان: {o.get('timestamp', 'نامشخص')}\n\n"
        )
        keyboard_rows.append([
            InlineKeyboardButton("✅ تأیید", callback_data=f"order_approve_{oid}"),
            InlineKeyboardButton("❌ رد", callback_data=f"order_reject_{oid}"),
        ])

    pag_buttons = []
    if page > 1:
        pag_buttons.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"orders_page_{page-1}"))
    if page < total_pages:
        pag_buttons.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"orders_page_{page+1}"))
    if pag_buttons:
        keyboard_rows.append(pag_buttons)
    keyboard_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=reply_markup)
    else:
        await target.message.reply_text(text, reply_markup=reply_markup)

    context.user_data['orders_page'] = page

@check_blacklist
async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMINS:
        return
    if is_rate_limited(user_id):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید.")
        return
    if 'pending_order_id' not in context.user_data:
        await update.message.reply_text("لطفاً ابتدا سفارش ثبت کنید.")
        return

    order_id = context.user_data.pop('pending_order_id')
    async with orders_lock:
        if order_id not in orders or orders[order_id]['status'] != 'pending':
            await update.message.reply_text("سفارش نامعتبر است.")
            return

    if not update.message.photo:
        await update.message.reply_text("لطفاً عکس رسید ارسال کنید.")
        context.user_data['pending_order_id'] = order_id
        return

    photo_id = update.message.photo[-1].file_id
    async with orders_lock:
        orders[order_id]['receipt_photo'] = photo_id
        await DataManager.save_orders()

    await update.message.reply_text("✅ رسید دریافت شد. منتظر تایید ادمین باشید.")

    order = orders[order_id]
    cfg = order.get('config_snapshot')
    if not cfg:
        logger.error(f"Config snapshot not found for order: {order_id}")
        return

    user_mention = update.effective_user.mention_html()
    caption_html = (
        f"📨 سفارش جدید با رسید:\n"
        f"👤 کاربر: {user_mention}\n"
        f"🆔 ID کاربر: {order['user_id']}\n"
        f"📋 ID سفارش: <code>{order_id}</code>\n"
        f"⚙️ کانفیگ: {cfg['volume']} - {cfg['duration']}\n"
        f"💰 قیمت: {cfg['price']} تومان\n"
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
            logger.error(f"Error sending to admin {admin}: {e}")

    async with orders_lock:
        orders[order_id]['admin_messages'] = admin_messages
        await DataManager.save_orders()

    try:
        group_message = await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=photo_id,
            caption=caption_html.replace("نوتیفیکیشن جدید", "نوتیفیکیشن گروهی"),
            reply_markup=admin_keyboard,
            parse_mode='HTML',
        )
        async with orders_lock:
            orders[order_id]['group_chat_id'] = group_message.chat.id
            orders[order_id]['group_message_id'] = group_message.message_id
            await DataManager.save_orders()
    except Exception as e:
        logger.error(f"Error sending to group: {e}")

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

async def export_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    csv_data = DataManager.export_orders_csv()
    await update.message.reply_document(document=BytesIO(csv_data), filename="orders.csv", caption="فایل CSV سفارش‌ها")

async def export_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    csv_data = DataManager.export_stats_csv()
    await update.message.reply_document(document=BytesIO(csv_data), filename="stats.csv", caption="فایل CSV آمار")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    context.user_data.clear()
    return ConversationHandler.END

async def add_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    await update.message.reply_text("حجم کانفیگ (مثل 10GB) را وارد کنید:")
    return ADD_CONFIG_VOLUME

async def add_config_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_config'] = {'volume': update.message.text}
    await update.message.reply_text("مدت زمان (مثل 30 روز) را وارد کنید:")
    return ADD_CONFIG_DURATION

async def add_config_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_config']['duration'] = update.message.text
    await update.message.reply_text("قیمت (به تومان) را وارد کنید:")
    return ADD_CONFIG_PRICE

async def add_config_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = int(update.message.text)
        context.user_data['new_config']['price'] = price
        await update.message.reply_text("لینک کانفیگ را وارد کنید:")
        return ADD_CONFIG_LINK
    except ValueError:
        await update.message.reply_text("لطفاً یک عدد معتبر برای قیمت وارد کنید:")
        return ADD_CONFIG_PRICE

async def add_config_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global config_id_counter
    async with configs_lock:
        config = context.user_data.pop('new_config')
        config['id'] = config_id_counter
        config['link'] = update.message.text
        configs[config['id']] = config
        config_id_counter += 1
        await DataManager.save_configs()
    await update.message.reply_text("✅ کانفیگ اضافه شد.")
    return ConversationHandler.END

async def remove_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    await update.message.reply_text("ID کانفیگ برای حذف را وارد کنید:")
    return REMOVE_CONFIG_ID

async def remove_config_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        config_id = int(update.message.text)
        async with configs_lock:
            if config_id in configs:
                del configs[config_id]
                await DataManager.save_configs()
                await update.message.reply_text("✅ کانفیگ حذف شد.")
            else:
                await update.message.reply_text("کانفیگ یافت نشد.")
    except ValueError:
        await update.message.reply_text("لطفاً یک ID معتبر وارد کنید:")
        return REMOVE_CONFIG_ID
    return ConversationHandler.END

async def bulk_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    action = context.user_data.get('bulk_action')
    if not action:
        await update.message.reply_text("خطا: اکشن نامعتبر.")
        return ConversationHandler.END
    order_ids = [oid.strip() for oid in update.message.text.split(',') if oid.strip()]
    if not order_ids:
        await update.message.reply_text("هیچ ID سفارشی وارد نشده است.")
        return ConversationHandler.END
    success = 0
    for order_id in order_ids:
        async with orders_lock:
            if order_id in orders and orders[order_id]['status'] == 'pending':
                orders[order_id]['status'] = action
                if action == 'reject':
                    cfg_snapshot = orders[order_id].get('config_snapshot')
                    if cfg_snapshot:
                        async with configs_lock:
                            configs[cfg_snapshot['id']] = cfg_snapshot
                success += 1
        if action == 'approve':
            user_id = orders[order_id]['user_id']
            cfg = orders[order_id].get('config_snapshot', {})
            link_md = md_escape(cfg.get('link', ''))
            oid_md = md_escape(order_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ پرداخت شما تأیید شد!\n🎉 کانفیگ شما:\n`{link_md}`\nID سفارش: `{oid_md}`",
                parse_mode='MarkdownV2',
            )
        else:
            oid_md = md_escape(order_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ پرداخت شما رد شد!\n⚠️ لطفاً به پشتیبانی مراجعه کنید: @manava_vpn\nID سفارش: `{oid_md}`",
                parse_mode='MarkdownV2',
            )
    await DataManager.save_orders()
    if action == 'reject':
        await DataManager.save_configs()
    await update.message.reply_text(f"✅ {success} سفارش با موفقیت {action} شدند.")
    return ConversationHandler.END

async def handle_ping(request):
    return web.Response(text="OK")

async def error_handler(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}", exc_info=True)
    try:
        if update and getattr(update, "message", None):
            await update.message.reply_text("خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        elif update and getattr(update, "callback_query", None):
            await update.callback_query.answer("خطایی رخ داد.")
    except Exception:
        pass

async def main():
    global ADMINS, ADMIN_GROUP_ID
    try:
        await DataManager.check_env()
        ADMIN_GROUP_ID = int(ADMIN_GROUP_ID_STR)
        ADMINS = [int(x.strip()) for x in (ADMINS_STR.split(',') if ADMINS_STR else []) if x.strip().isdigit()]
        if not ADMINS:
            logger.error("No valid admin IDs provided in ADMINS env variable")
            raise ValueError("ADMINS is empty or invalid")
    except (ValueError, AttributeError) as e:
        logger.error(f"Env error: {e}")
        return

    await DataManager.load_users_cache()
    await DataManager.load_orders()
    await DataManager.load_blacklist()
    await DataManager.load_configs()

    application = Application.builder().token(TOKEN).persistence(PicklePersistence(filepath=PERSISTENCE_FILE)).build()

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
        per_message=False,
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
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("restore", restore_help_command))
    if ADMINS:
        application.add_handler(MessageHandler(filters.Document.ALL & filters.User(user_id=ADMINS), restore_file_handler))
    application.add_error_handler(error_handler)

    try:
        await application.initialize()
        await application.start()
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("ربات در حالت Polling شروع به کار کرد.")
        await application.run_polling(
            drop_pending_updates=True,
            close_loop=False,
            stop_signals=()
        )
    except Exception as e:
        logger.error(f"Error running application: {e}", exc_info=True)
    finally:
        try:
            if application.updater.running:
                await application.updater.stop()
            await application.stop()
        except Exception as e:
            logger.error(f"Error stopping application: {e}", exc_info=True)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except Exception as e:
        logger.error(f"Error in main loop: {e}", exc_info=True)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception as e:
            logger.error(f"Error shutting down asyncgens: {e}", exc_info=True)
        finally:
            loop.close()
