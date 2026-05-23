"""
Swiggy Offer Telegram Bot
─────────────────────────
• 2 free credits on first /start
• 2 credits per offer run (20 requests)
• Recharge: 40 credits = ₹20 via UPI
• Admin: /addcredits USER_ID AMOUNT  |  /pending  |  /approve ID  |  /reject ID
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import database as db

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])          # your Telegram numeric ID
ADMIN_UPI   = os.environ.get("ADMIN_UPI", "yourname@upi")
TARGET_URL  = "https://lookupinfo.in/swiggy/json.php"
FREE_CREDITS    = 2
COST_PER_RUN    = 2
RECHARGE_CREDITS = 40
RECHARGE_PRICE  = "₹20"
REQUIRED_KEYS   = {"token", "tid", "sid", "deviceId", "customerId", "mobile"}

# ConversationHandler states
WAIT_JSON   = 1
WAIT_UTR    = 2

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_menu_kb(credits: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Run Offer (2 credits)", callback_data="run_offer")],
        [InlineKeyboardButton(f"💳 Add Credits  |  Balance: {credits} 🪙", callback_data="add_credits")],
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")],
        [InlineKeyboardButton("ℹ️ How it works", callback_data="how_it_works")],
    ])


def recharge_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy UPI ID", callback_data=f"copy_upi:{ADMIN_UPI}")],
        [InlineKeyboardButton("✅ I Paid", callback_data="i_paid")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_main")]
    ])


def admin_recharge_kb(recharge_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve:{recharge_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"admin_reject:{recharge_id}"),
    ]])


# ── Helpers ────────────────────────────────────────────────────────────────────

def validate_json(text: str) -> tuple[bool, dict | str]:
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON:\n`{e}`"
    if not isinstance(data, dict):
        return False, "JSON must be an object `{...}`"
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        return False, f"Missing keys: `{', '.join(sorted(missing))}`"
    return True, data


def esc(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def safe_send(bot, chat_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        logger.warning("safe_send failed: %s", e)


# ── Playwright (optimised) ─────────────────────────────────────────────────────

async def run_offer_browser(json_text: str, status_cb) -> dict:
    """
    Optimised flow:
    1. Open page (networkidle)          — concurrent with dismissing popup
    2. Dismiss popup
    3. Fill JSON + click Login
    4. Check Balance
    5. Start Offer Requests (20x)
    6. Poll for completion (2 s intervals)
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",          # faster startup in Docker
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            bypass_csp=True,
        )
        page = await ctx.new_page()

        # Block heavy assets → faster page load
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf,mp4,mp3}",
            lambda r: r.abort(),
        )

        try:
            await status_cb("🌐 Opening page…")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20_000)

            # ── Dismiss popup ────────────────────────────────────────────────
            await status_cb("🪟 Dismissing popup…")
            try:
                popup_btn = page.get_by_role("button", name=re.compile(r"Got it", re.I))
                await popup_btn.wait_for(state="visible", timeout=6_000)
                await popup_btn.click()
            except PWTimeout:
                pass  # no popup

            # ── Paste JSON ────────────────────────────────────────────────────
            await status_cb("📋 Pasting credentials…")
            textarea = page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=8_000)
            await textarea.fill(json_text)

            # ── Login ─────────────────────────────────────────────────────────
            await status_cb("🔐 Logging in…")
            login_btn = page.get_by_role("button", name=re.compile(r"Login with JSON", re.I))
            await asyncio.gather(
                login_btn.click(),
                page.wait_for_selector("text=Login Successful", timeout=12_000),
            )

            # grab mobile from page
            body_snap = await page.inner_text("body")
            mob_m = re.search(r"\b(9\d{9}|[6-9]\d{9})\b", body_snap)
            mobile_str = mob_m.group(1) if mob_m else "?"
            await status_cb(f"✅ Logged in! Mobile: `{mobile_str}`")

            # post-login popup
            try:
                pp = page.get_by_role("button", name=re.compile(r"Got it", re.I))
                await pp.wait_for(state="visible", timeout=3_000)
                await pp.click()
            except PWTimeout:
                pass

            # ── Check Balance ─────────────────────────────────────────────────
            await status_cb("💰 Checking balance…")
            cb_btn = page.get_by_role("button", name=re.compile(r"Check Balance", re.I))
            await cb_btn.wait_for(state="visible", timeout=8_000)
            await cb_btn.click()
            await page.wait_for_timeout(1_200)

            bal_body = await page.inner_text("body")
            bal_amounts = re.findall(r"₹\s*\d+", bal_body)
            bal_str = " | ".join(dict.fromkeys(bal_amounts)) if bal_amounts else "N/A"
            await status_cb(f"💰 Balance: {bal_str}")

            # ── Start Offers ──────────────────────────────────────────────────
            await status_cb("🚀 Starting 20 offer requests…")
            start_btn = page.get_by_role("button", name=re.compile(r"Start Offer Requests", re.I))
            await start_btn.wait_for(state="visible", timeout=8_000)
            await start_btn.click()

            # ── Monitor ───────────────────────────────────────────────────────
            last_prog = ""
            for tick in range(90):
                await page.wait_for_timeout(1_500)   # 1.5s poll (was 2s)
                body = await page.inner_text("body")

                prog_m = re.search(r"(\d+)\s*/\s*20", body)
                if prog_m:
                    prog = f"{prog_m.group(1)}/20"
                    if prog != last_prog:
                        last_prog = prog
                        await status_cb(f"📊 {prog} requests done…")

                if re.search(r"Process Complete|All 20 offer requests processed", body, re.I):
                    break
                if re.search(r"Not Eligible", body, re.I):
                    return {"success": True, "not_eligible": True, "body": body}

            # ── Collect results ───────────────────────────────────────────────
            final = await page.inner_text("body")
            requests = list(dict.fromkeys(
                re.findall(r"Request #\d+ ✓ Success — Receiver: .+", final)
            ))
            success_cnt = len(requests)
            failed_cnt  = len(re.findall(r"FAILED|\d+\nFAILED", final))

            earned_m = re.search(r"Total Earned[:\s]+₹\s*(\d+)", final)
            per_m    = re.search(r"Per Interaction[:\s]+₹\s*(\d+)", final)
            total_earned    = f"₹{earned_m.group(1)}" if earned_m else "N/A"
            per_interaction = f"₹{per_m.group(1)}" if per_m else "N/A"

            return {
                "success": True,
                "not_eligible": False,
                "success_cnt": success_cnt,
                "failed_cnt": failed_cnt,
                "total_earned": total_earned,
                "per_interaction": per_interaction,
                "requests": requests,
            }

        except Exception as exc:
            logger.exception("Playwright error")
            return {"success": False, "error": str(exc)}
        finally:
            await browser.close()


# ── Bot command/callback handlers ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row  = db.upsert_user(user.id, user.username, user.full_name)
    granted = db.give_free_credits(user.id, FREE_CREDITS)

    greeting = (
        f"👋 Welcome back, *{user.first_name}*!" if not granted
        else f"👋 Hey *{user.first_name}*! You just got *{FREE_CREDITS} free credits* 🎁"
    )
    credits = db.get_credits(user.id)

    await update.message.reply_text(
        f"{greeting}\n\n"
        f"🪙 Balance: *{credits} credits*\n"
        f"💡 Each offer run costs *{COST_PER_RUN} credits* (20 requests)\n\n"
        "What would you like to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(credits),
    )


# ── Callback: main menu buttons ────────────────────────────────────────────────

async def cb_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "run_offer":
        await _cb_run_offer(q, ctx)
    elif data == "add_credits":
        await _cb_add_credits(q, ctx)
    elif data.startswith("copy_upi:"):
        upi = data.split(":", 1)[1]
        await q.message.reply_text(f"`{upi}`", parse_mode=ParseMode.MARKDOWN)
    elif data == "i_paid":
        await _cb_i_paid(q, ctx)
    elif data == "back_main":
        credits = db.get_credits(q.from_user.id)
        await q.message.edit_text(
            f"🪙 Balance: *{credits} credits*\n\nWhat would you like to do?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(credits),
        )
    elif data == "my_stats":
        await _cb_stats(q, ctx)
    elif data == "how_it_works":
        await _cb_how(q, ctx)
    elif data.startswith("admin_approve:"):
        await _admin_approve(q, int(data.split(":")[1]))
    elif data.startswith("admin_reject:"):
        await _admin_reject(q, int(data.split(":")[1]))


async def _cb_run_offer(q, ctx):
    uid     = q.from_user.id
    credits = db.get_credits(uid)
    if credits < COST_PER_RUN:
        await q.message.edit_text(
            f"❌ *Insufficient Credits*\n\n"
            f"You have *{credits} credits* but need *{COST_PER_RUN}*.\n\n"
            "Please recharge to continue:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=recharge_kb(),
        )
        return

    await q.message.edit_text(
        "📤 *Send me your Swiggy JSON credentials*\n\n"
        "Required keys: `token · tid · sid · deviceId · customerId · mobile`\n\n"
        "Just paste the JSON below 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    ctx.user_data["awaiting_json"] = True


async def _cb_add_credits(q, ctx):
    await q.message.edit_text(
        f"💳 *Recharge Credits*\n\n"
        f"💰 *{RECHARGE_CREDITS} Credits = {RECHARGE_PRICE}*\n\n"
        f"Send payment to UPI:\n`{ADMIN_UPI}`\n\n"
        "After paying:\n"
        "1️⃣ Copy the UPI ID above\n"
        "2️⃣ Pay via any UPI app\n"
        "3️⃣ Click *I Paid* and share your Transaction ID",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=recharge_kb(),
    )


async def _cb_i_paid(q, ctx):
    ctx.user_data["awaiting_utr"] = True
    await q.message.edit_text(
        "🔢 *Submit Transaction ID*\n\n"
        "Please send your *UPI Transaction ID / Reference Number* (UTR)\n\n"
        "Example: `4239571234567` or `UPI123456789012`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


async def _cb_stats(q, ctx):
    uid  = q.from_user.id
    user = db.get_user(uid)
    runs = db.user_run_count(uid)
    await q.message.edit_text(
        f"📊 *Your Stats*\n\n"
        f"👤 Name: {user['full_name']}\n"
        f"🪙 Credits: *{user['credits']}*\n"
        f"🚀 Successful Runs: *{runs}*\n"
        f"📅 Joined: {user['joined_at'][:10]}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


async def _cb_how(q, ctx):
    await q.message.edit_text(
        "ℹ️ *How It Works*\n\n"
        "1️⃣ Each *offer run* sends 20 Swiggy requests automatically\n"
        "2️⃣ Each run costs *2 credits*\n"
        "3️⃣ New users get *2 free credits* to try once\n"
        "4️⃣ Recharge: *40 credits = ₹20* via UPI\n\n"
        "💡 Typical result: ₹100 earned per run (₹5–10/request)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ── Admin approval callbacks ───────────────────────────────────────────────────

async def _admin_approve(q, recharge_id: int):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not authorised.", show_alert=True)
        return
    row = db.resolve_recharge(recharge_id, "approved")
    if not row:
        await q.answer("Recharge not found.", show_alert=True)
        return
    new_bal = db.get_credits(row["user_id"])
    await q.message.edit_text(
        q.message.text + f"\n\n✅ *APPROVED* — {row['credits_req']} credits added.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await safe_send(
        q.get_bot(),
        row["user_id"],
        f"🎉 *Recharge Approved!*\n\n"
        f"✅ *+{row['credits_req']} credits* added to your account\n"
        f"🪙 New balance: *{new_bal} credits*\n\n"
        "Use /start to run offers!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _admin_reject(q, recharge_id: int):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not authorised.", show_alert=True)
        return
    row = db.resolve_recharge(recharge_id, "rejected")
    if not row:
        await q.answer("Recharge not found.", show_alert=True)
        return
    await q.message.edit_text(
        q.message.text + "\n\n❌ *REJECTED*",
        parse_mode=ParseMode.MARKDOWN,
    )
    await safe_send(
        q.get_bot(),
        row["user_id"],
        "❌ *Recharge Rejected*\n\n"
        "Your payment could not be verified.\n"
        "Please contact the admin if you believe this is an error.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Message handler (JSON input & UTR input) ───────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # ── UTR flow ──────────────────────────────────────────────────────────────
    if ctx.user_data.get("awaiting_utr"):
        ctx.user_data.pop("awaiting_utr")
        utr = text[:100]
        rid = db.create_recharge(user.id, user.username, utr)
        credits = db.get_credits(user.id)

        await update.message.reply_text(
            "✅ *Payment Submitted!*\n\n"
            f"🔢 UTR: `{utr}`\n"
            f"💰 Amount: {RECHARGE_PRICE}\n"
            f"🪙 Credits requested: {RECHARGE_CREDITS}\n\n"
            "⏳ Admin will verify and credit your account shortly.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(credits),
        )
        # Notify admin
        now = datetime.now().strftime("%d %b %Y %H:%M")
        await safe_send(
            ctx.bot,
            ADMIN_ID,
            f"💳 *New Recharge Request* #{rid}\n\n"
            f"👤 User: [{user.full_name}](tg://user?id={user.id})\n"
            f"🆔 User ID: `{user.id}`\n"
            f"🔖 Username: @{user.username or 'N/A'}\n"
            f"🔢 UTR: `{utr}`\n"
            f"💰 Amount: {RECHARGE_PRICE}\n"
            f"🪙 Credits: {RECHARGE_CREDITS}\n"
            f"🕐 Time: {now}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_recharge_kb(rid),
        )
        return

    # ── JSON flow ─────────────────────────────────────────────────────────────
    if ctx.user_data.get("awaiting_json"):
        ctx.user_data.pop("awaiting_json")
        ok, result = validate_json(text)
        if not ok:
            credits = db.get_credits(user.id)
            await update.message.reply_text(
                f"❌ *Invalid JSON*\n\n{result}\n\nPlease try again.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(credits),
            )
            return

        # Final credit check
        if not db.deduct_credits(user.id, COST_PER_RUN):
            credits = db.get_credits(user.id)
            await update.message.reply_text(
                f"❌ *Insufficient Credits* ({credits} remaining)\n\nPlease recharge:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=recharge_kb(),
            )
            return

        run_id  = db.start_run(user.id)
        credits = db.get_credits(user.id)
        await update.message.reply_text(
            f"✅ *JSON Validated — Run #{run_id} Started!*\n\n"
            f"📱 Mobile: `{result.get('mobile','?')}`\n"
            f"🪙 Credits used: *{COST_PER_RUN}* | Remaining: *{credits}*\n\n"
            "I'll send live updates as the process runs ⚡",
            parse_mode=ParseMode.MARKDOWN,
        )
        asyncio.create_task(
            _run_and_notify(ctx.application, user.id, text, run_id)
        )
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    credits = db.get_credits(user.id)
    db.upsert_user(user.id, user.username, user.full_name)
    await update.message.reply_text(
        f"👋 Hi! Use the menu below.\n🪙 Balance: *{credits} credits*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(credits),
    )


# ── Background offer runner ────────────────────────────────────────────────────

async def _run_and_notify(app, chat_id: int, json_text: str, run_id: int):
    async def status_cb(msg: str):
        await safe_send(app.bot, chat_id, msg, parse_mode=ParseMode.MARKDOWN)

    result = await run_offer_browser(json_text, status_cb)

    if not result["success"]:
        db.finish_run(run_id, "failed", 0, 0, "N/A")
        # Refund credits on browser error
        db.add_credits(chat_id, COST_PER_RUN)
        await safe_send(
            app.bot, chat_id,
            f"❌ *Offer run failed* (credits refunded)\n\n`{result.get('error','Unknown error')}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if result.get("not_eligible"):
        db.finish_run(run_id, "not_eligible", 0, 0, "N/A")
        credits = db.get_credits(chat_id)
        await safe_send(
            app.bot, chat_id,
            f"❌ *Not Eligible*\n\n"
            "This account is not eligible for offers right now.\n"
            f"🪙 Your balance: *{credits} credits*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(credits),
        )
        return

    db.finish_run(
        run_id, "done",
        result["success_cnt"], result["failed_cnt"],
        result["total_earned"],
    )

    reqs_text = "\n".join(result["requests"][:20])
    credits   = db.get_credits(chat_id)

    await safe_send(
        app.bot, chat_id,
        f"🎉 *Offer Run Complete!* \\(Run \\#{run_id}\\)\n\n"
        f"✅ Successful: `{result['success_cnt']}/20`\n"
        f"❌ Failed: `{result['failed_cnt']}`\n"
        f"💰 Total Earned: `{result['total_earned']}`\n"
        f"💵 Per Interaction: `{result['per_interaction']}`\n\n"
        f"```\n{reqs_text[:2800]}\n```\n\n"
        f"🪙 Remaining Credits: *{credits}*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(credits),
    )


# ── Admin commands ─────────────────────────────────────────────────────────────

async def cmd_addcredits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Usage: /addcredits USER_ID AMOUNT")
        return
    uid, amount = int(args[0]), int(args[1])
    new_bal = db.add_credits(uid, amount)
    await update.message.reply_text(
        f"✅ Added *{amount}* credits to `{uid}`\nNew balance: *{new_bal}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    await safe_send(
        ctx.bot, uid,
        f"🎉 *{amount} credits* have been added to your account by admin!\n"
        f"🪙 New balance: *{new_bal} credits*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = db.get_pending_recharges()
    if not rows:
        await update.message.reply_text("No pending recharges ✅")
        return
    for row in rows:
        await update.message.reply_text(
            f"💳 *Recharge #{row['id']}*\n"
            f"👤 User ID: `{row['user_id']}`\n"
            f"🔖 @{row['username'] or 'N/A'}\n"
            f"🔢 UTR: `{row['utr']}`\n"
            f"🕐 {row['created_at']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_recharge_kb(row["id"]),
        )


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /approve RECHARGE_ID")
        return
    rid = int(ctx.args[0])
    row = db.resolve_recharge(rid, "approved")
    if not row:
        await update.message.reply_text("Recharge not found.")
        return
    new_bal = db.get_credits(row["user_id"])
    await update.message.reply_text(f"✅ Approved #{rid} → +{row['credits_req']} credits to `{row['user_id']}`")
    await safe_send(
        ctx.bot, row["user_id"],
        f"🎉 *Recharge Approved!*\n+{row['credits_req']} credits\n🪙 Balance: *{new_bal}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /reject RECHARGE_ID")
        return
    rid = int(ctx.args[0])
    row = db.resolve_recharge(rid, "rejected")
    if not row:
        await update.message.reply_text("Recharge not found.")
        return
    await update.message.reply_text(f"❌ Rejected #{rid}")
    await safe_send(
        ctx.bot, row["user_id"],
        "❌ Your recharge was rejected. Contact admin for help.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show user their balance with menu."""
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)
    credits = db.get_credits(user.id)
    await update.message.reply_text(
        f"🪙 *Your Balance: {credits} credits*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(credits),
    )


# ── App setup ──────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",      "Start the bot / main menu"),
        BotCommand("balance",    "Check your credit balance"),
        BotCommand("addcredits", "[Admin] Add credits to a user"),
        BotCommand("pending",    "[Admin] List pending recharges"),
        BotCommand("approve",    "[Admin] Approve a recharge"),
        BotCommand("reject",     "[Admin] Reject a recharge"),
    ])


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)       # handle multiple users in parallel
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("balance",    cmd_balance))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("pending",    cmd_pending))
    app.add_handler(CommandHandler("approve",    cmd_approve))
    app.add_handler(CommandHandler("reject",     cmd_reject))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
