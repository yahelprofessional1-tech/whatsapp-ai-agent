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
    BUSINESS_NAME = "Adv. Yahel Baron"
    
    # Phone Config
    _raw_phone = os.getenv('LAWYER_PHONE', '')
    if _raw_phone and not _raw_phone.startswith('whatsapp:'):
        LAWYER_PHONE = f"whatsapp:{_raw_phone}"
    else:
        LAWYER_PHONE = _raw_phone

    # Email Config
    EMAIL_SENDER = os.getenv('EMAIL_SENDER')
    _raw_pass = os.getenv('EMAIL_PASSWORD', '')
    EMAIL_PASSWORD = _raw_pass.replace(" ", "").strip()
    LAWYER_EMAIL = os.getenv('LAWYER_EMAIL')
    
    # API Keys
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = os.getenv('WHATSAPP_NUMBER')
    
    # Services
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    VIP_NUMBERS = [LAWYER_PHONE]
    COOL_DOWN_HOURS = 24
    
    # Menu Config
    FLOW_STATES = {
        "START": {
            "message": """שלום, הגעתם למשרד עורכי דין יהל ברון. ⚖️
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

# --- 2. KEY MAKER ---
def create_credentials():
    if not os.path.exists(Config.SERVICE_ACCOUNT_FILE):
        json_content = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if json_content:
            with open(Config.SERVICE_ACCOUNT_FILE, 'w') as f:
                f.write(json_content)

create_credentials()

# --- 3. TOOLS ---
twilio_mgr = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN) if Config.TWILIO_SID else None

def send_email_report(name, topic, summary, phone):
    if not Config.EMAIL_SENDER or not Config.EMAIL_PASSWORD:
        return "Skipped (Config Missing)"
        
    msg = EmailMessage()
    msg['Subject'] = f"⚖️ ליד חדש: {name} - {topic}"
    msg['From'] = Config.EMAIL_SENDER
    msg['To'] = Config.LAWYER_EMAIL
    
    msg.set_content(f"שם לקוח: {name}\nטלפון: {phone}\nנושא: {topic}\n\nתקציר המקרה:\n{summary}")

    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(Config.EMAIL_SENDER, Config.EMAIL_PASSWORD)
            smtp.send_message(msg)
        return "Email Sent"
    except Exception as e:
        logger.error(f"Email Failed: {e}")
        return "Email Disabled (Firewall)"

def save_case_summary(name: str, topic: str, summary: str, phone: str):
    """
    Saves summary + sends Magic Link to lawyer.
    """
    try:
        # Prepare Data
        clean_phone = phone.replace("whatsapp:", "")
        link_phone = clean_phone.replace("+", "") 
        wa_link = f"https://wa.me/{link_phone}"

        # 1. Sheets Save
        if os.path.exists(Config.SERVICE_ACCOUNT_FILE):
            try:
                gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
                sheet = gc.open_by_key(Config.SHEET_ID).sheet1
                sheet.append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "CASE SUMMARY", name, clean_phone, topic, summary])
            except: pass 

        # 2. Email Save
        send_email_report(name, topic, summary, clean_phone)
        
        # 3. WhatsApp Alert (With Magic Link)
        if twilio_mgr and Config.LAWYER_PHONE:
            msg_body = f"""📝 *ליד חדש התקבל!*
            
👤 *שם:* {name}
📌 *נושא:* {topic}
📄 *סיכום:* {summary}

👇 *לחץ כאן לשיחה עם הלקוח:*
{wa_link}"""
            
            twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=msg_body, to=Config.LAWYER_PHONE)
            
        return f"Details saved. Client: {name}, Phone: {clean_phone}"
    except Exception as e: return f"Error: {str(e)[:100]}"

def book_meeting(client_name: str, reason: str):
    try:
        if not os.path.exists(Config.SERVICE_ACCOUNT_FILE): create_credentials()
        creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar'])
        calendar = build('calendar', 'v3', credentials=creds)
        
        start = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0).isoformat()
        end = (datetime.datetime.now() + datetime.timedelta(days=1, hours=1)).replace(hour=10, minute=0, second=0).isoformat()
        
        event = {
            'summary': f"Meeting: {client_name}",
            'description': reason,
            'start': {'dateTime': start, 'timeZone': 'Asia/Jerusalem'},
            'end': {'dateTime': end, 'timeZone': 'Asia/Jerusalem'}
        }
        calendar.events().insert(calendarId=Config.CALENDAR_ID, body=event).execute()
        return "Success: Meeting booked for tomorrow at 10:00 AM."
    except Exception as e: return f"Error: {str(e)[:1200]}"

# --- 4. AI AGENT ---
class GeminiAgent:
    def __init__(self):
        genai.configure(api_key=Config.GOOGLE_API_KEY)
        self.tools = [save_case_summary, book_meeting]
        
        # SYSTEM INSTRUCTION: Strict, Concise, No Phone Questions
        self.system_instruction = f"""
        You are the Intake Assistant for {Config.BUSINESS_NAME}.
        
        **RULES:**
        1. **SPEAK HEBREW ONLY.**
        2. **BE CONCISE:** Write short sentences. Act like an efficient clerk. No fluff.
        3. **GET THE NAME:** You MUST ask for the client's name if they haven't said it.
        4. **NO PHONE QUESTIONS:** You ALREADY possess the user's phone number in the system context. **NEVER ASK FOR IT.**
        
        **PROTOCOL:**
        1. **Understand:** Ask 1-2 sharp questions to understand the legal issue.
        2. **Details:** Ensure you have the Name and the Issue.
        3. **Save:** When you have the details, call `save_case_summary`.
           - **CRITICAL:** Pass the 'phone' value provided in the system context to the function.
        
        **After saving:** Tell the user "הפרטים נשמרו. עו"ד ברון ייצור קשר בקרוב." and end the chat.
        """
        
        self.model = genai.GenerativeModel('gemini-2.0-flash', tools=self.tools)
        self.active_chats = {}

    def chat(self, user_id, user_msg):
        if user_id not in self.active_chats:
            self.active_chats[user_id] = self.model.start_chat(enable_automatic_function_calling=True)
            self.active_chats[user_id].send_message(f"SYSTEM INSTRUCTION: {self.system_instruction}")
            
        # SILENT INJECTION: Whispering the phone number to the AI
        context_msg = f"[System Data - Current User Phone: {user_id}] User says: {user_msg}"
        return self.active_chats[user_id].send_message(context_msg).text

# --- 5. LOGIC ENGINE ---
agent = GeminiAgent()
user_sessions = {}
last_auto_replies = {} 

@app.route("/status", methods=['POST'])
def status(): 
    """
    Handles incoming Voice Calls.
    If a call is missed/busy, sends a WhatsApp message.
    """
    status = request.values.get('DialCallStatus', '')
    raw_caller = request.values.get('From', '')

    # --- FIX: Convert regular phone number to WhatsApp format ---
    # Incoming call: +97250... -> WhatsApp Outgoing: whatsapp:+97250...
    if raw_caller and not raw_caller.startswith('whatsapp:'):
        caller = f"whatsapp:{raw_caller}"
    else:
        caller = raw_caller
    # ------------------------------------------------------------

    if status in ['no-answer', 'busy', 'failed', 'canceled'] or request.values.get('CallStatus') == 'ringing':
        if caller in Config.VIP_NUMBERS: return str(VoiceResponse())
        
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        # Cooldown check (don't spam if they call 5 times in a row)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600):
            return str(VoiceResponse())
            
        state = Config.FLOW_STATES['START']
        # Send the "We missed you" message
        send_menu(caller, "הגעתם למשרד, אנו בשיחה כרגע.\n" + state['message'], state['options'])
        last_auto_replies[caller] = now
        
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    # 🕵️ HIDDEN RESET BUTTON (Type "reset")
    if incoming_msg.lower() == "reset":
        if sender in user_sessions: del user_sessions[sender]
        if sender in agent.active_chats: del agent.active_chats[sender]
        user_sessions[sender] = 'START'
        state = Config.FLOW_STATES['START']
        send_menu(sender, "🔄 *System Reset Success.*\n\n" + state['message'], state['options'])
        return str(MessagingResponse())

    if sender not in user_sessions: 
        user_sessions[sender] = 'START'
        state = Config.FLOW_STATES['START']
        send_menu(sender, state['message'], state['options'])
        return str(MessagingResponse())

    current_state = user_sessions[sender]

    if incoming_msg.isdigit():
        idx = int(incoming_msg) - 1
        options = Config.FLOW_STATES['START']['options']
        if 0 <= idx < len(options):
            selected = options[idx]
            if selected['next'] == 'AI_MODE_SUMMARY':
                user_sessions[sender] = 'AI_MODE'
                topic = selected['label']
                try:
                    # Inject phone from start
                    start_prompt = f"[System Data - Current User Phone: {sender}] The user selected {topic}. Ask them for their name and a short summary."
                    reply = agent.chat(sender, start_prompt)
                    send_msg(sender, reply)
                except Exception as e:
                    send_msg(sender, f"AI Error: {str(e)[:1200]}")
                return str(MessagingResponse())
            elif selected['next'] == 'ASK_BOOKING':
                user_sessions[sender] = 'ASK_BOOKING'
                send_msg(sender, Config.FLOW_STATES['ASK_BOOKING']['message'])
                return str(MessagingResponse())
            elif selected['next'] == 'AI_MODE':
                user_sessions[sender] = 'AI_MODE'
                send_msg(sender, "שלום. אני העוזר הדיגיטלי. במה אפשר לעזור?")
                return str(MessagingResponse())

    if current_state == 'ASK_BOOKING':
        book_meeting(sender, "Manual Booking")
        send_msg(sender, Config.FLOW_STATES['FINISH_BOOKING']['message'])
        user_sessions[sender] = 'START'
        return str(MessagingResponse())

    try:
        reply = agent.chat(sender, incoming_msg)
        send_msg(sender, reply)
    except Exception as e:
        logger.error(f"AI Crash: {e}")
        send_msg(sender, f"⚠️ תקלה: {str(e)[:1200]}")
        
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

# --- UPTIME ROBOT KEEPER (Must be OUTSIDE the main block) ---
@app.route("/", methods=['GET'])
def keep_alive():
    return "I am alive!", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)