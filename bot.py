import os
import json
import datetime
import logging
import re
import time
from flask import Flask, request, g, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient

# --- 1. SYSTEM SETUP ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HybridBot")
app = Flask(__name__)

# --- GLOBAL CONFIG ---
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
# Secret key for the website checkout endpoint (falls back to the legacy hardcoded value)
WEB_ORDER_API_KEY = os.getenv('WEB_ORDER_API_KEY', 'BUARON_SECURE_2026_MAX')

# Israel timezone (handles summer/winter clock automatically; falls back to UTC+3)
try:
    from zoneinfo import ZoneInfo
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    ISRAEL_TZ = None

# Supabase Setup
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
try:
    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    logger.error("Supabase connection failed (Check .env)")
    supabase = None

# Google AI Setup
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)


# ==============================================================================
#                 THE DYNAMIC TWILIO ROUTER (MULTI-TENANT)
# ==============================================================================
# This stores active connections in RAM so we don't query Supabase every single second
active_twilio_clients = {}

def get_dynamic_twilio_client(bot_number):
    """Pulls the exact Twilio Client for the specific business being texted."""
    clean_num = str(bot_number).replace("whatsapp:", "").replace("+", "").strip()

    # 1. Check if we already have it in RAM
    if clean_num in active_twilio_clients:
        return active_twilio_clients[clean_num]

    # 2. If not, ask Supabase for the keys
    if supabase:
        try:
            res = supabase.table('clients').select("*").ilike('phone_number', f'%{clean_num}%').execute()
            if res.data and len(res.data) > 0:
                business = res.data[0]
                sid = business.get('twilio_sid')
                token = business.get('twilio_token')

                # If the DB has keys, build the client and save to RAM
                if sid and token:
                    client = Client(sid, token)
                    active_twilio_clients[clean_num] = client
                    return client
                else:
                    logger.error(f"Missing SID or Token in Supabase for {clean_num}")
        except Exception as e:
            logger.error(f"Failed to load Twilio client for {clean_num}: {e}")

    return None

def ensure_whatsapp_prefix(phone):
    if not phone:
        return None
    clean = phone.strip()
    if not clean.startswith("whatsapp:"):
        return f"whatsapp:{clean}"
    return clean

# --- BUSINESS CONFIG CACHE (saves a Supabase query on every message) ---
business_cache = {}
BUSINESS_CACHE_TTL = 60  # seconds — edits in Supabase take effect within a minute

def get_business_from_supabase(bot_number):
    if not supabase: return None
    clean_num = str(bot_number).replace("whatsapp:", "").replace("+", "").strip()

    cached = business_cache.get(clean_num)
    if cached and time.time() - cached[1] < BUSINESS_CACHE_TTL:
        return cached[0]

    try:
        res = supabase.table('clients').select("*").ilike('phone_number', f'%{clean_num}%').execute()
        business = res.data[0] if res.data else None
        if business:
            business_cache[clean_num] = (business, time.time())
        return business
    except Exception as e:
        logger.error(f"Failed to load business config for {clean_num}: {e}")
        return None


# ==============================================================================
#                 SUPABASE BOT (BUTCHER & OTHERS - WHATSAPP TEXT)
# ==============================================================================

# Extra numbers allowed to run owner commands, on top of the owner_phone from the DB
ADMIN_OVERRIDE_NUMBERS = ["972547742596", "972525974462"]

def is_authorized_admin():
    """Security check: only the business owner (from the DB) or system admins pass."""
    current_business = getattr(g, 'business_config', None)
    if not current_business:
        return True  # no business context loaded — keep legacy behavior (check skipped)

    real_sender = request.values.get('From', '').replace("whatsapp:", "").replace("+", "")
    owner_phone = (current_business.get('owner_phone') or '').replace("+", "")
    if real_sender in [owner_phone] + ADMIN_OVERRIDE_NUMBERS:
        return True

    logger.warning(f"UNAUTHORIZED ADMIN ACTION BLOCKED FROM: {real_sender}")
    return False

ADMIN_DENIED_MSG = "❌ שגיאה: פעולה זו מורשית למנהל המערכת בלבד."

# --- TOOL 1: Save Orders ---
def save_order_supabase(name: str, order_details: str, method: str, address: str, timing: str, phone: str = "לא צוין"):
    """Saves a customer order to the database. NEVER use this for owner commands."""
    try:
        current_business = getattr(g, 'business_config', None)
        if not current_business: return "Error: No business context."

        owner_phone = current_business.get('owner_phone')
        bot_number = current_business.get('phone_number')
        owner_phone = ensure_whatsapp_prefix(owner_phone)

        client = get_dynamic_twilio_client(bot_number)
        real_sender = request.values.get('From', '')
        clean_phone = real_sender.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"

        body = (
            f"🚨 *הזמנה התקבלה / עודכנה!* 🚨\n\n"
            f"👤 *לקוח:* {name}\n"
            f"🥩 *פירוט:* {order_details}\n"
            f"🛍️ *איסוף/משלוח:* {method}\n"
            f"📍 *כתובת:* {address}\n"
            f"⏰ *שעה מבוקשת:* {timing}\n\n"
            f"💬 *לחץ כאן ליצירת קשר עם הלקוח:* \n{wa_link}"
        )

        if client and owner_phone:
             client.messages.create(from_=bot_number, to=owner_phone, body=body)

        if supabase:
            try:
                order_data = {
                    "business_phone": bot_number,
                    "client_name": name,
                    "client_phone": clean_phone,
                    "order_details": order_details,
                    "delivery_method": method,
                    "address": address,
                    "timing": timing,
                    "status": "new"
                }
                supabase.table('orders').insert(order_data).execute()
            except Exception as db_err:
                logger.error(f"Failed to save to DB: {db_err}")

        return "ההזמנה נשמרה בהצלחה והועברה לקצב."
    except Exception as e:
        return f"Error: {e}"

# --- TOOL 2: Mark Out of Stock ---
def mark_out_of_stock(product_name: str):
    """Marks a product as out of stock. Used when the owner says something ran out."""
    try:
        if not is_authorized_admin():
            return ADMIN_DENIED_MSG

        response = supabase.table('products').select('*').ilike('name', f'%{product_name}%').execute()

        if not response.data:
            return f"❌ לא מצאתי במערכת מוצר בשם '{product_name}'. ודא שהשם מדויק."

        target_product = response.data[0]
        current_name = target_product['name']
        product_id = target_product['id']

        if "אין במלאי" in current_name:
            return f"⚠️ המוצר '{current_name}' כבר מסומן כעת כחסר במלאי."

        new_name = f"{current_name} - אין במלאי"
        supabase.table('products').update({'name': new_name, 'in_stock': False}).eq('id', product_id).execute()

        return f"✅ עדכון אבטחה בוצע: '{new_name}' הוסר מהאתר בהצלחה."
    except Exception as e:
        logger.error(f"Stock Update Error: {str(e)}")
        return f"שגיאת מערכת בעדכון המלאי: {str(e)}"

# --- TOOL 3: Restock Product ---
def restock_product(product_name: str):
    """Returns a product to stock. Used when the owner asks to return an item to inventory."""
    try:
        if not is_authorized_admin():
            return ADMIN_DENIED_MSG

        response = supabase.table('products').select('*').ilike('name', f'%{product_name}%').execute()

        if not response.data:
            return f"❌ לא מצאתי מוצר בשם '{product_name}'."

        target = response.data[0]
        clean_name = target['name'].replace(" - אין במלאי", "").replace(" אין במלאי", "")

        supabase.table('products').update({'in_stock': True, 'name': clean_name}).eq('id', target['id']).execute()

        return f"✅ מושלם! '{clean_name}' חזר למלאי וזמין באתר."
    except Exception as e:
        return f"שגיאה: {str(e)}"

# --- TOOL 4: Update Price ---
def update_product_price(product_name: str, new_price: str):
    """Updates the price of a product. Used when the owner requests a price change."""
    try:
        if not is_authorized_admin():
            return ADMIN_DENIED_MSG

        clean_price = re.sub(r'[^\d.]', '', str(new_price))
        response = supabase.table('products').select('*').ilike('name', f'%{product_name}%').execute()

        if not response.data:
            return f"❌ לא מצאתי בשר בשם '{product_name}'."

        target = response.data[0]
        clean_name = target['name'].replace(" - אין במלאי", "").replace(" אין במלאי", "")

        supabase.table('products').update({'price': clean_price, 'in_stock': True, 'name': clean_name}).eq('id', target['id']).execute()

        return f"✅ מעולה! המחיר של '{clean_name}' שונה ל-{clean_price} ש״ח."
    except Exception as e:
        return f"שגיאה: {str(e)}"

# --- TOOL 5: Check Inventory Status ---
def check_out_of_stock_inventory():
    """Checks the database and returns a list of all products that are currently out of stock. Restricted to owner."""
    try:
        if not is_authorized_admin():
            return ADMIN_DENIED_MSG

        # Check by boolean or name
        res1 = supabase.table('products').select('name').eq('in_stock', False).execute()
        res2 = supabase.table('products').select('name').ilike('name', '%אין במלאי%').execute()

        missing = set()
        if res1.data:
            for item in res1.data: missing.add(item['name'].replace(' - אין במלאי', '').replace(' אין במלאי', ''))
        if res2.data:
            for item in res2.data: missing.add(item['name'].replace(' - אין במלאי', '').replace(' אין במלאי', ''))

        if not missing:
            return "✅ בדקתי במערכת וכרגע כל המוצרים שלנו נמצאים במלאי!"

        items_list = ", ".join(missing)
        return f"⚠️ המוצרים הבאים כרגע חסרים במלאי: {items_list}"
    except Exception:
        return "שגיאה בבדיקת המלאי מול בסיס הנתונים."


# ==============================================================================
#          TOOLS 6+7: OWNER PHONE ORDERS (WHATSAPP -> WEBSITE ORDER -> PRINTER)
# ==============================================================================
# The owner sends a free-text order over WhatsApp ("2 קילו אנטריקוט וקילו טחון,
# איסוף ב-14:00 על שם יוסי"). The AI extracts the details, validates them against
# the products table (real names + prices from the website), asks the owner about
# anything unclear, shows a preview, and only after explicit approval inserts a
# row into the 'orders' table in the EXACT format the website checkout uses -
# so the local printer agent picks it up and prints it like any web order.
# No printer-side changes are needed.

def _parse_price_value(price_raw):
    """The website stores prices as text ('₪ 199.00', '67₪', '50'). Extract a float."""
    if price_raw is None:
        return None
    if isinstance(price_raw, (int, float)):
        return float(price_raw)
    cleaned = re.sub(r'[^\d.]', '', str(price_raw))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _strip_stock_suffix(name):
    return str(name or "").replace(" - אין במלאי", "").replace(" אין במלאי", "").strip()


def _resolve_owner_order(items_json, customer_name, customer_phone, method, address, timing, notes=""):
    """Shared validation for preview/submit. Returns a dict:
    { ok, problems[], warnings[], preview, order_row } - order_row is ready to insert.
    Philosophy: only the ITEMS and the CUSTOMER NAME are mandatory. Everything else
    gets a sensible default (pickup / ASAP) so the owner isn't interrogated."""
    problems = []
    warnings = []

    # --- 1. Parse the items JSON ---
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else items_json
        if not isinstance(items, list) or not items:
            raise ValueError
    except Exception:
        return {"ok": False, "problems": ["items_json חייב להיות רשימת JSON של פריטים, למשל: "
                                          "[{\"name\": \"אנטריקוט\", \"qty\": 2}]"], "warnings": []}

    # --- 2. Delivery method: DEFAULT IS PICKUP. Never nag the owner about it. ---
    method_txt = str(method or "").strip()
    if "משלוח" in method_txt or "deliver" in method_txt.lower():
        method_norm = "delivery"
    else:
        method_norm = "pickup"

    if method_norm == "delivery" and not str(address or "").strip():
        problems.append("ההזמנה למשלוח - מה הכתובת?")

    # --- 3. Customer name is the ONE detail we do require ---
    if not str(customer_name or "").strip():
        problems.append("על איזה שם ההזמנה?")

    # --- 4. Resolve each item against the products table ---
    resolved = []
    for it in items:
        if not isinstance(it, dict):
            problems.append("אחד הפריטים לא בפורמט תקין.")
            continue

        raw_name = str(it.get("name", "")).strip()
        if not raw_name:
            problems.append("אחד הפריטים חסר שם.")
            continue

        # Quantity
        try:
            qty = float(it.get("qty", it.get("quantity")))
            if qty <= 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"כמה '{raw_name}'?")
            continue

        unit = str(it.get("unit", "")).strip() or "ק\"ג"
        item_note = str(it.get("note", "")).strip()
        manual_price = _parse_price_value(it.get("price"))

        display_name = raw_name
        unit_price = manual_price
        matched = None

        if supabase:
            try:
                res = supabase.table('products').select('*').ilike('name', f'%{raw_name}%').execute()
                candidates = res.data or []
            except Exception as e:
                logger.error(f"Owner order product lookup failed: {e}")
                candidates = []

            if len(candidates) == 1:
                matched = candidates[0]
            elif len(candidates) > 1:
                # Try an exact match (ignoring the out-of-stock suffix)
                exact = [c for c in candidates if _strip_stock_suffix(c.get('name')) == raw_name]
                if len(exact) == 1:
                    matched = exact[0]
                elif manual_price is None:
                    options = " / ".join(_strip_stock_suffix(c.get('name')) for c in candidates[:5])
                    problems.append(f"'{raw_name}' יכול להיות: {options}. איזה מהם?")
                    continue
            elif not candidates and manual_price is None:
                problems.append(f"לא מצאתי באתר מוצר בשם '{raw_name}'. מה השם המדויק, או מה המחיר "
                                f"כדי שארשום אותו כפריט חופשי?")
                continue

        if matched:
            display_name = _strip_stock_suffix(matched.get('name'))
            if unit_price is None:
                unit_price = _parse_price_value(matched.get('price'))
            if matched.get('in_stock') is False or "אין במלאי" in str(matched.get('name', '')):
                warnings.append(f"שים לב: '{display_name}' מסומן כרגע באתר כחסר במלאי.")

        if unit_price is None:
            problems.append(f"לא הצלחתי לקרוא את המחיר של '{display_name}' מהאתר. מה המחיר ל{unit}?")
            continue

        resolved.append({"name": display_name, "qty": qty, "unit": unit,
                         "unit_price": unit_price, "note": item_note})

    if not resolved and not problems:
        problems.append("לא זוהו פריטים בהזמנה.")

    if problems:
        return {"ok": False, "problems": problems, "warnings": warnings}

    # --- 5. Build the printer text in the SAME format as a website order ---
    total_price = 0.0
    order_details = ""
    preview_lines = []
    for i, r in enumerate(resolved):
        line_total = r["unit_price"] * r["qty"]
        total_price += line_total
        qty_str = f"{r['qty']:g}"
        line_name = f"{r['name']} ({r['note']})" if r["note"] else r["name"]
        order_details += f"{line_name} | {qty_str} {r['unit']} | {line_total:.2f} ש\"ח\n"
        preview_lines.append(f"{i+1}. {line_name} - {qty_str} {r['unit']} × ‏{r['unit_price']:.2f} = ‏{line_total:.2f} ש\"ח")

    order_details += f"------------------------------\nסה\"כ לתשלום: {total_price:.2f} ש\"ח"

    # --- 6. Address string, same convention as the web checkout (incl. notes) ---
    timing_txt = str(timing or "").strip()
    notes_txt = str(notes or "").strip()
    if method_norm == "delivery":
        db_address = str(address).strip()
        method_he = "משלוח 🚚"
    else:
        db_address = "איסוף עצמי"
        if timing_txt:
            db_address += f"\nשעת איסוף: {timing_txt}"
        method_he = "איסוף עצמי 🏬"
    if notes_txt:
        # Order-level notes land exactly where the website's 'הערה להזמנה' field goes
        db_address += f"\nהערות: {notes_txt}"

    current_business = getattr(g, 'business_config', None)
    bot_number = (current_business or {}).get('phone_number', '')
    clean_bot = str(bot_number).replace("whatsapp:", "").replace("+", "").strip()

    order_row = {
        "business_phone": clean_bot,
        "client_name": str(customer_name or "").strip() or "הזמנה טלפונית",
        "client_phone": str(customer_phone or "").strip(),
        "order_details": order_details.strip(),
        "delivery_method": method_norm,
        "address": db_address,
        "timing": timing_txt or "בהקדם",
        "status": "new"
    }

    preview = (
        f"🧾 *סיכום הזמנה לפני שליחה:*\n"
        f"👤 לקוח: {order_row['client_name']}\n"
        f"🛍️ {method_he}"
        + (f"\n📍 {db_address}" if method_norm == "delivery" else "")
        + (f"\n⏰ {timing_txt}" if timing_txt else "")
        + (f"\n📝 הערות: {notes_txt}" if notes_txt else "")
        + "\n\n"
        + "\n".join(preview_lines)
        + f"\n\n💰 *סה\"כ: {total_price:.2f} ש\"ח*"
    )
    if warnings:
        preview += "\n\n" + "\n".join(f"⚠️ {w}" for w in warnings)

    return {"ok": True, "problems": [], "warnings": warnings, "preview": preview, "order_row": order_row}


# --- TOOL 6: Preview Owner Order (validation only, does NOT save) ---
def preview_owner_order(items_json: str, customer_name: str = "", customer_phone: str = "",
                        method: str = "", address: str = "", timing: str = "", notes: str = ""):
    """Validates an order the OWNER dictated over WhatsApp against the website products,
    and returns a priced summary for approval. Does NOT save anything.
    items_json: JSON list like [{"name": "אנטריקוט", "qty": 2}]. Optional per item:
    "unit" ("ק\"ג" default, or "יח'"), "price" (manual price for items not on the site),
    "note" (prep request like "חתוך קטן"). notes: order-level note (goes to the printed
    הערות field). Only items + customer_name are required; method defaults to pickup.
    If the response starts with ❓ those are QUESTIONS to relay to the owner, not an error."""
    if not is_authorized_admin():
        return ADMIN_DENIED_MSG

    logger.info(f"preview_owner_order: items={items_json} | name={customer_name} | method={method} | "
                f"addr={address} | time={timing} | notes={notes}")

    result = _resolve_owner_order(items_json, customer_name, customer_phone, method, address, timing, notes)
    if not result["ok"]:
        txt = "❓ חסרים לי פרטים. שאל את הבעלים:\n" + "\n".join(f"• {p}" for p in result["problems"])
        if result["warnings"]:
            txt += "\n" + "\n".join(f"⚠️ {w}" for w in result["warnings"])
        txt += "\n\n[הנחיה למודל: אלו שאלות רגילות לבעלים - העבר אותן כפי שהן. זו איננה תקלה, אל תגיד 'משהו השתבש'.]"
        return txt

    return result["preview"] + "\n\nאם הכל נכון, אשר לי ואשלח את ההזמנה להדפסה. 🖨️"


# --- TOOL 7: Submit Owner Order (saves to the site -> auto-printed) ---
def submit_owner_order(items_json: str, customer_name: str = "", customer_phone: str = "",
                       method: str = "", address: str = "", timing: str = "", notes: str = ""):
    """FINAL step: saves the owner's dictated order into the website orders system,
    which automatically sends it to the shop printer. Call ONLY after the owner
    explicitly approved the preview from preview_owner_order, with the SAME details."""
    if not is_authorized_admin():
        return ADMIN_DENIED_MSG

    if not supabase:
        return "❌ אין חיבור לבסיס הנתונים - אי אפשר לשמור את ההזמנה."

    logger.info(f"submit_owner_order: items={items_json} | name={customer_name} | method={method} | "
                f"addr={address} | time={timing} | notes={notes}")

    result = _resolve_owner_order(items_json, customer_name, customer_phone, method, address, timing, notes)
    if not result["ok"]:
        return ("❓ אי אפשר לשלוח עדיין, שאל את הבעלים:\n" + "\n".join(f"• {p}" for p in result["problems"])
                + "\n\n[הנחיה למודל: העבר את השאלות לבעלים כפי שהן. זו איננה תקלה.]")

    try:
        supabase.table('orders').insert(result["order_row"]).execute()
        logger.info(f"Owner phone-order saved & queued for printing: {result['order_row']['client_name']}")
        total_line = result["order_row"]["order_details"].splitlines()[-1]
        return f"✅ ההזמנה נקלטה באתר ונשלחה להדפסה בחנות! 🖨️\n{total_line}"
    except Exception as e:
        logger.error(f"Failed to save owner phone-order: {e}")
        return f"❌ שגיאה בשמירת ההזמנה: {e}"


class SupabaseAgent:
    MAX_ACTIVE_CHATS = 500  # prevents unbounded RAM growth on a long-running server

    # Generic owner-mode prompt. Used as a FALLBACK for any business that has no
    # 'owner_system_instruction' value in its clients row - so the system works
    # out of the box for every tenant, and can be customized per business in
    # Supabase without touching code (same pattern as 'system_instruction').
    DEFAULT_OWNER_SYSTEM_PROMPT = (
        "אתה העוזר התפעולי החכם של העסק. אתה משוחח כרגע עם *בעל העסק* (או מנהל מערכת) - לא עם לקוח!\n"
        "חוק ברזל: ענה אך ורק בעברית (אנגלית מותרת רק לשמות פונקציות טכניות).\n"
        "לעולם אל תפנה את הבעלים לאתר ואל תשלח לו קישורים - זה האתר שלו.\n\n"
        "היכולות שלך עבור הבעלים:\n\n"
        "1. *קליטת הזמנה טלפונית והדפסתה בעסק (הכי חשוב):*\n"
        "כשהבעלים מבקש לרשום, להזמין או להדפיס הזמנה - שאל 'בכיף! מה ההזמנה?' אם אין פרטים.\n"
        "חובה רק שני דברים: *הפריטים עם הכמויות* + *שם הלקוח*. שום דבר אחר לא חובה!\n"
        "ברירות מחדל אוטומטיות: איסוף עצמי, בהקדם. אל תשאל על משלוח/איסוף, שעה, טלפון או כתובת - "
        "אלא אם הבעלים אמר בעצמו 'משלוח', ואז חסרה רק הכתובת.\n"
        "בקשות מיוחדות ('חתוך קטן', 'טחון פעמיים', 'ואקום') - אל תשאל עליהן! צרף אותן ב-\"note\" של הפריט, "
        "או בפרמטר notes אם זה על כל ההזמנה. הן יודפסו בשדה ההערות.\n"
        "- ברגע שיש פריטים ושם - קרא ל-preview_owner_order. הפרמטר items_json הוא רשימת JSON, למשל: "
        "[{\"name\": \"אנטריקוט\", \"qty\": 2, \"note\": \"חתוך קטן\"}]. אפשר גם \"unit\" (ברירת מחדל ק\"ג, או יח') "
        "ו-\"price\" (מחיר ידני לפריט שלא קיים באתר).\n"
        "- בכל קריאה שלח תמיד את *כל* רשימת הפריטים שנאספה מתחילת השיחה, לא רק את החדשים.\n"
        "- אם הכלי מחזיר טקסט שמתחיל ב-❓ - אלו שאלות הבהרה רגילות (למשל: 'לבבות עוף או עגל?'). "
        "העבר אותן לבעלים כמו שהן. זו *לא* תקלה - לעולם אל תגיד 'משהו השתבש' או 'יש תקלה במערכת'.\n"
        "- אם הבעלים נותן מידע מיותר או סותר - קבל את הגרסה האחרונה בשקט, בלי להתווכח ובלי לחקור אותו.\n"
        "- כשחוזר סיכום (🧾) - הצג אותו לבעלים ושאל אם לאשר.\n"
        "- רק אחרי אישור מפורש ('כן', 'אשר', 'שלח') - קרא ל-submit_owner_order עם אותם פרטים בדיוק. "
        "ההזמנה תיקלט במערכת ותודפס אוטומטית בעסק.\n"
        "אסור לך לאשר במקום הבעלים, לנחש פרטים חסרים, או לשלוח הזמנה בלי אישור מפורש.\n\n"
        "2. *ניהול מלאי ומחירים:* יש לך כלים להוריד מהמלאי (mark_out_of_stock), להחזיר למלאי (restock_product), "
        "לעדכן מחיר (update_product_price), ולבדוק מה חסר (check_out_of_stock_inventory). "
        "הבעלים מדבר טבעי ('נגמר העוף', 'תחזיר את האנטריקוט') - פעל מיד, אל תגיד שאתה לא מסוגל.\n\n"
        "3. *פתק חופשי למדפסת:* אם הבעלים רוצה להדפיס טקסט חופשי שאינו הזמנה, הסבר לו שיתחיל הודעה במילה "
        "\"הדפס\" ואחריה הטקסט, והיא תודפס ישירות בעסק.\n\n"
        "סגנון: קצר, ישראלי, תכל'ס ('סגור', 'בכיף'). בלי חפירות ובלי פסקאות ארוכות."
    )

    def __init__(self):
        self.chats = {}

    def get_response(self, user_phone, msg, config):
        chat_id = f"{config['phone_number']}_{user_phone}"

        if chat_id not in self.chats or msg.lower() == "reset":
            # --- WHO ARE WE TALKING TO? ---
            clean_user = str(user_phone).replace("whatsapp:", "").replace("+", "").strip()
            owner_phone = str(config.get('owner_phone') or '').replace("whatsapp:", "").replace("+", "").strip()
            is_owner_chat = clean_user in (([owner_phone] if owner_phone else []) + ADMIN_OVERRIDE_NUMBERS)
            logger.info(f"OWNER CHAT CHECK: sender={clean_user} | owner_in_db={owner_phone} | is_owner={is_owner_chat}")

            # --- TIME (ISRAEL) ---
            if ISRAEL_TZ:
                israel_time = datetime.datetime.now(ISRAEL_TZ)
            else:
                israel_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
            time_str = israel_time.strftime("%d/%m/%Y %H:%M")

            if is_owner_chat:
                # ==========================================================
                # OWNER MODE: per-business prompt from the clients row
                # ('owner_system_instruction'), or the generic default.
                # The customer-facing script from 'system_instruction' is
                # NOT loaded at all, so its "never collect order details /
                # always send to the website" rules can't leak in here.
                # ==========================================================
                sys_instruct = str(config.get('owner_system_instruction') or '').strip() or self.DEFAULT_OWNER_SYSTEM_PROMPT
                sys_instruct += f"\n\n[מידע מערכת: התאריך והשעה כרגע בישראל: {time_str}.]"
                tools_list = [save_order_supabase, mark_out_of_stock, restock_product, update_product_price,
                              check_out_of_stock_inventory, preview_owner_order, submit_owner_order]
            else:
                # ==========================================================
                # CUSTOMER MODE: exactly the original behavior, untouched.
                # ==========================================================
                sys_instruct = config.get('system_instruction', 'You are a helpful assistant.')
                sys_instruct += f"\n\n[מידע מערכת חסוי: התאריך והשעה כרגע בישראל: {time_str}.]"

                # --- THE GOD-MODE OVERRIDE ---
                sys_instruct += "\n[הוראת מערכת קריטית: מותר לך ואתה מסוגל לעדכן הזמנות קיימות! אם לקוח מבקש לשנות הזמנה שכבר ביצע באותה שיחה, פשוט אסוף את הפרטים החדשים והפעל שוב את הפונקציה save_order_supabase עם כל המידע המעודכן. לעולם אל תגיד ללקוח שאינך יכול לשנות הזמנה.]"

                # --- INVENTORY MANAGEMENT LAYER (OWNER INSTRUCTION) ---
                sys_instruct += "\n[ניהול מלאי: אתה מנהל גם את החנות מאחורי הקלעים עבור הבעלים. יש לך כלים להוריד מהמלאי (mark_out_of_stock), להחזיר למלאי (restock_product), לעדכן מחירים (update_product_price), ולבדוק מה חסר כרגע (check_out_of_stock_inventory). הבעלים יכול פשוט לדבר איתך באופן טבעי (למשל: 'נגמר העוף' או 'תחזיר את האנטריקוט'). השתמש בכלים האלו מיד כשהוא מבקש, ואל תגיד שאתה לא מסוגל. המערכת תחסום לקוחות רגילים מלהשתמש בזה (יש חסימת אבטחה בקוד), אז אתה יכול להפעיל את הכלים בלי לחשוש שמדובר בלקוח.]"

                tools_list = [save_order_supabase, mark_out_of_stock, restock_product, update_product_price, check_out_of_stock_inventory]

            # Evict the oldest session if we hit the cap
            if len(self.chats) >= self.MAX_ACTIVE_CHATS:
                oldest = next(iter(self.chats))
                del self.chats[oldest]

            model = genai.GenerativeModel('gemini-2.5-flash', tools=tools_list, system_instruction=sys_instruct)
            self.chats[chat_id] = model.start_chat(enable_automatic_function_calling=True)

        try:
            raw_reply = self.chats[chat_id].send_message(msg).text
            # Strips out any internal English reasoning before sending to WhatsApp
            clean_reply = re.sub(r'(?is)THOUGHT:.*?(?:\n\n|\n(?=[א-ת]))', '', raw_reply).strip()

            if not clean_reply and raw_reply:
                clean_reply = raw_reply

            return clean_reply

        except Exception as e:
            if chat_id in self.chats:
                del self.chats[chat_id]
            return f"🤖 שגיאת מודל AI:\n{str(e)}"

supabase_agent = SupabaseAgent()

def handle_supabase_flow(sender, msg, bot_number):
    if not supabase:
        resp = MessagingResponse()
        resp.message("❌ שגיאה קריטית: הבוט עיוור. חסרים משתני הסביבה SUPABASE_URL ו-SUPABASE_KEY בשרת Render שלכם!")
        return str(resp)

    business = get_business_from_supabase(bot_number)
    if not business:
        clean_num = bot_number.replace("whatsapp:", "").replace("+", "").strip()
        resp = MessagingResponse()
        resp.message(f"❌ לא מצאתי התאמה למספר הבוט: {clean_num}")
        return str(resp)

    g.business_config = business

    # Send directly to AI (AI will handle tool execution based on conversational context)
    reply = supabase_agent.get_response(sender, msg, business)
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# ==============================================================================
#                 PERSONAL NOTE PRINTING (OWNER -> PRINTER)
# ==============================================================================
# The owner (or a system admin) can send the bot a free-text WhatsApp message
# that starts with "הדפס" and the text will go straight to the shop printer.
# This is intercepted BEFORE the AI agent and BEFORE any Twilio template logic,
# so the existing order flow and the working template are NOT touched.

PRINT_NOTE_PREFIXES = ["הדפס:", "הדפס ", "הדפס\n", "print:", "Print:", "PRINT:"]

def try_handle_print_note(sender, msg, bot_number):
    """If this is an owner 'הדפס ...' command, queue it for the local printer.
    Returns a TwiML response string, or None to continue with the normal flow."""

    note_text = None
    if msg.strip() == "הדפס":
        note_text = ""  # command with no text -> we reply with usage help below
    else:
        for prefix in PRINT_NOTE_PREFIXES:
            if msg.startswith(prefix):
                note_text = msg[len(prefix):].strip()
                break

    if note_text is None:
        return None  # not a print command -> normal AI flow, untouched

    # --- SECURITY: only the business owner or system admins can print notes ---
    clean_sender = str(sender).replace("whatsapp:", "").replace("+", "").strip()
    business = get_business_from_supabase(bot_number)
    owner_phone = ""
    if business:
        owner_phone = (business.get('owner_phone') or '').replace("whatsapp:", "").replace("+", "").strip()

    allowed = ([owner_phone] if owner_phone else []) + ADMIN_OVERRIDE_NUMBERS
    if clean_sender not in allowed:
        # A regular customer typed something starting with "הדפס" -
        # silently fall through to the normal AI flow, don't reveal the feature.
        logger.warning(f"Print-note command ignored from non-admin: {clean_sender}")
        return None

    resp = MessagingResponse()

    if not note_text:
        resp.message("✍️ כדי להדפיס פתק בחנות, כתוב:\nהדפס <הטקסט שלך>")
        return str(resp)

    if not supabase:
        resp.message("❌ שגיאה: אין חיבור לבסיס הנתונים, אי אפשר לשלוח למדפסת.")
        return str(resp)

    try:
        clean_bot = str(bot_number).replace("whatsapp:", "").replace("+", "").strip()
        note_row = {
            "business_phone": clean_bot,
            "client_name": "הודעה אישית",
            "client_phone": clean_sender,
            "order_details": note_text,
            "delivery_method": "note",  # the printer agent uses this marker for the note layout
            "address": "",
            "timing": "",
            "status": "new"
        }
        supabase.table('orders').insert(note_row).execute()
        logger.info(f"Personal note queued for printing by {clean_sender}")
        resp.message("🖨️ ההודעה נשלחה למדפסת!")
    except Exception as e:
        logger.error(f"Failed to queue print note: {e}")
        resp.message(f"❌ שגיאה בשליחה למדפסת: {e}")

    return str(resp)

# ==============================================================================
#                 MAIN ROUTER (WHATSAPP TEXT)
# ==============================================================================

@app.route("/whatsapp", methods=['POST'])
def main_router():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    bot_number = request.values.get('To', '')

    # Personal note printing (owner only) - checked first, does NOT touch the AI/template flow
    note_response = try_handle_print_note(sender, incoming_msg, bot_number)
    if note_response:
        return note_response

    return handle_supabase_flow(sender, incoming_msg, bot_number)


# --- SECURITY: The Bouncer's Memory ---
ip_tracker = {}
# ==============================================================================
#                 WEBSITE CHECKOUT API (SECURED)
# ==============================================================================

@app.route("/api/web-order", methods=['POST', 'OPTIONS'])
def web_order():
    cors_headers = {"Access-Control-Allow-Origin": "*"}

    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type, X-API-KEY"
        }
        return ('', 204, headers)

    try:
        # --- SECURITY LAYER 1: THE SECRET HANDSHAKE ---
        client_key = request.headers.get('X-API-KEY')
        if client_key != WEB_ORDER_API_KEY:
            logger.warning(f"BLOCKED: Unauthorized access attempt from {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401, cors_headers

        # --- SECURITY LAYER 2: THE BOUNCER (Rate Limiting) ---
        client_ip = request.remote_addr
        current_time = time.time()

        # Purge stale entries so the tracker doesn't grow forever
        if len(ip_tracker) > 100:
            for ip in [ip for ip, (_, t) in ip_tracker.items() if current_time - t > 300]:
                del ip_tracker[ip]

        if client_ip in ip_tracker:
            requests_made, first_request_time = ip_tracker[client_ip]
            if current_time - first_request_time < 300:
                if requests_made >= 3:
                    logger.warning(f"BLOCKED: Spam detected from IP {client_ip}")
                    return jsonify({"error": "Too many requests. Wait 5 minutes."}), 429, cors_headers
                ip_tracker[client_ip] = (requests_made + 1, first_request_time)
            else:
                ip_tracker[client_ip] = (1, current_time)
        else:
            ip_tracker[client_ip] = (1, current_time)

        # --- PROCESS THE REAL ORDER ---
        data = request.get_json()
        customer = data.get('customer', {})
        items = data.get('items', [])
        total_price = data.get('total', 0)

        method_text = "משלוח 🚚" if data.get('deliveryMethod') == "delivery" else "איסוף עצמי 🏬"

        # Pull new variables from frontend
        notes = customer.get('notes', '')
        pickup_time = customer.get('pickupTime', '')

        # --- BUILD ADDRESS STRINGS (For both WhatsApp and Printer) ---
        whatsapp_address_block = ""
        database_address_string = ""

        if data.get('deliveryMethod') == "delivery":
            base_address = f"{customer.get('city', '')}, {customer.get('street', '')} {customer.get('houseNumber', '')}".strip()
            whatsapp_address_block += f"כתובת: {base_address}\n"
            database_address_string += base_address

            # Add Floor if it exists
            if customer.get('floor'):
                whatsapp_address_block += f"קומה/דירה: {customer.get('floor')}\n"
                database_address_string += f"\nקומה/דירה: {customer.get('floor')}"

            # Add Notes if they exist
            if notes:
                whatsapp_address_block += f"הערות: {notes}\n"
                database_address_string += f"\nהערות: {notes}"
        else:
            # Pickup logic - inject time and notes here so the Twilio template doesn't break!
            whatsapp_address_block = "איסוף עצמי"
            database_address_string = "איסוף עצמי"

            if pickup_time:
                whatsapp_address_block += f"\nשעת איסוף: {pickup_time}"
                database_address_string += f"\nשעת איסוף: {pickup_time}"
            if notes:
                whatsapp_address_block += f"\nהערות: {notes}"
                database_address_string += f"\nהערות: {notes}"

        # --- BUILD PRINTER TEXT (Safe RTL format) ---
        order_details_for_db = ""

        for i, item in enumerate(items):
            p = item.get('product', {})
            qty = item.get('quantity', 0)
            price = p.get('price', 0) * qty

            # Printer format
            order_details_for_db += f"{p.get('name')} | {qty} ק\"ג | {price:.2f} ש\"ח\n"

        # Add total to the printer output
        order_details_for_db += f"------------------------------\nסה\"כ לתשלום: {total_price:.2f} ש\"ח"

        clean_phone = customer.get('phone', '')
        if clean_phone.startswith('0'):
            clean_phone = '972' + clean_phone[1:]

        # -----------------------------------------------------
        # SENDING TO TWILIO AND SAVING TO SUPABASE
        # -----------------------------------------------------
        bot_number = "whatsapp:+97223723780"

        business = get_business_from_supabase(bot_number)
        if business and business.get('owner_phone'):
            target_phone = ensure_whatsapp_prefix(business.get('owner_phone'))
        else:
            target_phone = "whatsapp:+972547742596"
            logger.warning("Could not pull owner_phone from Supabase, using fallback number.")

        client = get_dynamic_twilio_client(bot_number)

        if client:
            # --- 1. PREPARE THE DYNAMIC VARIABLES FOR THE TEMPLATE ---
            items_list_whatsapp = ""
            for i, item in enumerate(items):
                p = item.get('product', {})
                qty = item.get('quantity', 0)
                price = p.get('price', 0) * qty
                items_list_whatsapp += f"{i+1}. {p.get('name')} - {qty} ק\"ג (₪{price:.2f})\n"

            template_variables = {
                # Add 'or' fallback to prevent empty "" strings from crashing the API
                "1": customer.get('name') or 'לקוח',
                "2": customer.get('phone') or 'לא צוין',
                "3": method_text,

                # The notes and pickup time are safely packed into the 'Address' slot (slot 4)
                # so the existing Twilio template will not break.
                "4": (whatsapp_address_block.strip() if whatsapp_address_block.strip() else "איסוף עצמי").replace('\n', ', '),

                # Replace any internal newlines in the items list with a divider symbol
                "5": items_list_whatsapp.strip().replace('\n', ' | '),

                "6": f"{total_price:.2f}",
                "7": clean_phone or "000000000"
            }

            # 1. Send WhatsApp Message via Content API
            try:
                client.messages.create(
                    from_=bot_number,
                    to=target_phone,
                    content_sid="HX646014f238db357b5f598f8c5c129d30",
                    content_variables=json.dumps(template_variables)
                )
                logger.info("Web order template message sent successfully.")
            except Exception as twilio_err:
                logger.error(f"Failed to send Twilio template: {twilio_err}")

            # 2. Save to Supabase for the Local Printer Agent
            if supabase:
                try:
                    order_data = {
                        "business_phone": bot_number.replace("whatsapp:", "").replace("+", ""),
                        "client_name": customer.get('name', 'לקוח אתר'),
                        "client_phone": customer.get('phone', ''),
                        "order_details": order_details_for_db.strip(),
                        "delivery_method": data.get('deliveryMethod', 'pickup'),
                        "address": database_address_string.strip(),  # includes time & notes
                        "timing": "בהקדם",
                        "status": "new"
                    }
                    supabase.table('orders').insert(order_data).execute()
                    logger.info("Web order successfully saved to Supabase for printing.")
                except Exception as db_err:
                    logger.error(f"Failed to save WEB ORDER to DB: {db_err}")

        else:
            logger.error("Could not send website order: No Twilio client found for bot number.")

        return jsonify({"status": "success"}), 200, cors_headers

    except Exception as e:
        logger.error(f"Web Order Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500, cors_headers

@app.route("/", methods=['GET'])
def health_check():
    return "Hybrid Voice & Text System Active 🚀", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)