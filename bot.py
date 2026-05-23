"""
Swiggy Offer Telegram Bot
─────────────────────────
• 2 free credits on first /start
• 2 credits per offer run (20 requests)
• Recharge: 40 credits = ₹20 via UPI
• Admin: /addcredits USER_ID AMOUNT  |  /pending  |  /approve ID  |  /reject ID
• Single live-editing progress message (no spam)
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
    Message,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import database as db

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_ID         = int(os.environ["ADMIN_ID"])
ADMIN_UPI        = os.environ.get("ADMIN_UPI", "yourname@upi")
TARGET_URL       = "https://lookupinfo.in/swiggy/json.php"
FREE_CREDITS     = 2
COST_PER_RUN     = 2
RECHARGE_CREDITS = 40
RECHARGE_PRICE   = "₹20"
REQUIRED_KEYS    = {"token", "tid", "sid", "deviceId", "customerId", "mobile"}

# Spinner frames for animated progress
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_menu_kb(credits: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Run Offer  (2 credits)", callback_data="run_offer")],
        [InlineKeyboardButton(f"💳 Add Credits  │  🪙 {credits} credits", callback_data="add_credits")],
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats"),
         InlineKeyboardButton("ℹ️ How it works", callback_data="how_it_works")],
    ])

def run_again_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start New Process", callback_data="run_offer")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")],
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


async def safe_edit(msg: Message, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """Edit a message, ignoring 'message not modified' errors."""
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit failed: %s", e)
    except Exception as e:
        logger.warning("edit failed: %s", e)


async def safe_send(bot, chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        logger.warning("safe_send failed: %s", e)
        return None


def progress_bar(done: int, total: int = 20, width: int = 12) -> str:
    filled = int(width * done / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * done / total)
    return f"[{bar}] {done}/{total}  ({pct}%)"


# ── Playwright (optimised) ─────────────────────────────────────────────────────

async def run_offer_browser(json_text: str, progress_cb) -> dict:
    """
    progress_cb(stage, done, total) — called on every update so the
    caller can edit a single Telegram message in-place.

    Stages: 'init' | 'login' | 'running' | 'done' | 'error' | 'not_eligible'
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
                "--single-process",
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

        # Block heavy assets → faster load
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf,mp4,mp3}",
            lambda r: r.abort(),
        )

        try:
            await progress_cb("init", 0, 20)
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20_000)

            # Dismiss popup
            try:
                popup_btn = page.get_by_role("button", name=re.compile(r"Got it", re.I))
                await popup_btn.wait_for(state="visible", timeout=6_000)
                await popup_btn.click()
            except PWTimeout:
                pass

            # Paste JSON
            textarea = page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=8_000)
            await textarea.fill(json_text)

            # Login
            await progress_cb("login", 0, 20)
            login_btn = page.get_by_role("button", name=re.compile(r"Login with JSON", re.I))
            await asyncio.gather(
                login_btn.click(),
                page.wait_for_selector("text=Login Successful", timeout=12_000),
            )

            # Post-login popup
            try:
                pp = page.get_by_role("button", name=re.compile(r"Got it", re.I))
                await pp.wait_for(state="visible", timeout=3_000)
                await pp.click()
            except PWTimeout:
                pass

            # Grab mobile
            body_snap = await page.inner_text("body")
            mob_m = re.search(r"\b([6-9]\d{9})\b", body_snap)
            mobile_str = mob_m.group(1) if mob_m else "?"

            # Check Balance
            cb_btn = page.get_by_role("button", name=re.compile(r"Check Balance", re.I))
            await cb_btn.wait_for(state="visible", timeout=8_000)
            await cb_btn.click()
            await page.wait_for_timeout(1_000)

            bal_body = await page.inner_text("body")
            bal_amounts = list(dict.fromkeys(re.findall(r"₹\s*\d+", bal_body)))
            # Remove site-wide counters (very large numbers like ₹375500)
            bal_relevant = [b for b in bal_amounts if int(re.search(r"\d+", b).group()) < 10000]
            bal_str = " | ".join(bal_relevant) if bal_relevant else " | ".join(bal_amounts[:2])

            # Start Offers
            start_btn = page.get_by_role("button", name=re.compile(r"Start Offer Requests", re.I))
            await start_btn.wait_for(state="visible", timeout=8_000)
            await start_btn.click()

            await progress_cb("running", 0, 20, mobile=mobile_str, balance=bal_str)

            # Monitor
            last_done = 0
            spin_i = 0
            for tick in range(90):
                await page.wait_for_timeout(1_500)
                body = await page.inner_text("body")

                prog_m = re.search(r"(\d+)\s*/\s*20", body)
                done = int(prog_m.group(1)) if prog_m else last_done

                if done != last_done or tick % 3 == 0:
                    last_done = done
                    spin_i = (spin_i + 1) % len(SPINNER)
                    await progress_cb("running", done, 20,
                                      mobile=mobile_str, balance=bal_str,
                                      spinner=SPINNER[spin_i])

                if re.search(r"Process Complete|All 20 offer requests processed", body, re.I):
                    break
                if re.search(r"Not Eligible", body, re.I):
                    return {"success": True, "not_eligible": True}

            # Collect results
            final = await page.inner_text("body")
            requests = list(dict.fromkeys(
                re.findall(r"Request #\d+ ✓ Success — Receiver: .+", final)
            ))
            success_cnt = len(requests)

            earned_m = re.search(r"Total Earned[:\s]+₹\s*(\d+)", final)
            per_m    = re.search(r"Per Interaction[:\s]+₹\s*(\d+)", final)
            total_earned    = f"₹{earned_m.group(1)}" if earned_m else "N/A"
            per_interaction = f"₹{per_m.group(1)}" if per_m else "N/A"

            return {
                "success": True,
                "not_eligible": False,
                "success_cnt": success_cnt,
                "failed_cnt": 20 - success_cnt,
                "total_earned": total_earned,
                "per_interaction": per_interaction,
                "requests": requests,
                "mobile": mobile_str,
                "balance": bal_str,
            }

        except Exception as exc:
            logger.exception("Playwright error")
            return {"success": False, "error": str(exc)}
        finally:
            await browser.close()


# ── Bot handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)
    granted = db.give_free_credits(user.id, FREE_CREDITS)
    credits = db.get_credits(user.id)

    greeting = (
        f"👋 Welcome back, *{user.first_name}*!" if not granted
        else f"🎁 Hey *{user.first_name}*\\! You got *{FREE_CREDITS} free credits* to start\\!"
    )
    await update.message.reply_text(
        f"{greeting}\n\n"
        f"🪙 Balance: *{credits} credits*\n"
        f"💡 Each offer run costs *{COST_PER_RUN} credits* \\(20 requests\\)\n\n"
        "What would you like to do?",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(credits),
    )


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
            f"You have *{credits}* credits but need *{COST_PER_RUN}*.\n\n"
            "Please recharge to continue 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=recharge_kb(),
        )
        return

    await q.message.edit_text(
        "📤 *Send your Swiggy JSON credentials*\n\n"
        "Required keys:\n"
        "`token · tid · sid · deviceId · customerId · mobile`\n\n"
        "Paste the JSON below 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    ctx.user_data["awaiting_json"] = True


async def _cb_add_credits(q, ctx):
    await q.message.edit_text(
        f"💳 *Recharge Credits*\n\n"
        f"🏷  *{RECHARGE_CREDITS} Credits = {RECHARGE_PRICE}*\n\n"
        f"📲 Pay to UPI ID:\n`{ADMIN_UPI}`\n\n"
        "Steps:\n"
        "1️⃣ Tap *Copy UPI ID* below\n"
        "2️⃣ Open any UPI app & pay\n"
        "3️⃣ Tap *I Paid* and send your Transaction ID",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=recharge_kb(),
    )


async def _cb_i_paid(q, ctx):
    ctx.user_data["awaiting_utr"] = True
    await q.message.edit_text(
        "🔢 *Enter Transaction ID*\n\n"
        "Send your *UPI Transaction ID / UTR number*\n\n"
        "Example: `4239571234567`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


async def _cb_stats(q, ctx):
    uid  = q.from_user.id
    user = db.get_user(uid)
    runs = db.user_run_count(uid)
    await q.message.edit_text(
        f"📊 *Your Stats*\n\n"
        f"👤 {user['full_name']}\n"
        f"🪙 Credits: *{user['credits']}*\n"
        f"🚀 Completed Runs: *{runs}*\n"
        f"📅 Joined: {str(user['joined_at'])[:10]}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


async def _cb_how(q, ctx):
    await q.message.edit_text(
        "ℹ️ *How It Works*\n\n"
        "1️⃣ Each run sends *20 Swiggy offer requests* automatically\n"
        "2️⃣ Costs *2 credits* per run\n"
        "3️⃣ New users get *2 free credits* on first start\n"
        "4️⃣ Recharge: *40 credits = ₹20* via UPI\n\n"
        "💡 Typical result: ₹100 earned per run",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ── Admin callbacks ────────────────────────────────────────────────────────────

async def _admin_approve(q, recharge_id: int):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not authorised.", show_alert=True)
        return
    row = db.resolve_recharge(recharge_id, "approved")
    if not row:
        await q.answer("Not found.", show_alert=True)
        return
    new_bal = db.get_credits(row["user_id"])
    await q.message.edit_text(
        q.message.text + f"\n\n✅ *APPROVED* — +{row['credits_req']} credits",
        parse_mode=ParseMode.MARKDOWN,
    )
    await safe_send(
        q.get_bot(), row["user_id"],
        f"🎉 *Recharge Approved!*\n\n"
        f"✅ *+{row['credits_req']} credits* added\n"
        f"🪙 New balance: *{new_bal} credits*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(new_bal),
    )


async def _admin_reject(q, recharge_id: int):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not authorised.", show_alert=True)
        return
    row = db.resolve_recharge(recharge_id, "rejected")
    if not row:
        await q.answer("Not found.", show_alert=True)
        return
    await q.message.edit_text(
        q.message.text + "\n\n❌ *REJECTED*",
        parse_mode=ParseMode.MARKDOWN,
    )
    await safe_send(
        q.get_bot(), row["user_id"],
        "❌ *Recharge Rejected*\n\nPayment could not be verified.\nContact admin if this is an error.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Message handler ────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    db.upsert_user(user.id, user.username, user.full_name)

    # UTR flow
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
        now = datetime.now().strftime("%d %b %Y %H:%M")
        await safe_send(
            ctx.bot, ADMIN_ID,
            f"💳 *New Recharge #{rid}*\n\n"
            f"👤 [{user.full_name}](tg://user?id={user.id})\n"
            f"🆔 `{user.id}`\n"
            f"🔖 @{user.username or 'N/A'}\n"
            f"🔢 UTR: `{utr}`\n"
            f"💰 {RECHARGE_PRICE}  →  {RECHARGE_CREDITS} credits\n"
            f"🕐 {now}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_recharge_kb(rid),
        )
        return

    # JSON flow
    if ctx.user_data.get("awaiting_json"):
        ctx.user_data.pop("awaiting_json")
        ok, result = validate_json(text)
        if not ok:
            credits = db.get_credits(user.id)
            await update.message.reply_text(
                f"❌ *Invalid JSON*\n\n{result}\n\nTry again:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(credits),
            )
            return

        if not db.deduct_credits(user.id, COST_PER_RUN):
            credits = db.get_credits(user.id)
            await update.message.reply_text(
                f"❌ *Not enough credits* ({credits} remaining)",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=recharge_kb(),
            )
            return

        run_id  = db.start_run(user.id)
        credits = db.get_credits(user.id)

        # Send the ONE progress message we'll keep editing
        live_msg = await update.message.reply_text(
            _build_progress_text("init", 0, 20, run_id=run_id, credits=credits),
            parse_mode=ParseMode.MARKDOWN,
        )

        asyncio.create_task(
            _run_and_notify(ctx.application, user.id, text, run_id, credits, live_msg)
        )
        return

    # Fallback
    credits = db.get_credits(user.id)
    await update.message.reply_text(
        f"🪙 Balance: *{credits} credits*\n\nChoose an option:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(credits),
    )


# ── Progress text builder ──────────────────────────────────────────────────────

def _build_progress_text(stage: str, done: int, total: int = 20, **kw) -> str:
    run_id  = kw.get("run_id", "?")
    credits = kw.get("credits", "?")
    mobile  = kw.get("mobile", "")
    balance = kw.get("balance", "")
    spinner = kw.get("spinner", "⏳")

    header = f"🎯 *Offer Run #{run_id}*\n"
    if mobile:
        header += f"📱 Mobile: `{mobile}`\n"
    if balance:
        header += f"💰 Balance: `{balance}`\n"
    header += f"🪙 Credits remaining: *{credits}*\n"
    header += "─────────────────────\n"

    if stage == "init":
        return header + f"{spinner} *Initialising…*\nOpening page & logging in\\.\\.\\."

    if stage == "login":
        return header + f"{spinner} *Logging in…*\nAuthenticating your credentials\\.\\.\\."

    if stage == "running":
        bar = progress_bar(done, total)
        pct = int(100 * done / total)
        step_emoji = "🟢" if pct >= 75 else ("🟡" if pct >= 40 else "🔵")
        return (
            header +
            f"{step_emoji} *Running Offer Requests*\n\n"
            f"`{bar}`\n\n"
            f"{spinner} Processing request {done}/{total}…"
        )

    return header + f"{spinner} Please wait…"


# ── Background runner ──────────────────────────────────────────────────────────

async def _run_and_notify(app, chat_id: int, json_text: str,
                          run_id: int, credits: int, live_msg: Message):

    spin_counter = [0]

    async def progress_cb(stage, done, total=20, **kw):
        spin_counter[0] = (spin_counter[0] + 1) % len(SPINNER)
        kw.setdefault("spinner", SPINNER[spin_counter[0]])
        kw.setdefault("run_id", run_id)
        kw.setdefault("credits", credits)
        text = _build_progress_text(stage, done, total, **kw)
        await safe_edit(live_msg, text, parse_mode=ParseMode.MARKDOWN)

    result = await run_offer_browser(json_text, progress_cb)
    final_credits = db.get_credits(chat_id)

    # ── Error ──────────────────────────────────────────────────────────────────
    if not result["success"]:
        db.finish_run(run_id, "failed", 0, 0, "N/A")
        db.add_credits(chat_id, COST_PER_RUN)   # refund
        await safe_edit(
            live_msg,
            f"❌ *Run #{run_id} Failed*\n\n"
            f"`{result.get('error','Unknown error')[:300]}`\n\n"
            f"🔄 Credits refunded → 🪙 *{db.get_credits(chat_id)}*",
            reply_markup=run_again_kb(),
        )
        return

    # ── Not eligible ───────────────────────────────────────────────────────────
    if result.get("not_eligible"):
        db.finish_run(run_id, "not_eligible", 0, 0, "N/A")
        await safe_edit(
            live_msg,
            f"⚠️ *Run #{run_id} — Not Eligible*\n\n"
            "This account is not eligible for offers right now.\n\n"
            f"🪙 Credits remaining: *{final_credits}*",
            reply_markup=run_again_kb(),
        )
        return

    # ── Success ────────────────────────────────────────────────────────────────
    db.finish_run(
        run_id, "done",
        result["success_cnt"], result["failed_cnt"],
        result["total_earned"],
    )

    reqs_lines = result["requests"][:20]
    reqs_text  = "\n".join(reqs_lines) if reqs_lines else "No request log available."

    success_msg = (
        f"✅ *Run #{run_id} Complete!*\n\n"
        f"📱 Mobile: `{result.get('mobile','?')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Successful : *{result['success_cnt']}/20*\n"
        f"❌ Failed     : *{result['failed_cnt']}*\n"
        f"💰 Earned     : *{result['total_earned']}*\n"
        f"💵 Per Request: *{result['per_interaction']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Credits remaining: *{final_credits}*\n\n"
        f"*Request Log:*\n"
        f"```\n{reqs_text[:2500]}\n```"
    )

    await safe_edit(live_msg, success_msg, reply_markup=run_again_kb())


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
        f"🎉 *{amount} credits* added by admin!\n🪙 Balance: *{new_bal}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(new_bal),
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
            f"👤 `{row['user_id']}` @{row['username'] or 'N/A'}\n"
            f"🔢 UTR: `{row['utr']}`\n"
            f"🕐 {str(row['created_at'])[:16]}",
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
        await update.message.reply_text("Not found.")
        return
    new_bal = db.get_credits(row["user_id"])
    await update.message.reply_text(f"✅ Approved #{rid} → +{row['credits_req']} credits")
    await safe_send(
        ctx.bot, row["user_id"],
        f"🎉 *Recharge Approved!*\n+{row['credits_req']} credits\n🪙 Balance: *{new_bal}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(new_bal),
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
        await update.message.reply_text("Not found.")
        return
    await update.message.reply_text(f"❌ Rejected #{rid}")
    await safe_send(ctx.bot, row["user_id"],
                    "❌ Recharge rejected. Contact admin for help.")


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)
    credits = db.get_credits(user.id)
    await update.message.reply_text(
        f"🪙 *Balance: {credits} credits*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(credits),
    )


# ── App setup ──────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    db.init_db()
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
        .concurrent_updates(True)
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
