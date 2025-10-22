import os
import json
import asyncio
import logging
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, ContextTypes

# ------------------ CONFIG ------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")  # bắt buộc
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")  # tuỳ chọn
DATA_FILE = "subscribers.json"
INTERVAL_SECONDS = 10 * 60   # 10 phút
SYMBOL = "ETHFI"
BINANCE_PERP = "ETHFIUSDT"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)

# --------------- STORAGE HELPERS ---------------
def load_subscribers():
    if not os.path.exists(DATA_FILE):
        return set()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            ids = json.load(f)
            return set(ids)
    except Exception:
        return set()

def save_subscribers(subs: set):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(subs), f)

SUBSCRIBERS = load_subscribers()

# --------------- DATA SOURCES ------------------
def get_coingecko_ethfi():
    """
    CoinGecko: price, market cap, 24h volume (USD)
    """
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": "ether-fi",          # CoinGecko id cho ETHFI
        "price_change_percentage": "1h,24h,7d"
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    arr = r.json()
    if not arr:
        return {}
    x = arr[0]
    return {
        "price": x.get("current_price"),
        "mcap": x.get("market_cap"),
        "vol24": x.get("total_volume"),
        "chg1h": x.get("price_change_percentage_1h_in_currency"),
        "chg24h": x.get("price_change_percentage_24h_in_currency"),
        "chg7d": x.get("price_change_percentage_7d_in_currency")
    }

def get_binance_funding_latest(symbol=BINANCE_PERP):
    """
    Binance Futures funding: lần settle gần nhất + dự báo hiện tại (premiumIndex)
    """
    # Funding đã settle
    hist = requests.get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        params={"symbol": symbol, "limit": 1}, timeout=15
    ).json()
    last_funding, last_time = None, None
    if isinstance(hist, list) and hist:
        try:
            last_funding = float(hist[0]["fundingRate"])
            last_time = int(hist[0]["fundingTime"])
        except Exception:
            pass

    # Funding dự báo hiện tại
    prem = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": symbol}, timeout=15
    ).json()
    predicted = None
    if isinstance(prem, dict) and "lastFundingRate" in prem:
        try:
            predicted = float(prem["lastFundingRate"])
        except Exception:
            predicted = None

    return {"last_funding": last_funding, "last_time": last_time, "predicted": predicted}

def get_binance_oi_change(symbol=BINANCE_PERP):
    """
    Open Interest hiện tại và thay đổi so với ~24h trước (theo dữ liệu 1h x 25 điểm)
    """
    oi_latest = requests.get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        params={"symbol": symbol}, timeout=15
    ).json()
    curr_oi = 0.0
    try:
        curr_oi = float(oi_latest.get("openInterest", 0.0))
    except Exception:
        pass

    hist = requests.get(
        "https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": symbol, "period": "1h", "limit": 25}, timeout=15
    ).json()
    past_oi = None
    if isinstance(hist, list) and len(hist) >= 24:
        try:
            past_oi = float(hist[0]["sumOpenInterest"])
        except Exception:
            past_oi = None

    delta, pct = None, None
    if past_oi and past_oi > 0:
        delta = curr_oi - past_oi
        pct = (delta / past_oi) * 100.0

    return {"oi": curr_oi, "oi_24h_delta": delta, "oi_24h_pct": pct}

def get_llama_tvl():
    """
    DeFiLlama: TVL của ether.fi
    """
    url = "https://api.llama.fi/protocol/ether.fi"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    tvl = data.get("tvl", 0)
    if isinstance(tvl, (int, float)):
        return tvl
    cur = data.get("currentChainTvls", {})
    if isinstance(cur, dict):
        return sum(cur.values())
    return 0

# --------------- FORMATTING ------------------
def pretty_usd(x):
    if x is None:
        return "—"
    try:
        if x >= 1_000_000_000:
            return f"${x/1_000_000_000:.2f}B"
        if x >= 1_000_000:
            return f"${x/1_000_000:.2f}M"
        if x >= 1_000:
            return f"${x/1_000:.2f}K"
        return f"${x:,.2f}"
    except Exception:
        return str(x)

def format_report():
    cg = get_coingecko_ethfi() or {}
    fund = get_binance_funding_latest()
    oi = get_binance_oi_change()
    try:
        tvl = get_llama_tvl()
    except Exception:
        tvl = None

    price = cg.get("price")
    mcap = cg.get("mcap")
    vol24 = cg.get("vol24")
    chg1h = cg.get("chg1h")
    chg24h = cg.get("chg24h")

    # --- Tín hiệu nhanh ---
    signal = "Neutral"
    reasons = []
    if fund.get("predicted") is not None and fund["predicted"] <= 0:
        reasons.append("Funding ≤ 0 (Short đông hơn)")
    if oi.get("oi_24h_pct") is not None:
        if oi["oi_24h_pct"] > 0:
            reasons.append("OI ↑ (mở thêm vị thế)")
        elif oi["oi_24h_pct"] < 0:
            reasons.append("OI ↓ (đóng vị thế)")

    if fund.get("predicted") is not None and fund["predicted"] < 0 and (oi.get("oi_24h_pct") or 0) >= 0:
        signal = "Bullish ⚡ (nguy cơ Short squeeze)"
    elif fund.get("predicted") is not None and fund["predicted"] > 0 and (oi.get("oi_24h_pct") or 0) > 3:
        signal = "Caution ⚠ (Long crowded)"
    elif fund.get("predicted") is not None and fund["predicted"] > 0 and (oi.get("oi_24h_pct") or 0) < 0:
        signal = "Pullback risk ⚠"

    # --- ETA tới $1.8 (ước lượng thô) ---
    eta_text = "—"
    try:
        if price and price > 0:
            gap = 1.8 - float(price)
            if gap <= 0:
                eta_text = "Đã ≥ $1.8"
            else:
                vol_factor = (vol24 or 0) / 70_000_000  # chuẩn 70M
                vol_factor = max(0.3, min(vol_factor, 2.0))
                daily_pct = abs(chg24h or 0) or 3.0
                daily_move = max(1.5, min(daily_pct, 8.0)) / 100.0
                days = (gap / float(price)) / daily_move
                days /= vol_factor
                eta_text = (
                    f"≈ {int(days*24)}–{int(days*24)+6} giờ (ước tính)"
                    if days < 1 else
                    f"≈ {int(days)}–{int(days)+4} ngày (ước tính)"
                )
    except Exception:
        pass

    dt = datetime.now(timezone.utc).astimezone().strftime("%d/%m %H:%M")
    msg = (
        f"📊 <b>ETHFI Cập nhập giá và Phân tích</b> — {dt}\n"
        f"• Giá: <b>${(price or 0):.4f}</b>  |  24h vol: <b>{pretty_usd(vol24)}</b>\n"
        f"• MCap: {pretty_usd(mcap)}  |  TVL: {pretty_usd(tvl)}\n"
        f"• 1h Δ%: {(chg1h or 0):+.2f}%  | 24h Δ%: {(chg24h or 0):+.2f}%\n"
        f"• Funding (pred): <b>{(fund.get('predicted') or 0):+.4%}</b>\n"
        f"• OI: <b>{oi.get('oi', 0):,.2f}</b>  |  OI 24h: "
        f"{(oi.get('oi_24h_delta') or 0):+.2f} ({(oi.get('oi_24h_pct') or 0):+.2f}%)\n"
        f"\n"
        f"🔎 Nhận định: <b>{signal}</b>\n"
        f"• {', '.join(reasons) if reasons else 'Đang tích lũy, chưa lệch phe.'}\n"
        f"🎯 ETA về $1.8: <b>{eta_text}</b>\n"
        f"\n"
        f"ℹ️ Nguồn: CoinGecko, Binance Futures, DeFiLlama"
        f"\n"
        f"🧑‍💻Người lập trình: <span style='color:green;'><b>Thanos Huang</b></span>"
    )
    return msg

# --------------- TELEGRAM HANDLERS ---------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    SUBSCRIBERS.add(chat_id)
    save_subscribers(SUBSCRIBERS)
    await update.message.reply_html(
        f"Chào <b>{user.first_name or ''}</b>! 👍\n"
        f"Bạn đã đăng ký nhận <b>ETHFI 4h Update</b> ở đây.\n"
        f"Dùng /now để nhận báo cáo ngay."
    )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in SUBSCRIBERS:
        SUBSCRIBERS.remove(chat_id)
        save_subscribers(SUBSCRIBERS)
        await update.message.reply_text("Đã hủy đăng ký cập nhật 4h.")
    else:
        await update.message.reply_text("Bạn chưa đăng ký.")

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = format_report()
        await update.message.reply_html(msg, disable_web_page_preview=True)
    except Exception as e:
        logging.exception(e)
        await update.message.reply_text("Lỗi khi lấy dữ liệu. Thử lại sau nhé.")

# --------- JOBQUEUE: gửi định kỳ mỗi 4 giờ ----------
async def send_broadcast(context: ContextTypes.DEFAULT_TYPE):
    if not SUBSCRIBERS:
        return
    try:
        msg = format_report()
        for chat_id in list(SUBSCRIBERS):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.warning(f"Send failed to {chat_id}: {e}")
    except Exception as e:
        logging.exception(f"Broadcast error: {e}")

async def setup_jobs(app: Application):
    # Gửi ngay 1 bản sau khi bot khởi động (first=1 giây),
    # và lặp lại mỗi INTERVAL_SECONDS
    app.job_queue.run_repeating(send_broadcast, interval=INTERVAL_SECONDS, first=1)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trong .env")

    # 👉 Tăng timeout & tắt HTTP/2 để tránh lỗi timeout trên một số mạng/Windows
    req = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .request(req)          # dùng request custom
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("now", now))
    application.add_handler(CommandHandler("stop", stop))

    # Đăng ký job định kỳ 4 giờ
    application.post_init = setup_jobs

    # Bắt đầu polling (PTB sẽ tự tạo event loop)
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
