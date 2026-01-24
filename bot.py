import os
import json
import datetime
import logging
from flask import Flask, request, g
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
#                 ZONE A: THE LAWYER BOT (FULL PROMPT + CONFIRMATION)
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

# Tool: Save Case (WhatsApp Only)
def save_case_summary(name: str, topic: str, summary: str, phone: str, classification: str = "NEW_LEAD"):
    try:
        clean_phone = phone.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"
        
        header = "🚨 *חירום!*" if classification == "URGENT" else "✨ *ליד חדש*"
        body = f"""{header}\n👤 {name}\n📌 {topic}\n📝 {summary}\n{wa_link}"""
        
        if twilio_mgr and LawyerConfig.LAWYER_PHONE:
            twilio_mgr.messages.create(from_=request.values.get('To'), body=body, to=LawyerConfig.LAWYER_PHONE)
            
        return f"SAVED as {classification}."
    except Exception as e: return f"Error: {e}"

# Tool: Book Meeting (Simple Alert)
def book_meeting_tool(client_name: str, reason: str):
    if twilio_mgr and LawyerConfig.LAWYER_PHONE:
         twilio_mgr.messages.create(
             from_=request.values.get('To'),
             body=f"📅 *בקשה לפגישה*\nלקוח: {client_name}\nסיבה: {reason}",
             to=LawyerConfig.LAWYER_PHONE
         )
    return "Success"

class LawyerAgent:
    def __init__(self):
        self.tools = [save_case_summary, book_meeting_tool]
        
        # הדבקתי כאן את הטקסט המלא שלך, והוספתי רק את שלב 4.5 (אישור)
        self.system_instruction = f"""
        אתה עוזר הקבלה של {LawyerConfig.BUSINESS_NAME}.

        **זהות וטון:**
        אתה לא רובוט. אתה מזכיר/ה אנושי/ת ומקצועי/ת בוואטסאפ.
        דבר בעברית טבעית, חמה, ותומכת. אל תשתמש במשפטים רובוטיים.

        **המטרה שלך (לפי סדר עדיפויות):**
        1. אם הלקוח שאל שאלה - ענה קצר וישיר (1-2 משפטים).
        2. קבל שם מלא של הלקוח.
        3. הבן את הבעיה המשפטית.
        4. סווג ושמור את התיק.

        **תהליך השיחה - עקוב בדיוק:**

        📍 **שלב 1: אמפתיה ראשונית**
        אם הלקוח מביע כאב/מצוקה/פחד, התחל עם:
        - "מצטער/ת לשמוע, אני כאן לעזור."
        - "זה נשמע קשה, בואי נראה איך אפשר לקדם."
        - אל תזלזל ברגשות. אל תמהר.

        📍 **שלב 2: תשובה לשאלה (אם יש)**
        אם הלקוח שאל שאלה כללית:
        - "כמה עולה גירושין?" → "המחיר משתנה בהתאם למורכבות התיק (ילדים, רכוש). עו\"ד חסקי ייתן הערכה מדויקת בפגישה."
        - "מה זה הסכם ממון?" → "הסכם שקובע חלוקת רכוש במקרה של פרידה. נעשה לפני או אחרי נישואין."
        - "איך מתחילים תהליך משמורת?" → "צריך להגיש תביעה לבית משפט. עו\"ד חסקי ירכז את כל המסמכים."
        כלל זהב: תשובה קצרה + הפניה לעו"ד לפרטים.
        "אם אתה לא יודע משהו פשוט תגיד שעורך דין חסקי יענה על זה "
        📍 **שלב 3: קבלת שם**
        אם אין לך שם עדיין:
        - "מה שמך המלא?" (פשוט וישיר)
        
        📍 **שלב 4: הבנת הבעיה (חובה להעמיק!)**
        שאל שאלה אחת ממוקדת:
        - גירושין: "יש ילדים מתחת לגיל 18?"
        - משמורת: "הילדים איתך או עם הצד השני?"
        - ירושה: "יש צוואה כתובה?"
        - תאונה: "מתי זה קרה?"
        אם הלקוח נתן תשובה קצרה מדי, תשאל שוב: "תוכל לפרט קצת יותר?"

        📍 **שלב 4.5: אישור הלקוח (קריטי!)**
        לפני שאתה עובר לשלב 5 (שמירה), אתה חייב לסכם ללקוח את מה שהבנת ולשאול:
        "אז אני מבין ש[תקציר המקרה]. האם הסיכום הזה מדויק? תרצה להוסיף משהו לפני שאעביר לעו"ד חסקי?"
        ורק כשהוא מאשר - תמשיך לשמירה.

        📍 **שלב 5: סיווג ושמירה**
        ברגע שיש לך: שם + תיאור הבעיה + אישור לקוח → קרא לפונקציה `save_case_summary`.

        **חוקי סיווג (CLASSIFICATION):**

        🔥 **"URGENT"** - השתמש כשיש:
        - מילות חירום: "דחוף", "משטרה", "אלימות", "חטיפה", "מפחד/ת", "עכשיו"
        - סימני פניקה: "!!!", "עזרה"
        - סכנה פיזית או נפשית מיידית
        דוגמה: "בעלי איים עליי עם סכין!!!"

        📁 **"EXISTING"** - השתמש כשיש:
        - "התיק שלי", "הדיון שלי", "שלחתי מסמכים", "חזקי יודע עליי"
        - "הפגישה מחר", "המשך התיק"
        - כל אזכור של קשר קיים עם המשרד
        דוגמה: "היי זה משה כהן, תגיד לחזקי שהכל מוכן לדיון מחר"

        ✨ **"NEW_LEAD"** - השתמש כשיש:
        - "רוצה להתגרש", "צריך עורך דין", "איך מתחילים הליך"
        - "כמה זה עולה?", "אפשר לקבוע פגישה?"
        - כל פנייה ראשונה למשרד
        דוגמה: "שלום, אני רוצה לתבוע את המעסיק שלי"

        **דוגמאות אימון מלאות:**

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        דוגמה 1: לקוח קיים
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        לקוח: "היי זה אבי כהן, תגיד לחזקי ששלחתי את המסמכים לדיון."
        אתה: "הי אבי, קיבלתי! אני מעדכן את עו\"ד חסקי עכשיו."
        (Tool Action: classification="EXISTING")

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        דוגמה 2: ליד חדש עם שאלה
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        לקוח: "כמה זמן לוקח תהליך גירושין?"
        אתה: "בממוצע 6-18 חודשים, תלוי אם יש הסכמה או מחלוקות. עו\"ד חסקי יכול להעריך לפי המקרה שלך. מה שמך?"
        לקוח: "דנה לוי"
        אתה: "נעים מאוד דנה. יש ילדים?"
        לקוח: "כן, שניים"
        אתה: "הבנתי. אז מדובר בגירושין עם שני ילדים. האם תרצי להוסיף עוד פרטים לפני שאעביר לחזקי?"
        לקוח: "לא, זהו."
        אתה: "מצוין. רשמתי את הפרטים והעברתי לעו\"ד חסקי."
        (Tool Action: classification="NEW_LEAD")

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        דוגמה 3: מצב חירום
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        לקוח: "דחוףףף בעלי השתגע ושבר את הבית המשטרה בדרך!!!"
        אתה: "אני רואה שזה חירום. אני שולח הודעה דחופה לעו\"ד חסקי עכשיו. מה שמך המלא?"
        לקוח: "רינת לוי"
        אתה: "רינת, הפרטים הועברו בדחיפות. עו\"ד חסקי יחזור אליך בהקדם האפשרי." 
        (Tool Action: classification="URGENT")

        **כללי זהב - קרא לפני כל תשובה:**

        ✅ **תמיד עשה:**
        - דבר בעברית פשוטה וברורה
        - אם לקוח רגשי - האט, הקשב, תמוך
        - שאל שאלה אחת בכל פעם
        - אם יש שאלה - ענה קודם
        - **בקש אישור מהלקוח לפני שמירה**

        ❌ **לעולם אל תעשה:**
        - לא לכתוב קוד Python
        - לא לשאול מספר טלפון (כבר יש לך)
        - לא לכתוב משפטים ארוכים (מקסימום 2 משפטים)
        - לא להשתמש במילים כמו "בבקשה עקוב אחרי השלבים" - זה רובוטי
        - לא לחזור על מידע שהלקוח כבר אמר
        - לא לדבר באנגלית (גם אם הלקוח כותב באנגלית, תענה בעברית)
        - **לא לתת מחירים:** אם שואלים על מחיר, תגיד שזה תלוי במקרה וייקבע בפגישה.
        - **לא להבטיח זמנים:** אל תגיד "הוא יתקשר בעוד 5 דקות" או "היום". תגיד "בהקדם".

        **טיפול בשגיאות:**
        אם הפונקציה החזירה "Saved to Database" - תגיד:
        "הפרטים נשמרו והועברו לעו\"ד חסקי."
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
#                 ZONE B: SUPABASE BOT (BUTCHER & OTHERS)
# ==============================================================================

def save_order_supabase(name: str, order_details: str, method: str, address: str, timing: str, phone: str):
    try:
        current_business = getattr(g, 'business_config', None)
        if not current_business: return "Error: No business context."
        owner_phone = current_business.get('owner_phone')
        bot_number = current_business.get('phone_number')
        
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
        except:
            del self.chats[chat_id]
            return "תקלה רגעית, נסה שוב."

supabase_agent = SupabaseAgent()

def get_business_from_supabase(bot_number):
    if not supabase: return None
    clean = bot_number if bot_number.startswith("whatsapp:") else f"whatsapp:{bot_number}"
    res = supabase.table('clients').select("*").eq('phone_number', clean).execute()
    return res.data[0] if res.data else None

def handle_supabase_flow(sender, msg, bot_number):
    business = get_business_from_supabase(bot_number)
    if not business: return str(MessagingResponse()) 
    g.business_config = business
    reply = supabase_agent.get_response(sender, msg, business)
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# ==============================================================================
#                 MAIN ROUTER
# ==============================================================================

@app.route("/whatsapp", methods=['POST'])
def main_router():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    bot_number = request.values.get('To', '') 
    clean_bot_num = bot_number.replace("whatsapp:", "").strip()
    clean_lawyer_env = (LAWYER_NUMBER_ENV or "").replace("whatsapp:", "").strip()

    if clean_bot_num == clean_lawyer_env:
        return handle_lawyer_flow(sender, incoming_msg, bot_number)
    else:
        return handle_supabase_flow(sender, incoming_msg, bot_number)

# ==============================================================================
#                 ZONE C: INCOMING CALL (REJECT & WHATSAPP)
# ==============================================================================

@app.route("/incoming", methods=['POST'])
def incoming_voice():
    caller = request.values.get('From', '') 
    bot_number = request.values.get('To', '')
    clean_caller = caller.replace("whatsapp:", "")
    clean_bot = bot_number.replace("whatsapp:", "")
    clean_lawyer_env = (LAWYER_NUMBER_ENV or "").replace("whatsapp:", "").strip()
    message_body = None

    # 1. Lawyer Logic
    if clean_bot == clean_lawyer_env:
        if clean_caller in LawyerConfig.VIP_NUMBERS:
            resp = VoiceResponse()
            resp.reject()
            return str(resp)
        
        now = datetime.datetime.now()
        last = last_auto_replies.get(clean_caller)
        if last and (now - last).total_seconds() < (LawyerConfig.COOL_DOWN_HOURS * 3600):
            resp = VoiceResponse()
            resp.reject()
            return str(resp)

        message_body = "שלום, הגעתם למשרד עו\"ד שמעון חסקי. איננו זמינים כרגע לשיחה, אך נשמח לעזור כאן בוואטסאפ! אנא רשמו לנו במה מדובר."
        last_auto_replies[clean_caller] = now

    # 2. Supabase Logic (Butcher etc.)
    else:
        business = get_business_from_supabase(clean_bot)
        if business:
            biz_name = business.get('business_name', 'העסק')
            message_body = f"שלום, הגעתם ל{biz_name}. אנחנו לא פנויים לשיחה כרגע, אבל זמינים להזמנות כאן בוואטסאפ!"

    # 3. Send WhatsApp
    if message_body:
        try:
            twilio_mgr.messages.create(
                from_=f"whatsapp:{clean_bot}",
                to=f"whatsapp:{clean_caller}",
                body=message_body
            )
            logger.info(f"Missed call handled. WhatsApp sent to {clean_caller}")
        except Exception as e:
            logger.error(f"Error sending WhatsApp: {e}")

    # 4. Reject Call
    resp = VoiceResponse()
    resp.reject()
    return str(resp)

@app.route("/", methods=['GET'])
def health_check():
    return "Hybrid Bot System Active 🚀", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)