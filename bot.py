import asyncio, os, random, logging, re
from decimal import Decimal
import psycopg2, psycopg2.extras
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID  = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
DATABASE_URL   = os.environ["DATABASE_URL"]

AD_POINTS      = 40
GAME_POINTS    = 30
POINTS_TO_TAKA = Decimal("0.05")

REQUIRED_CHANNELS = [
    {"username": "@ITACHI_32_PAIN",  "link": "https://t.me/ITACHI_32_PAIN",  "name": "ITACHI PAIN"},
    {"username": "@obito_pain_paiv", "link": "https://t.me/obito_pain_paiv", "name": "OBITO PAIN"},
    {"username": "@NARO_PAIN",       "link": "https://t.me/NARO_PAIN",       "name": "NARO PAIN"},
]

# In-memory session cache (restored from DB on first access)
phone_map:             dict[int, str]  = {}
pending_registrations: dict[int, dict] = {}
pending_withdrawals:   dict[int, dict] = {}


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def get_user(phone: str):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
        return cur.fetchone()

def create_user(username: str, phone: str):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, phone, points) VALUES (%s,%s,0) RETURNING *",
            (username, phone),
        )
        conn.commit()
        return cur.fetchone()

def update_points(user_id: int, new_points: int):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET points = %s WHERE id = %s", (new_points, user_id))
        conn.commit()

def log_game(user_id: int, game_id: int, won: bool, points_earned: int):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO game_sessions (user_id,game_id,won,points_earned) VALUES (%s,%s,%s,%s)",
            (user_id, game_id, won, points_earned),
        )
        conn.commit()

def log_ad(user_id: int):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ad_watches (user_id,points_earned) VALUES (%s,%s)",
            (user_id, AD_POINTS),
        )
        conn.commit()

def create_withdrawal(user_id: int, points: int, amount: Decimal, method: str, account_number: str):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO withdrawals (user_id,points,amount,method,account_number,status)
               VALUES (%s,%s,%s,%s,%s,'pending') RETURNING *""",
            (user_id, points, str(amount), method, account_number),
        )
        conn.commit()
        return cur.fetchone()

def get_leaderboard(limit: int = 10):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT username, phone, points FROM users ORDER BY points DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()

def get_user_game_stats(user_id: int):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS played, SUM(CASE WHEN won THEN 1 ELSE 0 END) AS won "
            "FROM game_sessions WHERE user_id = %s",
            (user_id,),
        )
        return cur.fetchone()

def get_user_ad_count(user_id: int) -> int:
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM ad_watches WHERE user_id = %s", (user_id,))
        return int(cur.fetchone()["cnt"])

def get_user_withdrawn(user_id: int) -> int:
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(points),0) AS total FROM withdrawals "
            "WHERE user_id = %s AND status = 'approved'",
            (user_id,),
        )
        return int(cur.fetchone()["total"])

# ── Session persistence ───────────────────────────────────────────────────────

def save_session(chat_id: int, phone: str):
    """Save login session to DB so it survives bot restarts."""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sessions (chat_id, phone, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (chat_id) DO UPDATE SET phone = %s, updated_at = NOW()""",
            (chat_id, phone, phone),
        )
        conn.commit()

def load_session(chat_id: int):
    """Load saved session from DB."""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT phone FROM sessions WHERE chat_id = %s", (chat_id,))
        row = cur.fetchone()
        return row["phone"] if row else None

def resolve_phone(chat_id: int):
    """
    Return the logged-in phone for this chat.
    Checks in-memory cache first; falls back to DB (and repopulates cache).
    Returns None if not logged in.
    """
    phone = phone_map.get(chat_id)
    if not phone:
        phone = load_session(chat_id)
        if phone:
            phone_map[chat_id] = phone
    return phone


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def check_channel_membership(bot, user_id: int) -> bool:
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                logger.info("User %s not in %s: status=%s", user_id, ch["username"], member.status)
                return False
        except Exception as exc:
            msg = str(exc)
            logger.warning("getChatMember error for %s: %s", ch["username"], msg)
            if any(kw in msg for kw in ("bot is not a member", "chat not found",
                                        "Forbidden", "CHANNEL_PRIVATE", "not enough rights")):
                logger.warning("⚠️  Bot has no access to %s — skipping check", ch["username"])
                continue
            return False
    return True

def channel_join_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📢 {ch['name']} চ্যানেলে জয়েন করুন", url=ch["link"])]
        for ch in REQUIRED_CHANNELS
    ]
    rows.append([InlineKeyboardButton("✅ জয়েন করেছি — চেক করুন", callback_data="check_joined")])
    return InlineKeyboardMarkup(rows)

def main_menu(points: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 গেম খেলুন (+৩০ পয়েন্ট)", callback_data="play_game")],
        [InlineKeyboardButton("📺 বিজ্ঞাপন দেখুন (+৪০ পয়েন্ট)", callback_data="watch_ad")],
        [InlineKeyboardButton("💸 উইথড্র করুন", callback_data="withdraw")],
        [
            InlineKeyboardButton("🏆 লিডারবোর্ড", callback_data="leaderboard"),
            InlineKeyboardButton("📊 স্ট্যাটস", callback_data="stats"),
        ],
    ])

def game_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪙 কয়েন ফ্লিপ", callback_data="game_1"),
            InlineKeyboardButton("🔢 নম্বর অনুমান", callback_data="game_2"),
        ],
        [InlineKeyboardButton("🎨 রঙ বেছে নিন", callback_data="game_3")],
        [InlineKeyboardButton("« পেছনে", callback_data="back_main")],
    ])

def back_and_retry(retry_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 আবার", callback_data=retry_cb),
            InlineKeyboardButton("🏠 মেনু", callback_data="back_main"),
        ]
    ])

async def send_admin_notification(bot, text: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Admin notify failed: %s", exc)


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user       = update.effective_user
    first_name = user.first_name if user else "বন্ধু"
    chat_id    = update.effective_chat.id
    joined     = await check_channel_membership(ctx.bot, user.id)

    if not joined:
        await update.message.reply_html(
            f"🎉 <b>পয়েন্ট আর্নিং বটে স্বাগতম, {first_name}!</b>\n\n"
            "বট ব্যবহার করতে হলে আগে নিচের <b>৩টি চ্যানেলে জয়েন</b> করুন:",
            reply_markup=channel_join_markup(),
        )
        return

    # Check saved session (survives restarts)
    existing_phone = resolve_phone(chat_id)
    if existing_phone:
        db_user = get_user(existing_phone)
        if db_user:
            await update.message.reply_html(
                f"👋 <b>স্বাগতম ফিরে, {db_user['username']}!</b>\n"
                f"💰 পয়েন্ট: <b>{db_user['points']}</b>\n\nকি করতে চান?",
                reply_markup=main_menu(db_user["points"]),
            )
            return

    await update.message.reply_html(
        f"🎉 <b>স্বাগতম {first_name}!</b>\n\n"
        "আপনার <b>মোবাইল নম্বর</b> পাঠান:\n(যেমন: 01712345678)\n\n"
        "📌 নতুন ব্যবহারকারীরাও সরাসরি এখানে রেজিস্ট্রেশন করতে পারবেন।"
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    joined  = await check_channel_membership(ctx.bot, update.effective_user.id)
    if not joined:
        await update.message.reply_text("⛔ আগে সব চ্যানেলে জয়েন করুন।", reply_markup=channel_join_markup())
        return
    phone = resolve_phone(chat_id)
    if not phone:
        await update.message.reply_text("প্রথমে মোবাইল নম্বর পাঠান।")
        return
    db_user = get_user(phone)
    if not db_user:
        await update.message.reply_text("ইউজার পাওয়া যায়নি। /start দিন।")
        return
    taka = float(db_user["points"]) * float(POINTS_TO_TAKA)
    await update.message.reply_html(
        f"👋 <b>{db_user['username']}</b>\n"
        f"💰 পয়েন্ট: <b>{db_user['points']}</b>  ({taka:.2f} টাকা)\n\nকি করতে চান?",
        reply_markup=main_menu(db_user["points"]),
    )

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    phone   = resolve_phone(chat_id)
    if not phone:
        await update.message.reply_text("প্রথমে মোবাইল নম্বর পাঠান.")
        return
    db_user = get_user(phone)
    if not db_user:
        await update.message.reply_text("ইউজার পাওয়া যায়নি।")
        return
    taka = float(db_user["points"]) * float(POINTS_TO_TAKA)
    await update.message.reply_html(
        f"💰 <b>{db_user['username']}</b>\n"
        f"পয়েন্ট: <b>{db_user['points']}</b> = {taka:.2f} টাকা"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📱 <b>কমান্ড তালিকা:</b>\n\n"
        "/start   — শুরু করুন\n"
        "/menu    — মূল মেনু\n"
        "/balance — পয়েন্ট দেখুন\n"
        "/help    — সাহায্য"
    )


# ── Text messages ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    pending_wd = pending_withdrawals.get(chat_id)

    # ── 1a. Withdrawal — point amount input ───────────────────────────────────
    if pending_wd and pending_wd.get("stage") == "amount":
        phone   = resolve_phone(chat_id)
        db_user = get_user(phone) if phone else None
        if not db_user:
            return
        if not text.isdigit():
            await update.message.reply_text("❌ সঠিক সংখ্যা দিন (যেমন: 100)")
            return
        pts = int(text)
        if pts <= 0:
            await update.message.reply_text("❌ সঠিক সংখ্যা দিন (যেমন: 100)")
            return
        if pts % 100 != 0:
            await update.message.reply_text("❌ পয়েন্ট ১০০ এর গুণিতক হতে হবে।\n(যেমন: 100, 200, 300…)")
            return
        if pts < 100:
            await update.message.reply_text("❌ কমপক্ষে ১০০ পয়েন্ট উইথড্র করতে হবে।")
            return
        if pts > db_user["points"]:
            await update.message.reply_html(
                f"❌ পর্যাপ্ত পয়েন্ট নেই।\nআপনার আছে: <b>{db_user['points']}</b> পয়েন্ট"
            )
            return
        taka = int(pts * float(POINTS_TO_TAKA))
        pending_withdrawals[chat_id] = {"stage": "method", "points": pts}
        await update.message.reply_html(
            f"💸 <b>{pts} পয়েন্ট = {taka} টাকা</b>\n\n📱 কোন মাধ্যমে পাঠাবো?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("বিকাশ", callback_data="method_bkash"),
                    InlineKeyboardButton("নগদ",   callback_data="method_nagad"),
                ],
                [InlineKeyboardButton("« বাতিল", callback_data="cancel_wd")],
            ]),
        )
        return

    # ── 1b. Withdrawal — account number input ─────────────────────────────────
    if pending_wd and pending_wd.get("stage") == "account":
        if not re.fullmatch(r"01[3-9]\d{8}", text):
            await update.message.reply_text("সঠিক ১১ ডিজিটের নম্বর দিন (যেমন: 01712345678)")
            return
        phone   = resolve_phone(chat_id)
        db_user = get_user(phone) if phone else None
        if not db_user:
            return
        points = pending_wd["points"]
        method = pending_wd["method"]
        if db_user["points"] < points:
            await update.message.reply_text(f"❌ পর্যাপ্ত পয়েন্ট নেই। আপনার: {db_user['points']}")
            pending_withdrawals.pop(chat_id, None)
            return
        amount  = Decimal(str(points)) * POINTS_TO_TAKA
        new_pts = db_user["points"] - points
        update_points(db_user["id"], new_pts)
        create_withdrawal(db_user["id"], points, amount, method, text)
        pending_withdrawals.pop(chat_id, None)
        method_name = "বিকাশ" if method == "bkash" else "নগদ"
        await update.message.reply_html(
            f"✅ <b>উইথড্র রিকোয়েস্ট পাঠানো হয়েছে!</b>\n\n"
            f"💸 পয়েন্ট: <b>{points}</b>\n"
            f"💵 পরিমাণ: <b>{amount:.2f} টাকা</b>\n"
            f"📱 {method_name}: <b>{text}</b>\n\n"
            "⏳ অনুমোদনের জন্য অপেক্ষা করুন।",
            reply_markup=main_menu(new_pts),
        )
        await send_admin_notification(
            ctx.bot,
            f"🔔 <b>নতুন উইথড্র!</b>\n"
            f"👤 {db_user['username']}\n📞 {db_user['phone']}\n"
            f"💸 {points} পয়েন্ট → {amount:.2f} টাকা\n"
            f"📱 {method_name}: {text}",
        )
        return

    # ── 2. Phone number ───────────────────────────────────────────────────────
    if re.fullmatch(r"01[3-9]\d{8}", text):
        joined = await check_channel_membership(ctx.bot, user_id)
        if not joined:
            await update.message.reply_text(
                "⛔ আগে সব চ্যানেলে জয়েন করুন।",
                reply_markup=channel_join_markup(),
            )
            return
        db_user = get_user(text)
        if db_user:
            # Existing user → login + save session
            phone_map[chat_id] = text
            save_session(chat_id, text)
            pending_registrations.pop(chat_id, None)
            await update.message.reply_html(
                f"✅ <b>লগিন সফল!</b>\n"
                f"👤 {db_user['username']}\n"
                f"💰 পয়েন্ট: <b>{db_user['points']}</b>",
                reply_markup=main_menu(db_user["points"]),
            )
        else:
            # New user → ask for name
            pending_registrations[chat_id] = {"phone": text}
            await update.message.reply_html(
                f"📝 <b>নতুন অ্যাকাউন্ট তৈরি হবে!</b>\n\n"
                f"📱 নম্বর: <b>{text}</b>\n\n"
                "👤 আপনার <b>নাম</b> লিখুন:\n(যেমন: Rahim Ahmed)",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ বাতিল", callback_data="cancel_reg")]
                ]),
            )
        return

    # ── 3. Registration name input ────────────────────────────────────────────
    reg_data = pending_registrations.get(chat_id)
    if reg_data:
        name = text
        if len(name) < 2:
            await update.message.reply_text("❌ নাম কমপক্ষে ২ অক্ষরের হতে হবে।")
            return
        if len(name) > 50:
            await update.message.reply_text("❌ নাম সর্বোচ্চ ৫০ অক্ষরের হতে হবে।")
            return
        try:
            new_user = create_user(name, reg_data["phone"])
            phone_map[chat_id] = reg_data["phone"]
            save_session(chat_id, reg_data["phone"])
            pending_registrations.pop(chat_id, None)
            await update.message.reply_html(
                f"🎉 <b>রেজিস্ট্রেশন সফল!</b>\n\n"
                f"👤 নাম: <b>{new_user['username']}</b>\n"
                f"📱 নম্বর: <b>{new_user['phone']}</b>\n"
                f"💰 পয়েন্ট: <b>0</b>\n\n"
                "স্বাগতম! গেম খেলুন ও পয়েন্ট আর্ন করুন।",
                reply_markup=main_menu(0),
            )
            await send_admin_notification(
                ctx.bot,
                f"🆕 <b>নতুন রেজিস্ট্রেশন!</b>\n"
                f"👤 {new_user['username']}\n📞 {new_user['phone']}",
            )
        except Exception as exc:
            msg = str(exc)
            if "unique" in msg or "duplicate" in msg:
                await update.message.reply_text(
                    "❌ এই নম্বরে ইতিমধ্যে অ্যাকাউন্ট আছে।\nআপনার নম্বর আবার পাঠান।"
                )
                pending_registrations.pop(chat_id, None)
            else:
                logger.error("create_user error: %s", exc)
                await update.message.reply_text("❌ রেজিস্ট্রেশন করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।")
        return

    # ── 4. Fallback ───────────────────────────────────────────────────────────
    if not resolve_phone(chat_id):
        await update.message.reply_text("প্রথমে আপনার মোবাইল নম্বর পাঠান:\n(যেমন: 01712345678)")


# ── Callback queries ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    data    = query.data
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    phone   = resolve_phone(chat_id)

    async def edit(text: str, markup=None):
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

    # ── Registration cancel ───────────────────────────────────────────────────
    if data == "cancel_reg":
        pending_registrations.pop(chat_id, None)
        await query.answer()
        await edit("❌ রেজিস্ট্রেশন বাতিল হয়েছে।\n\nআবার শুরু করতে /start দিন।")
        return

    # ── check_joined: answer BEFORE calling query.answer() ───────────────────
    if data == "check_joined":
        joined = await check_channel_membership(ctx.bot, user_id)
        if not joined:
            await query.answer("⛔ এখনো সব চ্যানেলে জয়েন করেননি!", show_alert=True)
            return
        await query.answer()
        await edit(
            "✅ <b>জয়েন সফল!</b>\n\n"
            "আপনার মোবাইল নম্বর পাঠান:\n(যেমন: 01712345678)\n\n"
            "📌 নতুন ব্যবহারকারীরাও এখানে রেজিস্ট্রেশন করতে পারবেন।"
        )
        return

    await query.answer()

    # ── Back to main menu ─────────────────────────────────────────────────────
    if data == "back_main":
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        pending_withdrawals.pop(chat_id, None)
        taka = float(db_user["points"]) * float(POINTS_TO_TAKA)
        await edit(
            f"💰 পয়েন্ট: <b>{db_user['points']}</b>  ({taka:.2f} টাকা)\n\nকি করতে চান?",
            markup=main_menu(db_user["points"]),
        )
        return

    # ── Game menu ─────────────────────────────────────────────────────────────
    if data == "play_game":
        await edit("🎮 <b>কোন গেম খেলবেন?</b>\n\nজিতলে +<b>৩০ পয়েন্ট</b>!", markup=game_menu())
        return

    if data == "game_1":
        await edit(
            "🪙 <b>কয়েন ফ্লিপ!</b> হেড নাকি টেইল?",
            markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("👆 হেড", callback_data="flip_heads"),
                    InlineKeyboardButton("👇 টেইল", callback_data="flip_tails"),
                ],
                [InlineKeyboardButton("« পেছনে", callback_data="play_game")],
            ]),
        )
        return

    if data == "game_2":
        await edit(
            "🔢 <b>নম্বর অনুমান!</b> ১–৫ এর মধ্যে বেছে নিন:",
            markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(str(n), callback_data=f"guess_{n}") for n in range(1, 6)],
                [InlineKeyboardButton("« পেছনে", callback_data="play_game")],
            ]),
        )
        return

    if data == "game_3":
        await edit(
            "🎨 <b>রঙ বেছে নিন!</b>",
            markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔴 লাল",   callback_data="color_red"),
                    InlineKeyboardButton("🔵 নীল",   callback_data="color_blue"),
                    InlineKeyboardButton("🟢 সবুজ", callback_data="color_green"),
                ],
                [InlineKeyboardButton("« পেছনে", callback_data="play_game")],
            ]),
        )
        return

    # ── Coin flip ─────────────────────────────────────────────────────────────
    if data in ("flip_heads", "flip_tails"):
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        result = random.choice(["heads", "tails"])
        won    = data == f"flip_{result}"
        pts    = GAME_POINTS if won else 0
        log_game(db_user["id"], 1, won, pts)
        new_pts = db_user["points"] + pts
        update_points(db_user["id"], new_pts)
        pick = "👆 হেড" if data == "flip_heads" else "👇 টেইল"
        res  = "👆 হেড" if result == "heads" else "👇 টেইল"
        msg  = (
            f"🎉 <b>জিতেছেন!</b>\nআপনি: {pick} | ফলাফল: {res}\n"
            f"+<b>{pts}</b> পয়েন্ট\n💰 মোট: <b>{new_pts}</b>"
            if won else
            f"😔 <b>হেরেছেন!</b>\nআপনি: {pick} | ফলাফল: {res}\n"
            f"💰 পয়েন্ট: <b>{new_pts}</b>"
        )
        await edit(msg, markup=back_and_retry("game_1"))
        return

    # ── Number guess ──────────────────────────────────────────────────────────
    if data.startswith("guess_"):
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        guess   = int(data.split("_")[1])
        correct = random.randint(1, 5)
        won     = guess == correct
        pts     = GAME_POINTS if won else 0
        log_game(db_user["id"], 2, won, pts)
        new_pts = db_user["points"] + pts
        update_points(db_user["id"], new_pts)
        msg = (
            f"🎉 <b>সঠিক!</b> আপনি: {guess} | সঠিক: {correct}\n"
            f"+<b>{pts}</b> পয়েন্ট\n💰 মোট: <b>{new_pts}</b>"
            if won else
            f"😔 <b>ভুল!</b> আপনি: {guess} | সঠিক ছিল: {correct}\n"
            f"💰 পয়েন্ট: <b>{new_pts}</b>"
        )
        await edit(msg, markup=back_and_retry("game_2"))
        return

    # ── Colour pick ───────────────────────────────────────────────────────────
    if data.startswith("color_"):
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        colors     = {"red": "🔴 লাল", "blue": "🔵 নীল", "green": "🟢 সবুজ"}
        user_color = data.split("_")[1]
        correct    = random.choice(list(colors.keys()))
        won        = user_color == correct
        pts        = GAME_POINTS if won else 0
        log_game(db_user["id"], 3, won, pts)
        new_pts = db_user["points"] + pts
        update_points(db_user["id"], new_pts)
        msg = (
            f"🎉 <b>সঠিক রঙ!</b> আপনি: {colors[user_color]} | সঠিক: {colors[correct]}\n"
            f"+<b>{pts}</b> পয়েন্ট\n💰 মোট: <b>{new_pts}</b>"
            if won else
            f"😔 <b>ভুল রঙ!</b> আপনি: {colors[user_color]} | সঠিক ছিল: {colors[correct]}\n"
            f"💰 পয়েন্ট: <b>{new_pts}</b>"
        )
        await edit(msg, markup=back_and_retry("game_3"))
        return

    # ── Ad watch ──────────────────────────────────────────────────────────────
    if data == "watch_ad":
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        await edit("📺 <b>বিজ্ঞাপন লোড হচ্ছে...</b>\n\n⏳ ৫ সেকেন্ড অপেক্ষা করুন...")
        await asyncio.sleep(5)
        log_ad(db_user["id"])
        new_pts = db_user["points"] + AD_POINTS
        update_points(db_user["id"], new_pts)
        await edit(
            f"✅ <b>বিজ্ঞাপন দেখা সম্পন্ন!</b>\n\n"
            f"+<b>{AD_POINTS}</b> পয়েন্ট!\n💰 মোট: <b>{new_pts}</b>",
            markup=main_menu(new_pts),
        )
        return

    # ── Withdraw ──────────────────────────────────────────────────────────────
    if data == "withdraw":
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        if db_user["points"] < 100:
            await edit(
                f"❌ কমপক্ষে ১০০ পয়েন্ট দরকার।\nআপনার: <b>{db_user['points']}</b>",
                markup=main_menu(db_user["points"]),
            )
            return
        pending_withdrawals[chat_id] = {"stage": "amount"}
        await edit(
            f"💸 <b>কত পয়েন্ট উইথড্র করতে চান?</b>\n\n"
            f"💰 আপনার পয়েন্ট: <b>{db_user['points']}</b>\n"
            f"💵 ১০০ পয়েন্ট = ৫ টাকা\n\n"
            f"✏️ এখন সংখ্যা টাইপ করে পাঠান\n(কমপক্ষে ১০০, ১০০ এর গুণিতক)",
            markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« বাতিল", callback_data="cancel_wd")]
            ]),
        )
        return

    # উইথড্র বাতিল
    if data == "cancel_wd":
        pending_withdrawals.pop(chat_id, None)
        db_user = get_user(phone) if phone else None
        pts = db_user["points"] if db_user else 0
        await edit("❌ উইথড্র বাতিল হয়েছে।", markup=main_menu(pts))
        return

    if data in ("method_bkash", "method_nagad"):
        pending = pending_withdrawals.get(chat_id)
        if not pending or not pending.get("points"):
            return
        method      = "bkash" if data == "method_bkash" else "nagad"
        method_name = "বিকাশ" if method == "bkash" else "নগদ"
        pending_withdrawals[chat_id] = {"stage": "account", "points": pending["points"], "method": method}
        await edit(
            f"📱 <b>{method_name} নম্বর দিন:</b>\n\n"
            "এখন নম্বরটি টাইপ করে পাঠান\n(যেমন: 01712345678)",
            markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« বাতিল", callback_data="cancel_wd")]
            ]),
        )
        return

    # ── Leaderboard ───────────────────────────────────────────────────────────
    if data == "leaderboard":
        top    = get_leaderboard(10)
        medals = ["🥇", "🥈", "🥉"]
        lines  = [
            f"{medals[i] if i < 3 else str(i + 1) + '.'} "
            f"<b>{u['username']}</b> — {u['points']} পয়েন্ট"
            for i, u in enumerate(top)
        ]
        await edit(
            "🏆 <b>শীর্ষ ১০ আর্নার</b>\n\n" + ("\n".join(lines) or "এখনো কেউ নেই"),
            markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« পেছনে", callback_data="back_main")]
            ]),
        )
        return

    # ── Stats ─────────────────────────────────────────────────────────────────
    if data == "stats":
        db_user = get_user(phone) if phone else None
        if not db_user:
            await edit("প্রথমে মোবাইল নম্বর পাঠান।")
            return
        gs   = get_user_game_stats(db_user["id"])
        ads  = get_user_ad_count(db_user["id"])
        wdwn = get_user_withdrawn(db_user["id"])
        await edit(
            f"📊 <b>{db_user['username']} এর স্ট্যাটস</b>\n\n"
            f"💰 পয়েন্ট: <b>{db_user['points']}</b>\n"
            f"🎮 গেম: <b>{gs['played']}</b> খেলেছেন, <b>{gs['won'] or 0}</b> জিতেছেন\n"
            f"📺 বিজ্ঞাপন: <b>{ads}</b> টি দেখেছেন\n"
            f"💸 উইথড্র (অনুমোদিত): <b>{wdwn}</b> পয়েন্ট",
            markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« পেছনে", callback_data="back_main")]
            ]),
        )
        return


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("menu",    cmd_menu))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("বট চালু হচ্ছে...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
