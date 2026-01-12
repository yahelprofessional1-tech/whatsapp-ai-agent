import os
import json
import datetime
import logging
import smtplib
import ssl
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
    LAWYER_PHONE = os.getenv('LAWYER_PHONE')
    
    # 📧 EMAIL CONFIG
    EMAIL_SENDER = os.getenv('EMAIL_SENDER')
    EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')
    LAWYER_EMAIL = os.getenv('LAWYER_EMAIL')
    
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = 'whatsapp:+14155238886'
    
    # Backup Sheet
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    VIP_NUMBERS = [LAWYER_PHONE]
    COOL_DOWN_HOURS = 24
    
    # 📋 MENU
    FLOW_STATES = {
        "START": {
            "message": """שלום, הגעתם למשרד עורכי דין יעל ברון. ⚖️
אני העוזר החכם של המשרד.

אני כאן כדי לעזור לך לקדם את התיק במהירות.
תוכל לבחור נושא, או **לכתוב לי תקציר של המקרה שלך כבר עכשיו**.

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

# --- 3. TOOLS (EMAIL ENGINE) ---
twilio_mgr = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN) if Config.TWILIO_SID else None

def send_email_report(name, topic, summary):
    """Sends a professional HTML email to the lawyer."""
    # Check if variables exist in Render
    if not Config.EMAIL_SENDER or not Config.EMAIL_PASSWORD:
        return "Email Skipped (Missing Config)"
        
    msg = EmailMessage()
    msg['Subject'] = f"⚖️ תקציר תיק חדש: {name} - {topic}"
    msg['From'] = Config.EMAIL_SENDER
    msg['To'] = Config.LAWYER_EMAIL
    
    html_content = f"""
    <div dir="rtl" style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #2c3e50;">תקציר תיק חדש התקבל</h2>
        <hr>
        <p><strong>👤 שם הלקוח:</strong> {name}</p>
        <p><strong>📂 נושא:</strong> {topic}</p>
        <p><strong>📅 תאריך:</strong> {datetime.datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
        <div style="background-color: #f9f9f9; padding: 15px; border-radius: 5px; border-right: 5px solid #2c3e50;">
            <h3 style="margin-top: 0;">סיכום המקרה:</h3>
            <p style="white-space: pre-wrap;">{summary}</p>
        </div>
        <hr>
        <p style="font-size: 12px; color: #777;">נשלח אוטומטית ע"י העוזר החכם.</p>
    </div>
    """
    msg.add_alternative(html_content, subtype='html')

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as smtp:
            smtp.login(Config.EMAIL_SENDER, Config.EMAIL_PASSWORD)
            smtp.send_message(msg)
        return "Email Sent Successfully"
    except Exception as e:
        logger.error(f"Email Failed: {e}")
        return f"Email Failed: {e}"

def save_case_summary(name: str, topic: str, summary: str):
    try:
        # 1. Silent Backup to Sheets
        if os.path.exists(Config.SERVICE_ACCOUNT_FILE):
            try:
                gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
                sheet = gc.open_by_key(Config.SHEET_ID).sheet1
                sheet.append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "CASE SUMMARY", name, summary, topic, "Pending Review"])
            except: pass 

        # 2. Send the Email
        email_status = send_email_report(name, topic, summary)
        
        # 3. Notify Lawyer via WhatsApp
        if twilio_mgr and Config.LAWYER_PHONE:
            msg_body = f"📧 *נשלח מייל חדש!* ({topic})\n\n👤 *לקוח:* {name}\nהתקציר המלא ממתין לך במייל."
            twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=msg_body, to=Config.LAWYER_PHONE)
            
        return f"Success. Email: {email_status}"
    except Exception as e: return f"Error: {str(e)}"

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
        
        if twilio_mgr and Config.LAWYER_PHONE:
             twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=f"📅 *פגישה חדשה!* {client_name}", to=Config.LAWYER_PHONE)
        return "Success: Meeting booked for tomorrow at 10:00 AM."
    except Exception as e: return f"Error: {str(e)}"

# --- 4. AI AGENT ---
class GeminiAgent:
    def __init__(self):
        genai.configure(api_key=Config.GOOGLE_API_KEY)
        self.tools = [save_case_summary, book_meeting]
        
        # Instructions (Hidden from Init)
        self.system_instruction = f"""
        You are the Smart Intake Assistant for {Config.BUSINESS_NAME}.
        1. **Fast-Track:** If a user selects a topic, immediately ask if they want to write a short summary.
        2. **Gathering:** Listen to their story. Get their Name.
        3. **Action:** Use `save_case_summary` once you have the info.
        4. **Tone:** Professional Hebrew.
        """
        
        # ✅ USING 'gemini-2.0-flash' (High IQ)
        # Note: We do NOT pass system_instruction here to avoid the freeze bug.
        self.model = genai.GenerativeModel('gemini-2.0-flash', tools=self.tools)
        self.active_chats = {}

    def chat(self, user_id, user_msg):
        if user_id not in self.active_chats:
            # Start Chat
            self.active_chats[user_id] = self.model.start_chat(enable_automatic_function_calling=True)
            # 💉 INJECTION METHOD: The key to stability!
            self.active_chats[user_id].send_message(f"SYSTEM INSTRUCTION: {self.system_instruction}")
            
        return self.active_chats[user_id].send_message(user_msg).text

# --- 5. LOGIC ENGINE ---
agent = GeminiAgent()
user_sessions = {}
last_auto_replies = {} 

@app.route("/status", methods=['POST'])
def status(): 
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '')
    if status in ['no-answer', 'busy', 'failed', 'canceled'] or request.values.get('CallStatus') == 'ringing':
        if caller in Config.VIP_NUMBERS: return str(VoiceResponse())
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600):
            return str(VoiceResponse())
        state = Config.FLOW_STATES['START']
        send_menu(caller, "הגעתם למשרד, אנו בשיחה.\n" + state['message'], state['options'])
        last_auto_replies[caller] = now
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
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
                    reply = agent.chat(sender, f"The user selected {topic}. Offer them to write a summary.")
                    send_msg(sender, reply)
                except Exception as e:
                    send_msg(sender, f"AI Error: {str(e)}")
                return str(MessagingResponse())
            elif selected['next'] == 'ASK_BOOKING':
                user_sessions[sender] = 'ASK_BOOKING'
                send_msg(sender, Config.FLOW_STATES['ASK_BOOKING']['message'])
                return str(MessagingResponse())
            elif selected['next'] == 'AI_MODE':
                user_sessions[sender] = 'AI_MODE'
                send_msg(sender, "שלום! אני כאן. איך אפשר לעזור?")
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
        send_msg(sender, f"⚠️ סליחה, קרתה תקלה: {str(e)}")
        
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

if __name__ == "__main__":
    app.run(port=5000, debug=True)