import os
import time
import requests
import telebot

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient, ReturnDocument
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread

# =========================================================
# RENDER KEEP-ALIVE SERVER
# =========================================================

app = Flask('')

@app.route('/')
def home():
    return "Bot is running and healthy!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web, daemon=True).start()


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))

CONTACT_USERNAME = os.getenv('CONTACT_USERNAME')

RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

RAZORPAY_API = "https://api.razorpay.com/v1"

if not all([
    BOT_TOKEN,
    MONGO_URI,
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET
]):
    raise ValueError("Missing required environment variables!")

bot = telebot.TeleBot(BOT_TOKEN)

client = MongoClient(MONGO_URI)
db = client['sub_management']

channels_col = db['channels']
users_col = db['users']
payments_col = db['payments']

# Prevent the same Razorpay payment from being processed twice.
payments_col.create_index(
    "razorpay_payment_id",
    unique=True,
    sparse=True
)

# Reuse HTTP connections to reduce connection overhead.
razorpay_session = requests.Session()


# =========================================================
# RAZORPAY HELPERS
# =========================================================

def razorpay_request(method, endpoint, **kwargs):
    """Make an authenticated Razorpay API request and log duration."""

    started = time.monotonic()

    try:
        response = razorpay_session.request(
            method,
            f"{RAZORPAY_API}{endpoint}",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            timeout=20,
            **kwargs
        )

        response.raise_for_status()
        result = response.json()

        elapsed = time.monotonic() - started

        print(
            f"Razorpay {method} {endpoint} "
            f"completed in {elapsed:.2f} seconds"
        )

        return result

    except Exception as e:

        elapsed = time.monotonic() - started

        print(
            f"Razorpay {method} {endpoint} "
            f"failed after {elapsed:.2f} seconds: {e}"
        )

        raise


def create_payment_qr(user_id, channel_id, mins, price):
    """Create a single-use QR for the exact subscription amount."""

    amount_paise = int(round(float(price) * 100))

    if amount_paise <= 0:
        raise ValueError("Invalid payment amount.")

    close_by = int(time.time()) + 15 * 60

    payload = {
        "type": "upi_qr",
        "name": f"Telegram Subscription {channel_id}",
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": amount_paise,
        "description": f"Subscription {mins} minutes",
        "close_by": close_by,
        "notes": {
            "telegram_user_id": str(user_id),
            "channel_id": str(channel_id),
            "plan_minutes": str(mins)
        }
    }

    return razorpay_request(
        "POST",
        "/payments/qr_codes",
        json=payload
    )


def get_qr_payments(qr_id):
    """Fetch payment records belonging to this specific QR."""

    return razorpay_request(
        "GET",
        f"/payments/qr_codes/{qr_id}/payments"
    )


# =========================================================
# SUBSCRIPTION APPROVAL
# =========================================================

def grant_subscription(user_id, channel_id, mins):
    """Create invite link and save subscription expiry."""

    expiry_datetime = datetime.now() + timedelta(minutes=mins)
    expiry_ts = int(expiry_datetime.timestamp())

    link = bot.create_chat_invite_link(
        channel_id,
        member_limit=1,
        expire_date=expiry_ts
    )

    users_col.update_one(
        {
            "user_id": user_id,
            "channel_id": channel_id
        },
        {
            "$set": {
                "expiry": expiry_datetime.timestamp()
            }
        },
        upsert=True
    )

    bot.send_message(
        user_id,
        f"🥳 Payment Approved!\n\n"
        f"Subscription: {mins} minutes\n\n"
        f"Join Link: {link.invite_link}\n\n"
        f"Your subscription expires in {mins} minutes."
    )

    return link.invite_link


# =========================================================
# START COMMAND
# =========================================================

@bot.message_handler(commands=['start'])
def start_handler(message):

    user_id = message.from_user.id
    text = message.text.split()

    # User enters through a channel deep link.
    if len(text) > 1:

        try:
            ch_id = int(text[1])

            ch_data = channels_col.find_one({
                "channel_id": ch_id
            })

            if ch_data:

                markup = InlineKeyboardMarkup()

                for p_time, p_price in ch_data['plans'].items():

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
                        url=f"https://t.me/{CONTACT_USERNAME}"
                    )
                )

                bot.send_message(
                    message.chat.id,
                    f"Welcome!\n\n"
                    f"You are joining: {ch_data['name']}.\n\n"
                    f"Please select a subscription plan:",
                    reply_markup=markup
                )

                return

        except Exception as e:
            print("Start deep-link error:", e)

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
            "Welcome! To join a channel, "
            "please use the link provided by the Admin."
        )


# =========================================================
# ADMIN: CHANNEL MANAGEMENT
# =========================================================

@bot.message_handler(
    commands=['channels'],
    func=lambda m: m.from_user.id == ADMIN_ID
)
def list_channels(message):

    markup = InlineKeyboardMarkup()

    cursor = channels_col.find({
        "admin_id": ADMIN_ID
    })

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
    commands=['add'],
    func=lambda m: m.from_user.id == ADMIN_ID
)
def add_channel_start(message):

    msg = bot.send_message(
        ADMIN_ID,
        "Ensure the bot is an Admin in your channel, "
        "then FORWARD any message from that channel here."
    )

    bot.register_next_step_handler(msg, get_plans)


@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):

    bot.answer_callback_query(call.id)

    msg = bot.send_message(
        ADMIN_ID,
        "Please FORWARD any message from your channel here."
    )

    bot.register_next_step_handler(msg, get_plans)


def get_plans(message):

    if message.forward_from_chat:

        ch_id = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title

        msg = bot.send_message(
            ADMIN_ID,
            f"Channel Detected: {ch_name}\n\n"
            f"Enter plans in format (Minutes:Price):\n"
            f"1440:99, 43200:199\n\n"
            f"Example: 1440:99, 43200:199"
        )

        bot.register_next_step_handler(
            msg,
            finalize_channel,
            ch_id,
            ch_name
        )

    else:

        bot.send_message(
            ADMIN_ID,
            "❌ Message was not forwarded. Use /add to try again."
        )


def finalize_channel(message, ch_id, ch_name):

    try:

        raw_plans = message.text.split(',')
        plans_dict = {}

        for p in raw_plans:

            t, pr = p.strip().split(':')

            if int(t) <= 0 or float(pr) <= 0:
                raise ValueError("Plan and price must be positive.")

            plans_dict[t] = pr

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
            f"✅ Setup Successful!\n\n"
            f"Invite Link for users:\n"
            f"https://t.me/{bot_username}?start={ch_id}"
        )

    except Exception as e:

        print("Channel setup error:", e)

        bot.send_message(
            ADMIN_ID,
            "❌ Invalid format. Use 1440:99, 43200:199. "
            "Use /add to retry."
        )


# =========================================================
# USER: CREATE RAZORPAY QR
# =========================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith('select_')
)
def user_pays(call):

    bot.answer_callback_query(call.id)

    try:

        _, ch_id, mins = call.data.split('_')

        ch_id = int(ch_id)
        mins = int(mins)

        ch_data = channels_col.find_one({
            "channel_id": ch_id
        })

        if not ch_data:
            bot.send_message(
                call.message.chat.id,
                "❌ Channel not found."
            )
            return

        price = ch_data['plans'].get(str(mins))

        if price is None:
            bot.send_message(
                call.message.chat.id,
                "❌ Subscription plan not found."
            )
            return

        # Create a new single-use QR for this user and plan.
        qr = create_payment_qr(
            call.from_user.id,
            ch_id,
            mins,
            price
        )

        qr_id = qr['id']

        # Save the pending payment in MongoDB.
        payments_col.insert_one({
            "qr_id": qr_id,
            "user_id": call.from_user.id,
            "channel_id": ch_id,
            "plan_minutes": mins,
            "amount_paise": int(round(float(price) * 100)),
            "status": "pending",
            "created_at": datetime.now()
        })

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                "✅ I Have Paid — Check Payment",
                callback_data=f"paid_{qr_id}"
            )
        )

        markup.add(
            InlineKeyboardButton(
                "📞 Contact Admin",
                url=f"https://t.me/{CONTACT_USERNAME}"
            )
        )

        bot.send_photo(
            call.message.chat.id,
            qr['image_url'],
            caption=(
                f"Subscription: {mins} minutes\n"
                f"Amount: ₹{price}\n\n"
                f"Scan this Razorpay QR code to pay.\n"
                f"QR expires in 15 minutes.\n\n"
                f"After payment, click "
                f"'I Have Paid — Check Payment'."
            ),
            reply_markup=markup
        )

    except Exception as e:

        print("QR creation error:", e)

        bot.send_message(
            call.message.chat.id,
            "❌ Unable to create payment QR right now. "
            "Please try again later."
        )


# =========================================================
# USER: VERIFY PAYMENT
# =========================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith('paid_')
)
def verify_payment(call):

    bot.answer_callback_query(
        call.id,
        "Checking Razorpay payment..."
    )

    qr_id = call.data[len("paid_"):]

    payment_record = payments_col.find_one({
        "qr_id": qr_id,
        "user_id": call.from_user.id
    })

    if not payment_record:

        bot.send_message(
            call.message.chat.id,
            "❌ Payment request not found. "
            "Please generate a new QR."
        )

        return

    if payment_record['status'] == "approved":

        bot.send_message(
            call.message.chat.id,
            "✅ Your subscription has already been approved."
        )

        return

    try:

        result = get_qr_payments(qr_id)
        items = result.get('items', [])

        valid_payment = None

        for payment in items:

            if (
                payment.get('status') == 'captured'
                and payment.get('captured') is True
                and payment.get('amount')
                    == payment_record['amount_paise']
                and payment.get('currency') == 'INR'
            ):

                valid_payment = payment
                break

        # No captured payment found.
        if not valid_payment:

            bot.send_message(
                call.message.chat.id,
                "⏳ Payment has not been verified yet.\n\n"
                "If you have completed payment, please wait "
                "a moment and check again.\n\n"
                "No subscription has been approved yet."
            )

            return

        payment_id = valid_payment['id']

        # Atomically claim this payment to prevent duplicate approvals.
        claimed = payments_col.find_one_and_update(
            {
                "qr_id": qr_id,
                "user_id": call.from_user.id,
                "status": "pending"
            },
            {
                "$set": {
                    "status": "processing",
                    "razorpay_payment_id": payment_id,
                    "verified_at": datetime.now()
                }
            },
            return_document=ReturnDocument.AFTER
        )

        if not claimed:

            bot.send_message(
                call.message.chat.id,
                "✅ This payment is already being processed "
                "or has been approved."
            )

            return

        # Payment genuinely captured. Grant subscription.
        link = grant_subscription(
            payment_record['user_id'],
            payment_record['channel_id'],
            payment_record['plan_minutes']
        )

        # Mark approved only after successful invite delivery.
        payments_col.update_one(
            {"qr_id": qr_id},
            {
                "$set": {
                    "status": "approved",
                    "approved_at": datetime.now()
                }
            }
        )

        ch_data = channels_col.find_one({
            "channel_id": payment_record['channel_id']
        })

        price_rupees = payment_record['amount_paise'] / 100

        # Notify admin ONLY for genuine captured payments.
        bot.send_message(
            ADMIN_ID,
            f"🎉 Genuine Payment Received!\n\n"
            f"User: {call.from_user.first_name}\n"
            f"User ID: {call.from_user.id}\n"
            f"Channel: {ch_data['name'] if ch_data else 'Unknown'}\n"
            f"Plan: {payment_record['plan_minutes']} minutes\n"
            f"Amount: ₹{price_rupees:.2f}\n"
            f"Payment ID: {payment_id}\n\n"
            f"✅ Subscription approved automatically."
        )

        bot.send_message(
            call.message.chat.id,
            "🎉 Payment verified successfully!\n\n"
            "Your subscription has been approved.\n"
            f"Join your channel:\n{link}"
        )

    except Exception as e:

        print("Payment verification error:", e)

        bot.send_message(
            call.message.chat.id,
            "⚠️ Unable to verify payment right now.\n\n"
            "Your payment has not been rejected. "
            "Please try checking again shortly."
        )


# =========================================================
# CHANNEL MANAGEMENT CALLBACK
# =========================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith('manage_')
)
def manage_ch(call):

    ch_id = int(call.data.split('_')[1])

    ch_data = channels_col.find_one({
        "channel_id": ch_id
    })

    if not ch_data:
        bot.answer_callback_query(call.id, "Channel not found.")
        return

    bot_username = bot.get_me().username

    link = f"https://t.me/{bot_username}?start={ch_id}"

    bot.edit_message_text(
        f"Settings for: {ch_data['name']}\n\n"
        f"Your Link: {link}\n\n"
        f"To edit prices, use /add and forward "
        f"a message from this channel again.",
        call.message.chat.id,
        call.message.message_id
    )


# =========================================================
# AUTOMATIC SUBSCRIPTION EXPIRY
# =========================================================

def kick_expired_users():

    now = datetime.now().timestamp()

    expired_users = users_col.find({
        "expiry": {"$lte": now}
    })

    bot_username = bot.get_me().username

    for user in expired_users:

        try:

            bot.ban_chat_member(
                user['channel_id'],
                user['user_id']
            )

            bot.unban_chat_member(
                user['channel_id'],
                user['user_id']
            )

            rejoin_url = (
                f"https://t.me/{bot_username}"
                f"?start={user['channel_id']}"
            )

            markup = InlineKeyboardMarkup().add(
                InlineKeyboardButton(
                    "🔄 Re-join / Renew",
                    url=rejoin_url
                )
            )

            bot.send_message(
                user['user_id'],
                "⚠️ Your subscription has expired.\n\n"
                "To join again or renew, click below:",
                reply_markup=markup
            )

            users_col.delete_one({
                "_id": user['_id']
            })

        except Exception as e:

            print("Expiry processing error:", e)


# =========================================================
# STARTUP
# =========================================================

if __name__ == '__main__':

    keep_alive()

    scheduler = BackgroundScheduler()

    scheduler.add_job(
        kick_expired_users,
        'interval',
        minutes=1
    )

    scheduler.start()

    bot.remove_webhook()

    print("Bot is running...")

    bot.infinity_polling(
        timeout=20,
        long_polling_timeout=10
    )
