import os
import json
import datetime
import logging
import smtplib
from email.message import EmailMessage
from flask import Flask, request, g
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread
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
LAWYER_NUMBER_ENV = os.getenv('LAWYER_WHATSAPP_NUMBER') # המספר של העורך דין (לזיהוי)

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
#                 ZONE A: THE LAWYER BOT (LEGACY CODE)
# ==============================================================================

# Lawyer Specific Globals
lawyer_sessions = {}
last_auto_replies = {} # זיכרון לשיחות שלא נענו (מונע ספאם)
SERVICE_ACCOUNT_FILE = 'credentials.json'

# Lawyer Config Class
class LawyerConfig:
    BUSINESS_NAME = "Adv. Shimon Hasky"
    SHEET_ID = "1GuXkaBAUfswXwA1uwytrouqhepOASyW35h4GVaC5bQ0" 
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    EMAIL_SENDER = os.getenv('EMAIL_SENDER')
    EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD', '').replace(" ", "").strip()
    LAWYER_EMAIL = os.getenv('LAWYER_EMAIL')
    LAWYER_PHONE = os.getenv('LAWYER_PHONE')
    CONTENT_SID = "HX28b3beac873cd8dba0852c183b8bf0ea"
    VIP_NUMBERS = [LAWYER_PHONE]
    COOL_DOWN_HOURS = 24
    
    # Lawyer Menu Flow
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
        "FINISH_BOOKING": { "message": "פגישה שוריינה למחר ב-10:00.", "action": "book_meeting" }
    }

# Helper: Create Credentials File
def create_credentials():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        json_content = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if json_content:
            with open(SERVICE_ACCOUNT_FILE, 'w') as f:
                f.write(json_content)

# Helper: Google Services
def get_google_services():
    create_credentials()
    try:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
            sheet = gc.open_by_key(LawyerConfig.SHEET_ID).sheet1
            
            cal_scopes = ['https://www.googleapis.com/auth/calendar']
            creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=cal_scopes)
            calendar = build('calendar', 'v3', credentials=creds)
            return sheet, calendar
    except Exception as e:
        logger.error(f"Google Service Error: {e}")
    return None, None

# Tool: Save Case
def save_case_summary(name: str, topic: str, summary: str, phone: str, classification: str = "NEW_LEAD"):
    try:
        sheet, _ = get_google_services()
        clean_phone = phone.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"
        
        # Save to Sheet
        if sheet:
            row = [datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), classification, name, clean_phone, topic, summary]
            sheet.append_row(row)

        # Send Email
        if LawyerConfig.EMAIL_SENDER and LawyerConfig.EMAIL_PASSWORD:
            msg = EmailMessage()
            msg['Subject'] = f"✨ ליד חדש: {name} - {topic} ({classification})"
            msg['From'] = LawyerConfig.EMAIL_SENDER
            msg['To'] = LawyerConfig.LAWYER_EMAIL
            msg.set_content(f"סוג: {classification}\nשם: {name}\nטלפון: {phone}\nסיכום:\n{summary}")
            with smtplib.SMTP('smtp.gmail.com', 587) as smtp:
                smtp.ehlo(); smtp.starttls(); smtp.ehlo()
                smtp.login(LawyerConfig.EMAIL_SENDER, LawyerConfig.EMAIL_PASSWORD)
                smtp.send_message(msg)

        # Notify Lawyer via WhatsApp
        if twilio_mgr and LawyerConfig.LAWYER_PHONE:
            header = "🚨 *חירום!*" if classification == "URGENT" else "✨ *ליד חדש*"
            body = f"""{header}\n👤 {name}\n📌 {topic}\n📝 {summary}\n{wa_link}"""
            twilio_mgr.messages.create(from_=request.values.get('To'), body=body, to=LawyerConfig.LAWYER_PHONE)
            
        return f"SAVED as {classification}."
    except Exception as e: return f"Error: {e}"

# Tool: Book Meeting
def book_meeting_tool(client_name: str, reason: str):
    try:
        _, calendar = get_google_services()
        if not calendar: return "Error: Calendar not connected."
        start = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=10, minute=0).isoformat()
        end = (datetime.datetime.now() + datetime.timedelta(days=1, hours=1)).replace(hour=10, minute=0).isoformat()
        event = {
            'summary': f"Meeting: {client_name}",
            'description': reason,
            'start': {'dateTime': start, 'timeZone': 'Asia/Jerusalem'},
            'end': {'dateTime': end, 'timeZone': 'Asia/Jerusalem'}
        }
        calendar.events().insert(calendarId=LawyerConfig.CALENDAR_ID, body=event).execute()
        return "Success: Meeting booked for tomorrow 10:00."
    except Exception as e: return f"Booking Error: {e}"

# Lawyer AI Agent - (YOUR EXACT VERSION)
class LawyerAgent:
    def __init__(self):
        self.tools = [save_case_summary, book_meeting_tool]
        
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

        📍 **שלב 3: קבלת שם**
        אם אין לך שם עדיין:
        - "מה שמך?" (פשוט וישיר)
        - אל תאמר "שם מלא" - תגיד רק "שם"
        - אם הם נתנו רק שם פרטי, תגיד: "ושם משפחה?"

        📍 **שלב 4: הבנת הבעיה**
        שאל שאלה אחת ממוקדת:
        - גירושין: "יש ילדים מתחת לגיל 18?"
        - משמורת: "הילדים איתך או עם הצד השני?"
        - ירושה: "יש צוואה כתובה?"
        - תאונה: "מתי זה קרה?"
        אל תשאל יותר משאלה אחת. תן ללקוח לספר.

        📍 **שלב 5: סיווג ושמירה**
        ברגע שיש לך: שם + תיאור הבעיה → קרא לפונקציה `save_case_summary`.

        **חוקי סיווג (CLASSIFICATION):**

        🔥 **"URGENT"** - השתמש כשיש:
        - מילות חירום: "דחוף", "משטרה", "אלימות", "חטיפה", "מפחד/ת", "עכשיו"
        - סימני פניקה: "!!!", אותיות גדולות, "עזרה"
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
        אתה: "הבנתי. רשמתי את הפרטים והעברתי לעו\"ד חסקי."
        (Tool Action: classification="NEW_LEAD")

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        דוגמה 3: מצב חירום
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        לקוח: "דחוףףף בעלי השתגע ושבר את הבית המשטרה בדרך!!!"
        אתה: "אני רואה שזה חירום. אני שולח הודעה דחופה לעו\"ד חסקי עכשיו. מה שמך המלא?"
        לקוח: "רינת לוי"
        אתה: "רינת, הפרטים הועברו בדחיפות. עו\"ד חסקי יחזור אליך בהקדם האפשרי." 
        (Tool Action: classification="URGENT")

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        דוגמה 4: שאלה כללית בלי סיפור
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        לקוח: "מה זה הסכם ממון?"
        אתה: "הסכם שקובע איך מחלקים רכוש במקרה של פרידה. אפשר לעשות לפני או אחרי נישואין. רוצה לשמוע עוד?"
        לקוח: "כן, איך עושים את זה?"
        אתה: "עו\"ד חסקי עושה את זה כל הזמן, זה לוקח פגישה אחת. מה שמך?"
        לקוח: "יוסי"
        אתה: "ושם משפחה?"
        לקוח: "אברהם"
        אתה: "מעולה יוסי. רשמתי ועו\"ד חסקי יחזור אליך."
        (Tool Action: classification="NEW_LEAD")

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        דוגמה 5: התחלה רגשית
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        לקוח: "אני לא יודעת מה לעשות, הבעל שלי רוצה לקחת את הילדים"
        אתה: "מצטער לשמוע שאת עוברת את זה. בואי נראה איך אפשר לעזור. מה שמך?"
        לקוח: "מיכל גולן"
        אתה: "מיכל, הילדים איתך עכשיו?"
        לקוח: "כן, אבל הוא מאיים"
        אתה: "הבנתי. העברתי את הפרטים לעו\"ד חסקי בדחיפות. הוא יחזור אליך בהקדם."
        (Tool Action: classification="URGENT")

        **כללי זהב - קרא לפני כל תשובה:**

        ✅ **תמיד עשה:**
        - דבר בעברית פשוטה וברורה
        - אם לקוח רגשי - האט, הקשב, תמוך
        - שאל שאלה אחת בכל פעם
        - אם יש שאלה - ענה קודם
        - אחרי שיש שם + בעיה - שמור מיד

        ❌ **לעולם אל תעשה:**
        - לא לכתוב קוד Python
        - לא לשאול מספר טלפון (כבר יש לך)
        - לא לכתוב משפטים ארוכים (מקסימום 2 משפטים)
        - לא להשתמש במילים כמו "בבקשה עקוב אחרי השלבים" - זה רובוטי
        - לא לחזור על מידע שהלקוח כבר אמר
        - לא לדבר באנגלית (גם אם הלקוח כותב באנגלית, תענה בעברית)
        - **לא לתת מחירים:** אם שואלים על מחיר, תגיד שזה תלוי במקרה וייקבע בפגישה.
        - **לא להבטיח זמנים:** אל תגיד "הוא יתקשר בעוד 5 דקות" או "היום". תגיד "בהקדם".

        **מבנה תשובה אידיאלי:**
        משפט 1: אמפתיה/תשובה
        משפט 2: שאלה ממוקדת
        סה"כ: 10-25 מילים.

        **טיפול בשגיאות:**
        אם הפונקציה החזירה "Saved to Database" - תגיד:
        "הפרטים נשמרו והועברו לעו\"ד חסקי."

        זכור: אתה לא עורך דין. אתה מזכיר חכם שמסנן, מסווג, ומעביר לעו"ד.
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

# --- THE LAWYER FLOW HANDLER ---
def handle_lawyer_flow(sender, incoming_msg, bot_number):
    # 1. Reset
    if incoming_msg.lower() == "reset":
        lawyer_sessions[sender] = 'START'
        return send_lawyer_menu(sender, "🔄 *System Reset*", LawyerConfig.FLOW_STATES['START']['options'], bot_number)

    # 2. New User
    if sender not in lawyer_sessions:
        lawyer_sessions[sender] = 'START'
        return send_lawyer_menu(sender, LawyerConfig.FLOW_STATES['START']['message'], LawyerConfig.FLOW_STATES['START']['options'], bot_number)

    # 3. Handle Menu Selection (Digits)
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

    # 4. Handle Booking Flow
    if lawyer_sessions[sender] == 'ASK_BOOKING':
        book_meeting_tool(sender, "Manual Booking")
        lawyer_sessions[sender] = 'START'
        return send_lawyer_msg(sender, LawyerConfig.FLOW_STATES['FINISH_BOOKING']['message'], bot_number)

    # 5. AI Chat
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
#                 ZONE B: THE NEW SUPABASE BOT (BUTCHER & OTHERS)
# ==============================================================================

def save_order_supabase(name: str, order_details: str, method: str, address: str, timing: str, phone: str):
    """Save order from Supabase Bot"""
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
    if not business:
        return str(MessagingResponse()) 

    g.business_config = business
    reply = supabase_agent.get_response(sender, msg, business)
    
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# ==============================================================================
#                 MAIN ROUTER (THE SWITCH)
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
#                 ZONE C: VOICE CALL HANDLER (FORWARDING + CATCHER)
# ==============================================================================

@app.route("/incoming", methods=['POST'])
def incoming_voice():
    """
    כאשר שיחה נכנסת: הבוט מעביר אותה (Forward) לטלפון האמיתי.
    """
    resp = VoiceResponse()
    
    # 1. זיהוי לאן השיחה הגיעה (עו"ד או אטליז)
    bot_number = request.values.get('To', '')
    clean_bot = bot_number.replace("whatsapp:", "")
    clean_lawyer_env = (LAWYER_NUMBER_ENV or "").replace("whatsapp:", "").strip()
    
    target_phone = None

    if clean_bot == clean_lawyer_env:
        target_phone = LawyerConfig.LAWYER_PHONE
    else:
        business = get_business_from_supabase(clean_bot)
        if business:
            target_phone = business.get('owner_phone')

    # 2. ביצוע הפניה (Forwarding)
    if target_phone:
        # מחייג לבעל העסק. אם לא עונים תוך 20 שניות -> לך ל-/call_ended
        dial = resp.dial(timeout=20, action='/call_ended')
        dial.number(target_phone)
    else:
        resp.say("Business number not configured.")
    
    return str(resp)

@app.route("/call_ended", methods=['POST'])
def call_ended_handler():
    """
    נקרא רק אחרי שהחיוג הסתיים. בודק אם ענו. אם לא - שולח וואטסאפ.
    """
    dial_status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '') # הלקוח
    bot_number = request.values.get('To', '') # המספר העסקי

    # סטטוסים שנחשבים "לא ענו" (Busy, No-answer, Failed, Canceled)
    if dial_status in ['busy', 'no-answer', 'failed', 'canceled']:
        
        clean_bot = bot_number.replace("whatsapp:", "")
        clean_lawyer_env = (LAWYER_NUMBER_ENV or "").replace("whatsapp:", "").strip()
        msg_body = None

        if clean_bot == clean_lawyer_env:
            # עורך דין (בדיקת VIP)
            if caller not in LawyerConfig.VIP_NUMBERS:
                 msg_body = "שלום, הגעתם למשרד עו\"ד שמעון חסקי. לא יכולנו לענות לשיחה כרגע, אבל אנחנו זמינים כאן! כתבו לנו הודעה ונחזור בהקדם."
        else:
            # אטליז / עסק אחר
            business = get_business_from_supabase(clean_bot)
            if business:
                name = business.get('business_name', 'העסק')
                msg_body = f"שלום, הגעתם ל{name}. אנחנו לא זמינים כרגע לשיחה, אבל אפשר לבצע הזמנות כאן בוואטסאפ!"

        # שליחת הודעת WhatsApp
        if msg_body:
            try:
                # הוספת whatsapp: לשני הצדדים לשליחה תקינה
                final_from = f"whatsapp:{clean_bot.replace('whatsapp:', '')}"
                final_to = f"whatsapp:{caller.replace('whatsapp:', '')}"
                
                twilio_mgr.messages.create(from_=final_from, to=final_to, body=msg_body)
                logger.info(f"Missed call detected ({dial_status}). WhatsApp sent to {caller}.")
            except Exception as e:
                logger.error(f"Failed to send miss-call WhatsApp: {e}")

    return str(VoiceResponse())

@app.route("/", methods=['GET'])
def health_check():
    return "Hybrid Bot System Active 🚀", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)