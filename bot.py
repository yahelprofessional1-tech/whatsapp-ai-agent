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


class SupabaseAgent:
    MAX_ACTIVE_CHATS = 500  # prevents unbounded RAM growth on a long-running server

    def __init__(self):
        self.chats = {}

    def get_response(self, user_phone, msg, config):
        chat_id = f"{config['phone_number']}_{user_phone}"

        if chat_id not in self.chats or msg.lower() == "reset":
            sys_instruct = config.get('system_instruction', 'You are a helpful assistant.')

            # --- 1. TIME INJECTION (ISRAEL TIME) ---
            if ISRAEL_TZ:
                israel_time = datetime.datetime.now(ISRAEL_TZ)
            else:
                israel_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
            time_str = israel_time.strftime("%d/%m/%Y %H:%M")
            sys_instruct += f"\n\n[מידע מערכת חסוי: התאריך והשעה כרגע בישראל: {time_str}.]"

            # --- 2. THE GOD-MODE OVERRIDE ---
            sys_instruct += "\n[הוראת מערכת קריטית: מותר לך ואתה מסוגל לעדכן הזמנות קיימות! אם לקוח מבקש לשנות הזמנה שכבר ביצע באותה שיחה, פשוט אסוף את הפרטים החדשים והפעל שוב את הפונקציה save_order_supabase עם כל המידע המעודכן. לעולם אל תגיד ללקוח שאינך יכול לשנות הזמנה.]"

            # --- 3. INVENTORY MANAGEMENT LAYER (OWNER INSTRUCTION) ---
            sys_instruct += "\n[ניהול מלאי: אתה מנהל גם את החנות מאחורי הקלעים עבור הבעלים. יש לך כלים להוריד מהמלאי (mark_out_of_stock), להחזיר למלאי (restock_product), לעדכן מחירים (update_product_price), ולבדוק מה חסר כרגע (check_out_of_stock_inventory). הבעלים יכול פשוט לדבר איתך באופן טבעי (למשל: 'נגמר העוף' או 'תחזיר את האנטריקוט'). השתמש בכלים האלו מיד כשהוא מבקש, ואל תגיד שאתה לא מסוגל. המערכת תחסום לקוחות רגילים מלהשתמש בזה (יש חסימת אבטחה בקוד), אז אתה יכול להפעיל את הכלים בלי לחשוש שמדובר בלקוח.]"

            # Evict the oldest session if we hit the cap
            if len(self.chats) >= self.MAX_ACTIVE_CHATS:
                oldest = next(iter(self.chats))
                del self.chats[oldest]

            tools_list = [save_order_supabase, mark_out_of_stock, restock_product, update_product_price, check_out_of_stock_inventory]
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
#                 MAIN ROUTER (WHATSAPP TEXT)
# ==============================================================================

@app.route("/whatsapp", methods=['POST'])
def main_router():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    bot_number = request.values.get('To', '')

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
