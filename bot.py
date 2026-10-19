
import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread

# --- RENDER KEEP-ALIVE SERVER ---
app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is running and healthy!"


def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
UPI_ID = os.getenv("UPI_ID")
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME")

if not BOT_TOKEN or not MONGO_URI or not ADMIN_ID or not UPI_ID or not CONTACT_USERNAME:
    raise ValueError(
        "Missing environment variables. Check BOT_TOKEN, MONGO_URI, "
        "ADMIN_ID, UPI_ID and CONTACT_USERNAME in Render."
    )

bot = telebot.TeleBot(BOT_TOKEN)

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client["sub_management"]
channels_col = db["channels"]
users_col = db["users"]


# --- ADMIN PANEL ---

@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = message.from_user.id
    parts = message.text.split()

    # User entered through a channel deep link
    if len(parts) > 1:
        try:
            ch_id = int(parts[1])
            ch_data = channels_col.find_one({"channel_id": ch_id})

            if ch_data:
                markup = InlineKeyboardMarkup()

                for p_time, p_price in ch_data["plans"].items():
                    mins = int(p_time)

                    if mins < 60:
                        label = f"{mins} Minutes"
                    elif mins < 1440:
                        label = f"{mins // 60} Hours"
                    else:
                        label = f"{mins // 1440} Days"

                    markup.add(
                        InlineKeyboardButton(
                            f"💳 {label} - ₹{p_price}",
                            callback_data=f"select_{ch_id}_{p_time}"
                        )
                    )

                markup.add(
                    InlineKeyboardButton(
                        "📞 Contact Admin",
                        url=f"https://t.me/{CONTACT_USERNAME.lstrip('@')}"
                    )
                )

                bot.send_message(
                    message.chat.id,
                    f"Welcome!\n\nYou are joining: *{ch_data['name']}*."
                    "\n\nPlease select a subscription plan below:",
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
                return

        except Exception as e:
            print(f"Start/deep-link error: {e}")

    # Admin greeting
    if user_id == ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "✅ Admin Panel Active!\n\n"
            "/add - Add/Edit Channel & Prices\n"
            "/channels - Manage Existing Channels"
        )
    else:
        bot.send_message(
            message.chat.id,
            "Welcome! To join a channel, please use the link provided by the Admin."
        )


@bot.message_handler(
    commands=["channels"],
    func=lambda m: m.from_user.id == ADMIN_ID
)
def list_channels(message):
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0

    for ch in cursor:
        markup.add(
            InlineKeyboardButton(
                f"Channel: {ch['name']}",
                callback_data=f"manage_{ch['channel_id']}"
            )
        )
        count += 1

    markup.add(
        InlineKeyboardButton(
            "➕ Add New Channel",
            callback_data="add_new"
        )
    )

    if count == 0:
        bot.send_message(
            ADMIN_ID,
            "No channels found. Click below to add one.",
            reply_markup=markup
        )
    else:
        bot.send_message(
            ADMIN_ID,
            "Your Managed Channels:",
            reply_markup=markup
        )


@bot.message_handler(
    commands=["add"],
    func=lambda m: m.from_user.id == ADMIN_ID
)
def add_channel_start(message):
    msg = bot.send_message(
        ADMIN_ID,
        "Please ensure the bot is an Admin in your channel, "
        "then FORWARD any message from that channel here."
    )
    bot.register_next_step_handler(msg, get_plans)


@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    bot.answer_callback_query(call.id)

    if call.from_user.id != ADMIN_ID:
        return

    msg = bot.send_message(
        ADMIN_ID,
        "Please FORWARD any message from your channel here."
    )
    bot.register_next_step_handler(msg, get_plans)


def get_plans(message):
    if not message.forward_from_chat:
        bot.send_message(
            ADMIN_ID,
            "❌ Error: Message was not forwarded. Use /add to try again."
        )
        return

    ch_id = message.forward_from_chat.id
    ch_name = message.forward_from_chat.title

    msg = bot.send_message(
        ADMIN_ID,
        f"Channel Detected: *{ch_name}*\n\n"
        "Enter plans in format (Minutes:Price):\n"
        "`Minutes:Price, Minutes:Price`\n\n"
        "Example:\n`1440:99, 43200:199` (1 Day and 30 Days)",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, finalize_channel, ch_id, ch_name)


def finalize_channel(message, ch_id, ch_name):
    try:
        raw_plans = message.text.split(",")
        plans_dict = {}

        for plan in raw_plans:
            minutes_text, price_text = plan.strip().split(":")
            minutes = int(minutes_text.strip())
            price = float(price_text.strip())

            if minutes <= 0 or price <= 0:
                raise ValueError("Minutes and price must be positive.")

            plans_dict[str(minutes)] = (
                int(price) if price.is_integer() else price
            )

        channels_col.update_one(
            {"channel_id": ch_id},
            {
                "$set": {
                    "name": ch_name,
                    "plans": plans_dict,
                    "admin_id": ADMIN_ID
                }
            },
            upsert=True
        )

        bot_username = bot.get_me().username

        bot.send_message(
            ADMIN_ID,
            "✅ Setup Successful!\n\n"
            f"Invite Link for users:\n"
            f"`https://t.me/{bot_username}?start={ch_id}`",
            parse_mode="Markdown"
        )

    except Exception as e:
        print(f"Plan setup error: {e}")
        bot.send_message(
            ADMIN_ID,
            "❌ Invalid format. Use `Minutes:Price, Minutes:Price`.\n"
            "Example: `1440:99, 43200:199`\n\n"
            "Use /add to try again.",
            parse_mode="Markdown"
        )


# --- USER: PAYMENT FLOW ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_"))
def user_pays(call):
    bot.answer_callback_query(call.id)

    try:
        _, ch_id, mins = call.data.split("_")
        ch_data = channels_col.find_one({"channel_id": int(ch_id)})

        if not ch_data or mins not in ch_data["plans"]:
            bot.send_message(
                call.message.chat.id,
                "❌ This channel or plan could not be found. Contact the admin."
            )
            return

        price = ch_data["plans"][mins]

        qr_url = (
            "https://api.qrserver.com/v1/create-qr-code/"
            f"?size=300x300&data=upi://pay?pa={UPI_ID}"
            f"%26am={price}%26cu=INR"
        )

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "✅ I Have Paid",
                callback_data=f"paid_{ch_id}_{mins}"
            )
        )
        markup.add(
            InlineKeyboardButton(
                "📞 Contact Admin",
                url=f"https://t.me/{CONTACT_USERNAME.lstrip('@')}"
            )
        )

        bot.send_photo(
            call.message.chat.id,
            qr_url,
            caption=(
                f"Plan: {mins} Minutes\n"
                f"Price: ₹{price}\n"
                f"UPI ID: `{UPI_ID}`\n\n"
                "Please complete the payment and click 'I Have Paid'."
            ),
            reply_markup=markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        print(f"Payment selection error: {e}")
        bot.send_message(
            call.message.chat.id,
            "❌ Something went wrong. Please try again or contact the admin."
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def admin_notify(call):
    bot.answer_callback_query(call.id)

    try:
        _, ch_id, mins = call.data.split("_")
        user = call.from_user
        ch_data = channels_col.find_one({"channel_id": int(ch_id)})

        if not ch_data or mins not in ch_data["plans"]:
            bot.send_message(
                call.message.chat.id,
                "❌ Channel or plan not found. Please contact the admin."
            )
            return

        price = ch_data["plans"][mins]

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"app_{user.id}_{ch_id}_{mins}"
            )
        )
        markup.add(
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"rej_{user.id}"
            )
        )

        bot.send_message(
            ADMIN_ID,
            f"🔔 *Payment Verification Required!*\n\n"
            f"User: {user.first_name}\n"
            f"User ID: `{user.id}`\n"
            f"Channel: {ch_data['name']}\n"
            f"Plan: {mins} Minutes\n"
            f"Price: ₹{price}",
            reply_markup=markup,
            parse_mode="Markdown"
        )

        user_markup = InlineKeyboardMarkup()
        user_markup.add(
            InlineKeyboardButton(
                "📞 Contact Admin",
                url=f"https://t.me/{CONTACT_USERNAME.lstrip('@')}"
            )
        )

        bot.send_message(
            call.message.chat.id,
            "✅ Your payment request has been sent. "
            "Please wait for Admin approval.",
            reply_markup=user_markup
        )

    except Exception as e:
        print(f"Admin notification error: {e}")
        bot.send_message(
            call.message.chat.id,
            "❌ Could not send your request. Please contact the admin."
        )


# --- APPROVAL ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("app_"))
def approve_now(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "Only the admin can approve payments.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        _, u_id, ch_id, mins = call.data.split("_")
        u_id = int(u_id)
        ch_id = int(ch_id)
        mins = int(mins)

        expiry_datetime = datetime.now() + timedelta(minutes=mins)
        expiry_ts = int(expiry_datetime.timestamp())

        # Bot must be an admin with invite-link permissions.
        link = bot.create_chat_invite_link(
            ch_id,
            member_limit=1,
            expire_date=expiry_ts
        )

        users_col.update_one(
            {"user_id": u_id, "channel_id": ch_id},
            {
                "$set": {
                    "user_id": u_id,
                    "channel_id": ch_id,
                    "expiry": expiry_ts
                }
            },
            upsert=True
        )

        bot.send_message(
            u_id,
            f"🥳 *Payment Approved!*\n\n"
            f"Subscription: {mins} Minutes\n\n"
            f"Join Link: {link.invite_link}\n\n"
            f"⚠️ Note: The invite link expires in {mins} minutes.",
            parse_mode="Markdown"
        )

        bot.edit_message_text(
            f"✅ Approved user {u_id} for {mins} minutes.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )

    except Exception as e:
        print(f"Approval error: {e}")
        bot.send_message(ADMIN_ID, f"❌ Approval error: {e}")


# --- REJECTION: CORRECTED HANDLER ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("rej_"))
def reject_now(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "Only the admin can reject payments.",
            show_alert=True
        )
        return

    # Answer immediately so Telegram stops showing the loading spinner.
    bot.answer_callback_query(call.id, "Processing rejection...")

    try:
        user_id = int(call.data.split("_")[1])

        # Update the admin message first.
        bot.edit_message_text(
            f"❌ Payment request rejected for user {user_id}.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )

        # Notify the user. This may fail if the user blocked the bot.
        try:
            bot.send_message(
                user_id,
                "❌ Your payment request has been rejected by the admin.\n\n"
                "If you believe this is a mistake, please contact the admin."
            )
        except Exception as notify_error:
            print(
                f"Could not notify rejected user {user_id}: "
                f"{notify_error}"
            )

    except Exception as e:
        print(f"Reject error: {e}")
        bot.send_message(
            ADMIN_ID,
            f"❌ Could not process rejection: {e}"
        )


# --- CHANNEL MANAGEMENT ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("manage_"))
def manage_ch(call):
    bot.answer_callback_query(call.id)

    if call.from_user.id != ADMIN_ID:
        return

    try:
        ch_id = int(call.data.split("_")[1])
        ch_data = channels_col.find_one({"channel_id": ch_id})

        if not ch_data:
            bot.send_message(ADMIN_ID, "❌ Channel not found.")
            return

        bot_username = bot.get_me().username
        link = f"https://t.me/{bot_username}?start={ch_id}"

        bot.edit_message_text(
            f"Settings for: *{ch_data['name']}*\n\n"
            f"Your Link: `{link}`\n\n"
            "To edit prices, use /add and forward a message "
            "from this channel again.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:
        print(f"Channel management error: {e}")
        bot.send_message(ADMIN_ID, f"❌ Error: {e}")


# --- AUTOMATIC EXPIRY ---

def kick_expired_users():
    now = datetime.now().timestamp()
    expired_users = users_col.find({"expiry": {"$lte": now}})

    try:
        bot_username = bot.get_me().username
    except Exception as e:
        print(f"Could not get bot username: {e}")
        return

    for user in expired_users:
        try:
            channel_id = user["channel_id"]
            user_id = user["user_id"]

            # Bot must be an admin with permission to ban members.
            bot.ban_chat_member(channel_id, user_id)
            bot.unban_chat_member(channel_id, user_id)

            rejoin_url = f"https://t.me/{bot_username}?start={channel_id}"
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(
                    "🔄 Re-join / Renew",
                    url=rejoin_url
                )
            )

            try:
                bot.send_message(
                    user_id,
                    "⚠️ Your subscription has expired.\n\n"
                    "To join again or renew, click below:",
                    reply_markup=markup
                )
            except Exception as notify_error:
                print(
                    f"Could not notify expired user {user_id}: "
                    f"{notify_error}"
                )

            users_col.delete_one({"_id": user["_id"]})

        except Exception as e:
            print(
                f"Expiry processing error for user "
                f"{user.get('user_id')}: {e}"
            )


# --- STARTUP ---

if __name__ == "__main__":
    keep_alive()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        kick_expired_users,
        "interval",
        minutes=1,
        max_instances=1
    )
    scheduler.start()

    bot.remove_webhook()
    print("Bot is running...")

    try:
        bot.infinity_polling(
            timeout=20,
            long_polling_timeout=10
        )
    finally:
        scheduler.shutdown(wait=False)
