import os
import json
import datetime
import logging
from flask import Flask, request, g, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
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
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
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

# Twilio Client
twilio_mgr = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID else None

# ==============================================================================
#                 ZONE A: THE LAWYER BOT (ON HOLD - NOT ROUTED)
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
        real_sender = request.values.get('From', '')
        clean_phone = real_sender.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"
        
        header = "🚨 *חירום!*" if classification == "URGENT" else "✨ *ליד חדש*"
        body = f"""{header}\n👤 {name}\n📌 {topic}\n📝 {summary}\n{wa_link}"""
        
        target_phone = ensure_whatsapp_prefix(LawyerConfig.LAWYER_PHONE)
        
        if twilio_mgr and target_phone:
            twilio_mgr.messages.create(from_=request.values.get('To'), body=body, to=target_phone)
            return f"SAVED as {classification}."
        else:
            return f"SAVED as {classification} (Note: Lawyer phone not configured)."
            
    except Exception as e: return f"Error: {e}"

def book_meeting_tool(client_name: str, reason: str):
    target_phone = ensure_whatsapp_prefix(LawyerConfig.LAWYER_PHONE)
    if twilio_mgr and target_phone:
         twilio_mgr.messages.create(
             from_=request.values.get('To'),
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
        self.model = genai.GenerativeModel('gemini-2.0-flash', tools=self.tools, system_instruction=self.system_instruction)
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
    twilio_mgr.messages.create(from_=from_, body=body, to=to)
    return str(MessagingResponse())

def send_lawyer_menu(to, body, options, from_):
    try:
        rows = [{"id": opt["label"], "title": opt["label"][:24]} for opt in options]
        payload = {"type": "list", "header": {"type": "text", "text": "תפריט"}, "body": {"text": body}, "action": {"button": "בחירה", "sections": [{"title": "אפשרויות", "rows": rows}]}}
        twilio_mgr.messages.create(from_=from_, to=to, body=body, persistent_action=[json.dumps(payload)])
    except:
        opts_text = "\n".join([f"{i+1}. {opt['label']}" for i, opt in enumerate(options)])
        twilio_mgr.messages.create(from_=from_, to=to, body=f"{body}\n{opts_text}")
    return str(MessagingResponse())

# ==============================================================================
#                 ZONE B: SUPABASE BOT (BUTCHER & OTHERS WHATSAPP TEXT)
# ==============================================================================

def save_order_supabase(name: str, order_details: str, method: str, address: str, timing: str, phone: str):
    try:
        current_business = getattr(g, 'business_config', None)
        if not current_business: return "Error: No business context."
        owner_phone = current_business.get('owner_phone')
        bot_number = current_business.get('phone_number')
        
        owner_phone = ensure_whatsapp_prefix(owner_phone)

        if twilio_mgr and owner_phone:
             twilio_mgr.messages.create(
                 from_=bot_number,
                 to=owner_phone,
                 body=f"New Order!\nName: {name}\nDetails: {order_details}\nAddress: {address}"
             )
        return "Order Saved & Sent to Owner."
    except Exception as e: return f"Error: {e}"

class SupabaseAgent:
    def __init__(self):
        self.chats = {}

    def get_response(self, user_phone, msg, config):
        chat_id = f"{config['phone_number']}_{user_phone}"
        if chat_id not in self.chats or msg.lower() == "reset":
            sys_instruct = config.get('system_instruction', 'You are a helpful assistant.')
            model = genai.GenerativeModel('gemini-2.0-flash', tools=[save_order_supabase], system_instruction=sys_instruct)
            self.chats[chat_id] = model.start_chat(enable_automatic_function_calling=True)
        
        try:
            return self.chats[chat_id].send_message(msg).text
        except Exception as e:
            if chat_id in self.chats:
                del self.chats[chat_id]
            # MAGIC FIX: Instead of a generic error, text us the exact API crash reason!
            return f"🤖 שגיאת מודל AI:\n{str(e)}"

supabase_agent = SupabaseAgent()

def get_business_from_supabase(bot_number):
    """Helper function for Zone C to grab business info safely using FUZZY SEARCH."""
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
        resp.message(f"❌ לא מצאתי התאמה. הנה המספר הנקי שחיפשתי: {clean_num}")
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
    
    # FORCING BUTCHER SHOP MODE
    return handle_supabase_flow(sender, incoming_msg, bot_number)

# ==============================================================================
#                 ZONE C: RETELL AI WEBHOOK (NEW VOICE CALL ORDERS)
# ==============================================================================

@app.route("/retell-webhook", methods=['POST'])
def retell_webhook():
    """Retell AI triggers this endpoint when a voice call order is complete."""
    try:
        data = request.get_json()
        args = data.get('args', {})
        
        name = args.get('name', 'לא צוין')
        order_details = args.get('order_details', 'לא צוין')
        address = args.get('address', 'לא צוין')
        
        working_bot_number = "97223723780" 
        business = get_business_from_supabase(working_bot_number)
        
        if business and twilio_mgr:
            owner_phone = ensure_whatsapp_prefix(business.get('owner_phone'))
            body = f"☎️ *הזמנה קולית חדשה (משיחת טלפון)!*\n👤 שם: {name}\n🥩 הזמנה: {order_details}\n📍 כתובת/איסוף: {address}"
            
            twilio_mgr.messages.create(
                from_=f"whatsapp:+{working_bot_number}",
                body=body, 
                to=owner_phone
            )
            
        return jsonify({"status": "success", "message": "ההזמנה נשלחה לבעל העסק בהצלחה."})
        
    except Exception as e:
        logger.error(f"Retell Webhook Error: {e}")
        return jsonify({"status": "error", "message": "שגיאה במערכת."})

@app.route("/", methods=['GET'])
def health_check():
    return "Hybrid Voice & Text System Active 🚀", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)