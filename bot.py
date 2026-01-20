import os
import json
import datetime
import logging
import smtplib
from email.message import EmailMessage
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread
from dotenv import load_dotenv

# --- 1. SYSTEM SETUP ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LawyerBot")
app = Flask(__name__)

class Config:
    BUSINESS_NAME = "Adv. Shimon Hasky"
    CONTENT_SID = "HX28b3beac873cd8dba0852c183b8bf0ea" 

    _raw_phone = os.getenv('LAWYER_PHONE', '')
    if _raw_phone and not _raw_phone.startswith('whatsapp:'):
        LAWYER_PHONE = f"whatsapp:{_raw_phone}"
    else:
        LAWYER_PHONE = _raw_phone

    EMAIL_SENDER = os.getenv('EMAIL_SENDER')
    _raw_pass = os.getenv('EMAIL_PASSWORD', '')
    EMAIL_PASSWORD = _raw_pass.replace(" ", "").strip()
    LAWYER_EMAIL = os.getenv('LAWYER_EMAIL')
    
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = os.getenv('WHATSAPP_NUMBER')
    
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    VIP_NUMBERS = [LAWYER_PHONE]
    COOL_DOWN_HOURS = 24
    
    FLOW_STATES = {
        "START": {
            "message": """שלום, הגעתם למשרד עו"ד שמעון חסקי. ⚖️
אני העוזר החכם של המשרד.

כדי שנתקדם, תוכל לבחור נושא, או לכתוב לי ישר מה קרה.

1️⃣ גירושין
2️⃣ משמורת ילדים
3️⃣ הסכמי ממון
4️⃣ צוואות וירושות
5️⃣ תיאום פגישה
6️⃣ 🤖 התייעצות עם נציג (AI)""",
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

def create_credentials():
    if not os.path.exists(Config.SERVICE_ACCOUNT_FILE):
        json_content = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if json_content:
            with open(Config.SERVICE_ACCOUNT_FILE, 'w') as f:
                f.write(json_content)

create_credentials()

twilio_mgr = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN) if Config.TWILIO_SID else None

def send_email_report(name, topic, summary, phone, classification):
    if not Config.EMAIL_SENDER or not Config.EMAIL_PASSWORD:
        return "Skipped"
    
    # נושא אימייל דינמי לפי סיווג
    if classification == "URGENT":
        subject_line = f"🚨 דחוף ביותר: {name} - {topic}"
    elif classification == "EXISTING":
        subject_line = f"📂 הודעה מלקוח: {name}"
    else:
        subject_line = f"✨ ליד חדש: {name} - {topic}"

    msg = EmailMessage()
    msg['Subject'] = subject_line
    msg['From'] = Config.EMAIL_SENDER
    msg['To'] = Config.LAWYER_EMAIL
    msg.set_content(f"סוג פנייה: {classification}\nשם: {name}\nטלפון: {phone}\n\nסיכום:\n{summary}")
    
    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo()
            smtp.login(Config.EMAIL_SENDER, Config.EMAIL_PASSWORD)
            smtp.send_message(msg)
        return "Email Sent"
    except: return "Email Failed"

# --- UPDATED SAVE FUNCTION: HANDLES CLASSIFICATION ---
def save_case_summary(name: str, topic: str, summary: str, phone: str, classification: str = "NEW_LEAD"):
    """
    Saves case details.
    classification options: 'URGENT', 'EXISTING', 'NEW_LEAD'
    """
    try:
        clean_phone = phone.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"

        # שמירה בגיליון עם העמודה החדשה
        if os.path.exists(Config.SERVICE_ACCOUNT_FILE):
            try:
                gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
                sheet = gc.open_by_key(Config.SHEET_ID).sheet1
                sheet.append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), classification, name, clean_phone, topic, summary])
            except: pass 

        send_email_report(name, topic, summary, clean_phone, classification)
        
        # בניית הודעת וואטסאפ מותאמת אישית לחסקי
        if twilio_mgr and Config.LAWYER_PHONE:
            
            if classification == "URGENT":
                header = "🚨 *מקרה חירום / דחוף!* 🚨"
            elif classification == "EXISTING":
                header = "📂 *הודעה מלקוח קיים*"
            else:
                header = "✨ *ליד חדש נכנס!*"

            msg_body = f"""{header}
👤 *שם:* {name}
📌 *נושא:* {topic}
📝 *סיכום:* {summary}

👇 *לחץ לחיוג/וואטסאפ:*
{wa_link}"""
            
            twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=msg_body, to=Config.LAWYER_PHONE)
            
        return f"SAVED as {classification}. Client: {name}."
    except Exception as e: return f"Error: {str(e)[:100]}"

def book_meeting(client_name: str, reason: str):
    try:
        if not os.path.exists(Config.SERVICE_ACCOUNT_FILE): create_credentials()
        creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar'])
        calendar = build('calendar', 'v3', credentials=creds)
        start = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0).isoformat()
        end = (datetime.datetime.now() + datetime.timedelta(days=1, hours=1)).replace(hour=10, minute=0, second=0).isoformat()
        event = {'summary': f"Meeting: {client_name}", 'description': reason, 'start': {'dateTime': start, 'timeZone': 'Asia/Jerusalem'}, 'end': {'dateTime': end, 'timeZone': 'Asia/Jerusalem'}}
        calendar.events().insert(calendarId=Config.CALENDAR_ID, body=event).execute()
        return "Success: Meeting booked."
    except Exception as e: return f"Error: {str(e)}"

# --- 4. AI AGENT (CLASSIFIER MODE) ---
class GeminiAgent:
    def __init__(self):
        genai.configure(api_key=Config.GOOGLE_API_KEY)
        self.tools = [save_case_summary, book_meeting]
        
        # המוח המשודרג: יודע לסווג ולשלוח התראות שונות
        self.system_instruction = f"""
        You are "HaskyAI", the office manager for {Config.BUSINESS_NAME}.
        Language: HEBREW ONLY.

        **MISSION:**
        1. Identify User Type.
        2. Get Name + Story.
        3. CALL `save_case_summary` with the correct `classification`.

        **CLASSIFICATION RULES (Critical):**
        * **"URGENT"**: Police, Violence, Kidnapping, "Scared", "Emergency".
        * **"EXISTING"**: "My file", "Hearing tomorrow", "Hezki knows me", "Sent documents".
        * **"NEW_LEAD"**: "I want to divorce", "How much?", "Sue someone".

        **KNOWLEDGE BASE:**
        * Lawyer: Adv. Shimon Hasky ("Hezki").
        * **How to Sue:** Need a lawyer.
        * **Divorce/Custody/Assets:** We handle it all.

        **TRAINING EXAMPLES:**

        --- Ex 1: Existing Client ---
        User: "היי זה אבי כהן, תגיד לחזקי ששלחתי את המסמכים."
        You: "קיבלתי אבי. אני מעדכן את עו\"ד חסקי שהמסמכים נשלחו."
        (Tool Action: classification="EXISTING")

        --- Ex 2: New Lead ---
        User: "איך מתחילים הליך גירושין?"
        You: "צריך ייעוץ משפטי לבנות אסטרטגיה. עו\"ד חסקי מומחה בזה. מה שמך?"
        User: "דנה"
        You: "נעים מאוד דנה. רשמתי את הפרטים."
        (Tool Action: classification="NEW_LEAD")

        --- Ex 3: URGENT / PANIC ---
        User: "דחוףףף המשטרה בדרך לפה בעלי השתגע!!"
        You: "אני מבין שזה חירום! אני מקפיץ הודעה דחופה לעו\"ד חסקי. מה שמך המלא?"
        User: "רינת לוי"
        You: "רשמתי רינת. מטופל בדחיפות."
        (Tool Action: classification="URGENT")
        -------------------------------------------

        **PROTOCOL:**
        1. **Check:** Did user provide Name? If YES -> Don't ask again.
        2. **Classify:** Decide if URGENT, EXISTING, or NEW_LEAD.
        3. **Action:** Call `save_case_summary(name, topic, summary, phone, classification)`.
        
        **RULES:**
        * Do NOT ask for phone number.
        * Short answers (1-2 sentences).
        """
        
        self.model = genai.GenerativeModel('gemini-2.0-flash', tools=self.tools)
        self.active_chats = {}

    def chat(self, user_id, user_msg):
        if user_id not in self.active_chats:
            self.active_chats[user_id] = self.model.start_chat(enable_automatic_function_calling=True)
            self.active_chats[user_id].send_message(f"SYSTEM INSTRUCTION: {self.system_instruction}")
            
        context_msg = f"[System Data - Phone: {user_id}] User says: {user_msg}"
        
        try:
            response = self.active_chats[user_id].send_message(context_msg)
            if not response.text:
                return "הפרטים הועברו בהצלחה לעו\"ד חסקי. נחזור אליך בהקדם."
            return response.text
        except Exception as e:
            logger.error(f"AI Error: {e}")
            return "תודה. רשמתי את ההודעה ואני מעביר אותה לעו\"ד חסקי כעת."

# --- 5. LOGIC ---
agent = GeminiAgent()
user_sessions = {}
last_auto_replies = {} 

@app.route("/status", methods=['POST'])
def status(): 
    status = request.values.get('DialCallStatus', '')
    raw_caller = request.values.get('From', '')
    if raw_caller and not raw_caller.startswith('whatsapp:'): caller = f"whatsapp:{raw_caller}"
    else: caller = raw_caller

    if status in ['no-answer', 'busy', 'failed', 'canceled'] or request.values.get('CallStatus') == 'ringing':
        if caller in Config.VIP_NUMBERS: return str(VoiceResponse())
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600): return str(VoiceResponse())
        try:
            twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, to=caller, content_sid=Config.CONTENT_SID)
            last_auto_replies[caller] = now
        except:
            try: twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, to=caller, body="שלום, הגעתם למשרד עו\"ד שמעון חסקי. אנו בשיחה כרגע. איך אפשר לעזור?")
            except: pass
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    if incoming_msg.lower() == "reset":
        if sender in user_sessions: del user_sessions[sender]
        if sender in agent.active_chats: del agent.active_chats[sender]
        user_sessions[sender] = 'START'
        send_menu(sender, "🔄 *System Reset Success.*\n\n" + Config.FLOW_STATES['START']['message'], Config.FLOW_STATES['START']['options'])
        return str(MessagingResponse())

    if sender not in user_sessions: 
        user_sessions[sender] = 'START'
        send_menu(sender, Config.FLOW_STATES['START']['message'], Config.FLOW_STATES['START']['options'])
        return str(MessagingResponse())

    if incoming_msg.isdigit():
        idx = int(incoming_msg) - 1
        options = Config.FLOW_STATES['START']['options']
        if 0 <= idx < len(options):
            selected = options[idx]
            if selected['next'] == 'AI_MODE_SUMMARY':
                user_sessions[sender] = 'AI_MODE'
                try:
                    reply = agent.chat(sender, f"The user selected {selected['label']}. Be professional.")
                    send_msg(sender, reply)
                except: send_msg(sender, "במה אוכל לעזור?")
                return str(MessagingResponse())
            elif selected['next'] == 'ASK_BOOKING':
                user_sessions[sender] = 'ASK_BOOKING'
                send_msg(sender, Config.FLOW_STATES['ASK_BOOKING']['message'])
                return str(MessagingResponse())
            elif selected['next'] == 'AI_MODE':
                user_sessions[sender] = 'AI_MODE'
                send_msg(sender, "שלום. אני העוזר הדיגיטלי. במה אפשר לעזור?")
                return str(MessagingResponse())

    if user_sessions[sender] == 'ASK_BOOKING':
        book_meeting(sender, "Manual Booking")
        send_msg(sender, Config.FLOW_STATES['FINISH_BOOKING']['message'])
        user_sessions[sender] = 'START'
        return str(MessagingResponse())

    reply = agent.chat(sender, incoming_msg)
    send_msg(sender, reply)
    return str(MessagingResponse())

def send_menu(to, body, options):
    if not twilio_mgr: return
    try:
        rows = [{"id": opt["label"], "title": opt["label"][:24]} for opt in options]
        payload = {"type": "list", "header": {"type": "text", "text": "תפריט"}, "body": {"text": body}, "action": {"button": "בחירה", "sections": [{"title": "אפשרויות", "rows": rows}]}}
        twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, to=to, body=body, persistent_action=[json.dumps(payload)])
    except: send_msg(to, body)

def send_msg(to, body):
    if twilio_mgr: twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=body, to=to)

@app.route("/", methods=['GET'])
def keep_alive(): return "I am alive!", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)