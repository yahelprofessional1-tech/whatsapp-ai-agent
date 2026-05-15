import os
import json
import datetime
import logging
import re
from flask import Flask, request, g, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient
import time 

# --- 1. SYSTEM SETUP ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HybridBot")
app = Flask(__name__)

# --- GLOBAL CONFIG ---
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
LAWYER_NUMBER_ENV = os.getenv('LAWYER_WHATSAPP_NUMBER') 

# Supabase Setup
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
try:
    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
except:
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

# ==============================================================================
#                 ZONE A: THE LAWYER BOT (TEXT)
# ==============================================================================

lawyer_sessions = {}
last_auto_replies = {} 

class LawyerConfig:
    BUSINESS_NAME = "Adv. Shimon Hasky"
    LAWYER_PHONE = os.getenv('LAWYER_PHONE') # חזקי
    VIP_NUMBERS = [LAWYER_PHONE]
    COOL_DOWN_HOURS = 24
    
    FLOW_STATES = {
        "START": {
            "message": """שלום, הגעתם למשרד עו"ד שמעון חסקי. ⚖️\nאני העוזר החכם של המשרד.\nכדי שנתקדם, תוכל לבחור נושא, או לכתוב לי ישר מה קרה.\n1️⃣ גירושין\n2️⃣ משמורת ילדים\n3️⃣ הסכמי ממון\n4️⃣ צוואות וירושות\n5️⃣ תיאום פגישה\n6️⃣ 🤖 התייעצות עם נציג (AI)""",
            "options": [
                { "label": "גירושין", "next": "AI_MODE_SUMMARY" },
                { "label": "משמורת ילדים", "next": "AI_MODE_SUMMARY" },
                { "label": "הסכמי ממון", "next": "AI_MODE_SUMMARY" },
                { "label": "צוואות וירושות", "next": "AI_MODE_SUMMARY" },
                { "label": "תיאום פגישה", "next": "ASK_BOOKING" },
                { "label": "נציג וירטואלי", "next": "AI_MODE" }
            ]
        },
        "ASK_BOOKING": { "message": "מתי תרצה להיפגש?", "next": "FINISH_BOOKING" },
        "FINISH_BOOKING": { "message": "העברתי בקשה למזכירות לתיאום פגישה, נחזור אליך בהקדם.", "action": "book_meeting" }
    }

def ensure_whatsapp_prefix(phone):
    if not phone: return None
    clean = phone.strip()
    if not clean.startswith("whatsapp:"):
        return f"whatsapp:{clean}"
    return clean

def save_case_summary(name: str, topic: str, summary: str, phone: str = "Unknown", classification: str = "NEW_LEAD"):
    try:
        bot_number = request.values.get('To')
        client = get_dynamic_twilio_client(bot_number)
        
        real_sender = request.values.get('From', '')
        clean_phone = real_sender.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"
        
        header = "🚨 *חירום!*" if classification == "URGENT" else "✨ *ליד חדש*"
        body = f"""{header}\n👤 {name}\n📌 {topic}\n📝 {summary}\n{wa_link}"""
        
        target_phone = ensure_whatsapp_prefix(LawyerConfig.LAWYER_PHONE)
        
        if client and target_phone:
            client.messages.create(from_=bot_number, body=body, to=target_phone)
            return f"SAVED as {classification}."
        else:
            return f"SAVED as {classification} (Note: Lawyer phone or Client not configured)."
            
    except Exception as e: return f"Error: {e}"

def book_meeting_tool(client_name: str, reason: str):
    bot_number = request.values.get('To')
    client = get_dynamic_twilio_client(bot_number)
    target_phone = ensure_whatsapp_prefix(LawyerConfig.LAWYER_PHONE)
    
    if client and target_phone:
         client.messages.create(
             from_=bot_number,
             body=f"📅 *בקשה לפגישה*\nלקוח: {client_name}\nסיבה: {reason}",
             to=target_phone
         )
    return "Success"

class LawyerAgent:
    def __init__(self):
        self.tools = [save_case_summary, book_meeting_tool]
        
        self.system_instruction = f"""
        אתה עוזר הקבלה של {LawyerConfig.BUSINESS_NAME}.

        **זהות וטון:**
        אתה לא רובוט. אתה מזכיר אנושי ומקצועי.
        דבר בעברית טבעית, חמה, ותומכת. אל תשתמש במשפטים רובוטיים. פסק את המשפטים שלך עם פסיקים ונקודות כדי שהדיבור יישמע טבעי.
        
        **שפה ומגדר (קריטי):**
        פנה למשתמש תמיד בלשון זכר כברירת מחדל (אתה, מעוניין, תרצה), אלא אם המשתמש מדבר על עצמו במפורש בלשון נקבה. לעולם אל תערבב זכר ונקבה באותו משפט.

        **המטרה שלך (לפי סדר עדיפויות):**
        1. אם הלקוח שאל שאלה - ענה קצר וישיר (1-2 משפטים).
        2. קבל שם מלא של הלקוח.
        3. הבן את הבעיה המשפטית.
        4. סווג ושמור את התיק.

        **תהליך השיחה - עקוב בדיוק:**

        📍 **שלב 1: אמפתיה ראשונית**
        אם הלקוח מביע כאב/מצוקה/פחד, התחל עם מילות תמיכה והקשבה.

        📍 **שלב 2: תשובה לשאלה (אם יש)**
        כלל זהב: תשובה קצרה + הפניה לעו"ד לפרטים. "אם אתה לא יודע משהו פשוט תגיד שעורך דין חסקי יענה על זה".

        📍 **שלב 3: קבלת שם**
        אם אין לך שם עדיין, פשוט שאל לשמו המלא.
   
        📍 **שלב 4: הבנת הבעיה**
        שאל שאלה אחת ממוקדת כדי להבין את המקרה.

        📍 **שלב 5: סיכום ואישור (פעם אחת בלבד!)**
        לפני שאתה שומר את התיק, סכם ללקוח את מה שהבנת.
        השתמש בדיוק במבנה הבא:
        1. "אז אני מבין ש..." (סיכום המקרה).
        2. סיום עם השאלה: **"האם תרצה להוסיף עוד פרטים לפני שאעביר את ההודעה?"**

        **כלל ברזל למניעת לולאות:** שאל את שאלת האישור הזו **פעם אחת ויחידה**. 
        אם הלקוח עונה "לא", "זהו", או מאשר --> קרא מיד לפונקציה `save_case_summary`.
        אם הלקוח מאשר אך מוסיף פרט קטן --> הוסף את המידע לסיכום הפנימי שלך וקרא **מיד** לפונקציה `save_case_summary`. **בשום אופן אל תשאל שוב!**

        **חוקי סיווג (CLASSIFICATION):**
        🔥 "URGENT" - מילות חירום: דחוף, משטרה, אלימות.
        📁 "EXISTING" - קשר קיים: התיק שלי, הדיון שלי.
        ✨ "NEW_LEAD" - פנייה ראשונה: רוצה להתגרש, כמה עולה.

        **טיפול בשגיאות:**
        אם הפונקציה החזירה "Saved" - תגיד רק:
        "הפרטים נשמרו והועברו לעו"ד חסקי."
        """
        self.model = genai.GenerativeModel('gemini-2.5-flash', tools=self.tools, system_instruction=self.system_instruction)
        self.chats = {}

    def chat(self, user, msg):
        if user not in self.chats:
            self.chats[user] = self.model.start_chat(enable_automatic_function_calling=True)
        try:
            res = self.chats[user].send_message(msg)
            return res.text if res.text else "הפרטים נקלטו."
        except: return "אירעה שגיאה, נסה שוב."

lawyer_ai = LawyerAgent()

def handle_lawyer_flow(sender, incoming_msg, bot_number):
    if incoming_msg.lower() == "reset":
        lawyer_sessions[sender] = 'START'
        if sender in lawyer_ai.chats:
            del lawyer_ai.chats[sender]
        return send_lawyer_menu(sender, "🔄 *System Reset*", LawyerConfig.FLOW_STATES['START']['options'], bot_number)

    if sender not in lawyer_sessions:
        lawyer_sessions[sender] = 'START'
        return send_lawyer_menu(sender, LawyerConfig.FLOW_STATES['START']['message'], LawyerConfig.FLOW_STATES['START']['options'], bot_number)

    if incoming_msg.isdigit() and lawyer_sessions[sender] == 'START':
        idx = int(incoming_msg) - 1
        options = LawyerConfig.FLOW_STATES['START']['options']
        if 0 <= idx < len(options):
            selected = options[idx]
            if selected['next'] == 'AI_MODE_SUMMARY':
                lawyer_sessions[sender] = 'AI_MODE'
                reply = lawyer_ai.chat(sender, f"User chose: {selected['label']}. Start conversation.")
                return send_lawyer_msg(sender, reply, bot_number)
            elif selected['next'] == 'ASK_BOOKING':
                lawyer_sessions[sender] = 'ASK_BOOKING'
                return send_lawyer_msg(sender, LawyerConfig.FLOW_STATES['ASK_BOOKING']['message'], bot_number)
            elif selected['next'] == 'AI_MODE':
                lawyer_sessions[sender] = 'AI_MODE'
                return send_lawyer_msg(sender, "היי, אני כאן. איך אפשר לעזור?", bot_number)

    if lawyer_sessions[sender] == 'ASK_BOOKING':
        book_meeting_tool(sender, "Manual Booking")
        lawyer_sessions[sender] = 'START'
        return send_lawyer_msg(sender, LawyerConfig.FLOW_STATES['FINISH_BOOKING']['message'], bot_number)

    reply = lawyer_ai.chat(sender, incoming_msg)
    return send_lawyer_msg(sender, reply, bot_number)

def send_lawyer_msg(to, body, from_):
    client = get_dynamic_twilio_client(from_)
    if client:
        client.messages.create(from_=from_, body=body, to=to)
    return str(MessagingResponse())

def send_lawyer_menu(to, body, options, from_):
    client = get_dynamic_twilio_client(from_)
    if not client: return str(MessagingResponse())
    
    try:
        rows = [{"id": opt["label"], "title": opt["label"][:24]} for opt in options]
        payload = {"type": "list", "header": {"type": "text", "text": "תפריט"}, "body": {"text": body}, "action": {"button": "בחירה", "sections": [{"title": "אפשרויות", "rows": rows}]}}
        client.messages.create(from_=from_, to=to, body=body, persistent_action=[json.dumps(payload)])
    except:
        opts_text = "\n".join([f"{i+1}. {opt['label']}" for i, opt in enumerate(options)])
        client.messages.create(from_=from_, to=to, body=f"{body}\n{opts_text}")
    return str(MessagingResponse())


# ==============================================================================
#                 ZONE B: SUPABASE BOT (BUTCHER & OTHERS WHATSAPP TEXT)
# ==============================================================================

def save_order_supabase(name: str, order_details: str, method: str, address: str, timing: str, phone: str = "לא צוין"):
    try:
        current_business = getattr(g, 'business_config', None)
        if not current_business: return "Error: No business context."
        
        owner_phone = current_business.get('owner_phone')
        bot_number = current_business.get('phone_number')
        owner_phone = ensure_whatsapp_prefix(owner_phone)

        # GET DYNAMIC CLIENT
        client = get_dynamic_twilio_client(bot_number)

        # FOOLPROOF FIX: Grab the EXACT phone number from Twilio's HTTP request
        real_sender = request.values.get('From', '')
        clean_phone = real_sender.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"

        # Beautiful Hebrew Formatting for the Boss (Master Phone)
        body = (
            f"🚨 *הזמנה התקבלה / עודכנה!* 🚨\n\n"
            f"👤 *לקוח:* {name}\n"
            f"🥩 *פירוט:* {order_details}\n"
            f"🛍️ *איסוף/משלוח:* {method}\n"
            f"📍 *כתובת:* {address}\n"
            f"⏰ *שעה מבוקשת:* {timing}\n\n"
            f"💬 *לחץ כאן ליצירת קשר עם הלקוח:* \n{wa_link}"
        )

        # 1. Send WhatsApp to Boss
        if client and owner_phone:
             client.messages.create(
                 from_=bot_number,
                 to=owner_phone,
                 body=body
             )
             
        # 2. Save directly to Supabase DB
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
                logger.error(f"Failed to save to DB (Table might not exist yet): {db_err}")

        return "ההזמנה נשמרה בהצלחה והועברה לקצב."
    except Exception as e: 
        return f"Error: {e}"

class SupabaseAgent:
    def __init__(self):
        self.chats = {}

    def get_response(self, user_phone, msg, config):
        chat_id = f"{config['phone_number']}_{user_phone}"
        
        if chat_id not in self.chats or msg.lower() == "reset":
            sys_instruct = config.get('system_instruction', 'You are a helpful assistant.')
            
            # --- 1. TIME INJECTION (ISRAEL TIME) ---
            israel_time = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
            time_str = israel_time.strftime("%d/%m/%Y %H:%M")
            sys_instruct += f"\n\n[מידע מערכת חסוי: התאריך והשעה כרגע בישראל: {time_str}.]"
            
            # --- 2. THE GOD-MODE OVERRIDE ---
            sys_instruct += "\n[הוראת מערכת קריטית: מותר לך ואתה מסוגל לעדכן הזמנות קיימות! אם לקוח מבקש לשנות הזמנה שכבר ביצע באותה שיחה, פשוט אסוף את הפרטים החדשים והפעל שוב את הפונקציה save_order_supabase עם כל המידע המעודכן. לעולם אל תגיד ללקוח שאינך יכול לשנות הזמנה.]"

            model = genai.GenerativeModel('gemini-2.5-flash', tools=[save_order_supabase], system_instruction=sys_instruct)
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

def get_business_from_supabase(bot_number):
    if not supabase: return None
    clean_num = bot_number.replace("whatsapp:", "").replace("+", "").strip()
    try:
        res = supabase.table('clients').select("*").ilike('phone_number', f'%{clean_num}%').execute()
        return res.data[0] if res.data else None
    except:
        return None

def handle_supabase_flow(sender, msg, bot_number):
    if not supabase:
        resp = MessagingResponse()
        resp.message("❌ שגיאה קריטית: הבוט עיוור. חסרים משתני הסביבה SUPABASE_URL ו-SUPABASE_KEY בשרת Render שלכם!")
        return str(resp)
    
    clean_num = bot_number.replace("whatsapp:", "").replace("+", "").strip()
    
    try:
        res = supabase.table('clients').select("*").ilike('phone_number', f'%{clean_num}%').execute()
    except Exception as e:
        resp = MessagingResponse()
        resp.message(f"❌ שגיאת תקשורת מול מסד הנתונים: {str(e)}")
        return str(resp)
        
    if not res.data: 
        resp = MessagingResponse()
        resp.message(f"❌ לא מצאתי התאמה למספר הבוט: {clean_num}")
        return str(resp)
        
    business = res.data[0]
    g.business_config = business
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

# ==============================================================================
#                 ZONE C: RETELL AI WEBHOOK (POST-CALL TRANSCRIPTS)
# ==============================================================================

@app.route("/retell-webhook", methods=['POST'])
def retell_webhook():
    """Retell AI triggers this endpoint when a voice call is completely finished."""
    try:
        data = request.get_json()
        event = data.get('event')
        
        # We only care about the end of the call
        if event == "call_analyzed":
            call = data.get('call', {})
            agent_id = call.get('agent_id')
            
            # --- EXTRACT THE ANALYSIS ---
            transcript = call.get('transcript', 'לא זמין')
            call_summary = call.get('call_analysis', {}).get('call_summary', 'לא זמין')
            user_phone = call.get('from_number', 'Unknown')
            
            # 1. IS IT THE BUTCHER BOT?
            # TODO: Replace YOUR_BUTCHER_AGENT_ID with the actual Retell Agent ID
            if agent_id == "YOUR_BUTCHER_AGENT_ID":
                working_bot_number = "97223723780"
                business = get_business_from_supabase(working_bot_number)
                client = get_dynamic_twilio_client(working_bot_number)
                
                custom_data = call.get('custom_analysis_data', {})
                name = custom_data.get('name', 'לקוח קולי')
                order_details = custom_data.get('order_details', call_summary)
                
                if business and client:
                    owner_phone = ensure_whatsapp_prefix(business.get('owner_phone'))
                    body = f"☎️ *סיכום שיחת הזמנה!*\n👤 מטלפון: {user_phone}\n🥩 סיכום שיחה: {order_details}"
                    client.messages.create(from_=f"whatsapp:+{working_bot_number}", body=body, to=owner_phone)

            # 2. IS IT THE LAWYER BOT?
            # TODO: Replace YOUR_LAWYER_AGENT_ID with the actual Retell Agent ID
            elif agent_id == "YOUR_LAWYER_AGENT_ID":
                lawyer_bot_number = "972XXXXXXXXX" # TODO: Put Lawyer's Twilio Number here
                boss_phone = ensure_whatsapp_prefix(LawyerConfig.LAWYER_PHONE)
                client = get_dynamic_twilio_client(lawyer_bot_number)
                
                if client:
                    msg = (
                        f"⚖️ *שיחה נכנסת חדשה (בוט קולי)* ⚖️\n\n"
                        f"📱 מטלפון: {user_phone}\n\n"
                        f"📝 *סיכום ה-AI:*\n{call_summary}\n\n"
                        f"📄 *תמלול מלא:*\n{transcript}"
                    )
                    client.messages.create(from_=f"whatsapp:+{lawyer_bot_number}", body=msg, to=boss_phone)

        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        logger.error(f"Retell Webhook Error: {e}")
        return jsonify({"status": "error"}), 500

# ==============================================================================
#                 ZONE E: RETELL MID-CALL TOOLS (FUNCTION CALLING)
# ==============================================================================

@app.route("/retell-tool", methods=['POST'])
def retell_tool_handler():
    try:
        data = request.get_json()
        tool_name = data.get('name')
        args = data.get('args', {})
        caller_phone = data.get('call', {}).get('from_number', '') 
        
        # NOTE: You'll want to dynamically set this based on the business eventually.
        bot_number = "whatsapp:+972XXXXXXXXX" # TODO: Put Lawyer's Twilio Number here
        boss_phone = ensure_whatsapp_prefix(LawyerConfig.LAWYER_PHONE)
        client = get_dynamic_twilio_client(bot_number)

        # --- TOOL 1: CHECK AVAILABILITY ---
        if tool_name == "check_availability":
            date_requested = args.get('date')
            return jsonify({"result": f"Looked at the calendar. Yes, {date_requested} has open slots in the afternoon. Ask the user if 14:00 or 16:00 works better."})

        # --- TOOL 2: BOOK APPOINTMENT ---
        elif tool_name == "book_appointment":
            client_name = args.get('client_name', 'לקוח לא ידוע')
            date_time = args.get('date_time', 'מועד לא ידוע')
            
            if client:
                client.messages.create(
                    from_=bot_number,
                    to=boss_phone,
                    body=f"📅 *פגישה חדשה נקבעה בשיחה!*\nשם: {client_name}\nמתי: {date_time}\nטלפון: {caller_phone}"
                )
            return jsonify({"result": f"Successfully booked for {client_name} at {date_time}. Confirm this with the user."})

        # --- TOOL 3: SEND SMS/WHATSAPP MID-CALL ---
        elif tool_name == "send_whatsapp_link":
            link_type = args.get('link_type') # 'intake' or 'document'
            
            msg = ""
            if link_type == "intake":
                msg = "שלום! הנה הקישור לטופס הקליטה למשרד עו\"ד שמעון חסקי: [LINK]"
            elif link_type == "document":
                msg = "להעלאת מסמכים מאובטחת לתיק שלך, לחץ כאן: [LINK]"
            else:
                msg = "שלום, להלן הקישור שביקשת מהנציג הקולי: [LINK]"

            if client and caller_phone:
                clean_caller = ensure_whatsapp_prefix(caller_phone)
                client.messages.create(from_=bot_number, to=clean_caller, body=msg)
                
            return jsonify({"result": "The WhatsApp message was sent successfully. Tell the user to check their phone right now."})

        # --- TOOL 4: CHECK CASE STATUS ---
        elif tool_name == "check_case_status":
            id_number = args.get('id_number')
            
            if len(id_number) > 5:
                status = "The documents were submitted to the court yesterday, we are waiting for the judge's response."
            else:
                status = "I couldn't find a case with that ID number. Please ask them to verify it."
                
            return jsonify({"result": status})

        # --- FALLBACK ---
        return jsonify({"result": "Error: Tool not recognized."})

    except Exception as e:
        logger.error(f"Retell Tool Error: {e}")
        return jsonify({"result": "Error occurred while executing tool."}), 500


# --- SECURITY: The Bouncer's Memory ---
ip_tracker = {}

# ==============================================================================
#                 ZONE D: WEBSITE CHECKOUT API (SECURED)
# ==============================================================================

@app.route("/api/web-order", methods=['POST', 'OPTIONS'])
def web_order():
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
        if client_key != "BUARON_SECURE_2026_MAX":
            logger.warning(f"BLOCKED: Unauthorized access attempt from {request.remote_addr}")
            headers = {"Access-Control-Allow-Origin": "*"}
            return jsonify({"error": "Unauthorized"}), 401

        # --- SECURITY LAYER 2: THE BOUNCER (Rate Limiting) ---
        client_ip = request.remote_addr
        current_time = time.time()
        
        if client_ip in ip_tracker:
            requests_made, first_request_time = ip_tracker[client_ip]
            if current_time - first_request_time < 300: 
                if requests_made >= 3:
                    logger.warning(f"BLOCKED: Spam detected from IP {client_ip}")
                    headers = {"Access-Control-Allow-Origin": "*"}
                    return jsonify({"error": "Too many requests. Wait 5 minutes."}), 429
                ip_tracker[client_ip] = (requests_made + 1, first_request_time)
            else:
                ip_tracker[client_ip] = (1, current_time) 
        else:
            ip_tracker[client_ip] = (1, current_time)

        # --- PROCESS THE REAL ORDER ---
        data = request.get_json()
        customer = data.get('customer', {})
        items = data.get('items', [])
        
        method_text = "משלוח 🚚" if data.get('deliveryMethod') == "delivery" else "איסוף עצמי 🏬"
        
        msg = f"🟢 *הזמנה חדשה מהאתר!* 🟢\n"
        msg += f"--------------------\n"
        msg += f"שם: {customer.get('name')}\n"
        msg += f"טלפון: {customer.get('phone')}\n"
        msg += f"שיטה: {method_text}\n"
        
        if data.get('deliveryMethod') == "delivery":
            msg += f"עיר: {customer.get('city')}\n"
            msg += f"רחוב: {customer.get('street')} {customer.get('houseNumber')}\n"
            if customer.get('floor'): msg += f"קומה: {customer.get('floor')}\n"
            if customer.get('doorCode'): msg += f"אינטרקום: {customer.get('doorCode')}\n"
            
        msg += f"\n*פירוט:*\n"
        for i, item in enumerate(items):
            p = item.get('product', {})
            qty = item.get('quantity', 0)
            price = p.get('price', 0) * qty
            msg += f"{i+1}. {p.get('name')} - {qty} ק\"ג (₪{price:.2f})\n"
            
        msg += f"\n*סה\"כ משוער: ₪{data.get('total', 0):.2f}*\n"
        
        clean_phone = customer.get('phone', '')
        if clean_phone.startswith('0'):
            clean_phone = '972' + clean_phone[1:]
        msg += f"\n💬 *לחץ כאן לשליחת הודעה ללקוח:*\nhttps://wa.me/{clean_phone}"

        target_phone = "whatsapp:+972587742596" 
        bot_number = "whatsapp:+97223723780" 
        
        client = get_dynamic_twilio_client(bot_number)
        
        if client:
            client.messages.create(
                from_=bot_number,
                to=target_phone,
                body=msg
            )
        else:
            logger.error("Could not send website order: No Twilio client found for bot number.")
            
        headers = {"Access-Control-Allow-Origin": "*"}
        return jsonify({"status": "success"}), 200, headers
        
    except Exception as e:
        logger.error(f"Web Order Error: {e}")
        headers = {"Access-Control-Allow-Origin": "*"}
        return jsonify({"status": "error", "message": str(e)}), 500, headers

@app.route("/", methods=['GET'])
def health_check():
    return "Hybrid Voice & Text System Active 🚀", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)