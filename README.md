# 🎯 Swiggy Offer Telegram Bot

Fully automated Swiggy JSON offer runner with **credit system**, **UPI recharge**, and **admin panel**.

---

## ✨ Features

| Feature | Detail |
|---------|--------|
| 🎁 Free Credits | 2 credits on first `/start` |
| 💳 Credit System | 2 credits = 1 offer run (20 requests) |
| 💰 Recharge | 40 credits = ₹20 via UPI |
| 🔔 Admin Panel | Approve/reject recharges inline |
| ⚡ Optimised | Asset blocking, 1.5s poll, `--single-process` Chromium |
| 🗄️ Persistent DB | SQLite on `/data` volume (survives redeploys) |

---

## 🤖 Bot Flow

```
/start
  → 2 free credits granted (once)
  → Main Menu with credit balance

[Run Offer]
  → Checks credits ≥ 2
  → Ask for JSON
  → Validate JSON (6 required keys)
  → Deduct 2 credits
  → Playwright background task:
      1. Open page
      2. Dismiss popup
      3. Paste JSON → Login
      4. Check Balance
      5. Start Offer (20x)
      6. Monitor & send live updates
      7. Final result with earnings
  → Credits refunded on browser error

[Add Credits]
  → Show UPI + price
  → Copy UPI ID button
  → I Paid → enter UTR
  → Admin notified instantly
  → Admin approves/rejects inline
  → User gets notification
```

---

## 🚀 Deploy to Railway

### 1. Push to GitHub

```bash
cd swiggy-bot
git remote add origin https://github.com/YOUR_USER/swiggy-offer-bot.git
git push -u origin master
```

### 2. Create Railway project

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
2. Select your repo

### 3. Add Volume (for SQLite)

1. Your Service → **Volumes** → **Add Volume**
2. Name: `swiggy-bot-data`
3. Mount path: `/data`

### 4. Set Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | BotFather token |
| `ADMIN_ID` | ✅ | Your Telegram numeric user ID |
| `ADMIN_UPI` | ✅ | Your UPI ID e.g. `name@upi` |

### 5. Deploy

Railway auto-builds and deploys on every `git push`.

---

## 🛠️ Admin Commands

| Command | Description |
|---------|-------------|
| `/addcredits USER_ID AMOUNT` | Manually add credits |
| `/pending` | List pending recharge requests |
| `/approve RECHARGE_ID` | Approve a recharge |
| `/reject RECHARGE_ID` | Reject a recharge |

Admin also receives **inline approve/reject buttons** for every new recharge request.

---

## 📦 Local Dev

```bash
pip install -r requirements.txt
playwright install chromium
export BOT_TOKEN=xxx ADMIN_ID=123456789 ADMIN_UPI=name@upi
python bot.py
```
