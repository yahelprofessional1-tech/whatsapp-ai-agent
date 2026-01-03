import os
import json
import datetime
import logging
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
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
    
    # ✅ UPDATED MENU: Added numbers to the text so users ALWAYS see options
    FLOW_STATES = {
        "START": {
            "message": """שלום, הגעתם למשרד עורכי דין.
באיזה נושא אפשר לעזור? (אנא השב עם מספר):

1️⃣ גירושין
2️⃣ משמורת ילדים
3️⃣ הסכמי ממון
4️⃣ צוואות וירושות
5️⃣ תיאום פגישה במשרד""",
            "options": [
                { "label": "גירושין", "next": "ASK_NAME" },
                { "label": "משמורת ילדים", "next": "ASK_NAME" },
                { "label": "הסכמי ממון", "next": "ASK_NAME" },
                { "label": "צוואות וירושות", "next": "ASK_NAME" },
                { "label": "תיאום פגישה", "next": "ASK_BOOKING" }
            ]
        },
        "ASK_NAME": {
            "message": "אשמח לעזור בנושא זה. כדי שנתקדם, מה שמך המלא?",
            "allow_free_text": True,
            "next": "ASK_DETAILS"
        },
        "ASK_DETAILS": {
            "message": "נעים להכיר. אנא תאר בקצרה את המקרה או השאלה המשפטית שלך.",
            "allow_free_text": True,
            "next": "FINISH"
        },
        "ASK_BOOKING": {
            "message": "בשמחה. לאיזה יום ושעה תרצה לתאם פגישה?",
            "allow_free_text": True,
            "next": "FINISH_BOOKING"
        },
        "FINISH": {
            "message": "תודה רבה. קיבלנו את הפרטים ועורך הדין יחזור אליך בהקדם.",
            "action": "save_lead"
        },
        "FINISH_BOOKING": {
            "message": "רשמתי את הבקשה ביומן. ניצור קשר לאישור סופי. תודה!",
            "action": "book_meeting"
        }
    }
    
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    LAWYER_PHONE = os.getenv('LAWYER_PHONE')
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = 'whatsapp:+14155238886'
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    VIP_NUMBERS = [LAWYER_PHONE, "whatsapp:+972500000000"]
    COOL_DOWN_HOURS = 24

# --- 2. GOOGLE MANAGER ---
class GoogleManager:
    def __init__(self):
        self.sheet = None; self.calendar = None
        self._authenticate()
        
    def _authenticate(self):
        try:
            if not os.path.exists(Config.SERVICE_ACCOUNT_FILE):
                if os.getenv('GOOGLE_CREDENTIALS_JSON'):
                    with open(Config.SERVICE_ACCOUNT_FILE, 'w') as f: 
                        f.write(os.getenv('GOOGLE_CREDENTIALS_JSON'))
            gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
            self.sheet = gc.open_by_key(Config.SHEET_ID).sheet1
            creds = service_account.Credentials.from_service_account_file(
                Config.SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar']
            )
            self.calendar = build('calendar', 'v3', credentials=creds)
            logger.info("✅ Google Services Connected")
        except Exception as e:
            logger.error(f"❌ Google Error: {e}")

    def save_lead(self, phone, data):
        if not self.sheet: return
        try:
            row = [
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), 
                phone, 
                data.get('name', ''), 
                data.get('details', ''), 
                data.get('topic', 'General'), 
                "New"
            ]
            self.sheet.append_row(row)
            logger.info(f"📝 Lead Saved: {phone}")
        except Exception as e:
            logger.error(f"Save Error: {e}")

    def book_event(self, summary, description):
        if not self.calendar: return
        try:
            tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
            start = tomorrow.replace(hour=10, minute=0, second=0).isoformat()
            end = (tomorrow + datetime.timedelta(hours=1)).isoformat()
            event = {
                'summary': summary, 
                'description': description, 
                'start': {'dateTime': start, 'timeZone': 'Asia/Jerusalem'}, 
                'end': {'dateTime': end, 'timeZone': 'Asia/Jerusalem'}
            }
            self.calendar.events().insert(calendarId=Config.CALENDAR_ID, body=event).execute()
            logger.info("📅 Meeting Booked")
        except Exception as e:
            logger.error(f"Calendar Error: {e}")

# --- 3. TWILIO MANAGER ---
class TwilioManager:
    def __init__(self):
        try: self.client = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN)
        except: self.client = None

    def send_whatsapp(self, to, body):
        if self.client: 
            try: self.client.messages.create(from_=Config.TWILIO_NUMBER, body=body, to=to)
            except: pass

    def send_interactive_message(self, to, body_text, options):
        if not self.client: return
        try:
            # Always send text first (Backup)
            # self.send_whatsapp(to, body_text) # Optional: Send text separately if list fails often
            
            if options and len(options) > 3:
                rows = [{"id": opt["label"], "title": opt["label"][:24], "description": ""} for opt in options]
                list_payload = {
                    "type": "list",
                    "header": {"type": "text", "text": "תפריט"},
                    "body": {"text": body_text},
                    "footer": {"text": "בחר אפשרות 👇"},
                    "action": {
                        "button": "לחץ לבחירה",
                        "sections": [{"title": "אפשרויות", "rows": rows}]
                    }
                }
                self.client.messages.create(
                    from_=Config.TWILIO_NUMBER, to=to, body=body_text,
                    persistent_action=[json.dumps(list_payload)]
                )
            elif options and len(options) > 0:
                buttons = [{"type": "reply", "reply": {"id": opt["label"], "title": opt["label"]}} for opt in options]
                button_payload = {
                    "type": "button",
                    "parameters": {"display_text": body_text, "buttons": buttons}
                }
                self.client.messages.create(
                    from_=Config.TWILIO_NUMBER, to=to, body=body_text,
                    persistent_action=[json.dumps(button_payload)]
                )
            else:
                self.send_whatsapp(to, body_text)
        except Exception as e:
            logger.error(f"Twilio Send Error: {e}")
            self.send_whatsapp(to, body_text)

# --- 4. LOGIC & ROUTES ---
google_mgr = GoogleManager()
twilio_mgr = TwilioManager()
user_sessions = {}
last_auto_replies = {}

@app.route("/incoming", methods=['POST'])
def incoming(): return str(MessagingResponse())

@app.route("/status", methods=['POST'])
def status(): 
    from twilio.twiml.voice_response import VoiceResponse
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '')
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        if caller in Config.VIP_NUMBERS: return str(VoiceResponse())
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600):
            return str(VoiceResponse())
        state = Config.FLOW_STATES['START']
        twilio_mgr.send_interactive_message(caller, "הגעתם למשרד, אנו בשיחה.\n" + state['message'], state.get('options', []))
        last_auto_replies[caller] = now
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    try:
        incoming_msg = request.values.get('Body', '').strip()
        sender = request.values.get('From', '')
        
        if sender not in user_sessions:
            user_sessions[sender] = {'current_state': 'START', 'data': {}}
            state = Config.FLOW_STATES['START']
            twilio_mgr.send_interactive_message(sender, state['message'], state.get('options', []))
            return str(MessagingResponse())

        session = user_sessions[sender]
        current_state_name = session['current_state']
        state_data = Config.FLOW_STATES.get(current_state_name)

        if not state_data:
            session['current_state'] = 'START'
            state_data = Config.FLOW_STATES['START']

        next_state_name = None
        options = state_data.get('options', [])
        
        # ✅ NEW LOGIC: Support Numbers (1, 2, 3) AND Text
        if incoming_msg.isdigit():
            idx = int(incoming_msg) - 1
            if 0 <= idx < len(options):
                next_state_name = options[idx]['next']
                session['data']['topic'] = options[idx]['label']

        # Fallback: Check for Exact Match or Button Click
        if not next_state_name:
            for opt in options:
                if incoming_msg == opt['label'] or incoming_msg == opt.get('id'):
                    next_state_name = opt['next']
                    session['data']['topic'] = opt['label']
                    break
        
        if not next_state_name and state_data.get('allow_free_text'):
            next_state_name = state_data.get('next')
            if current_state_name == 'ASK_NAME': session['data']['name'] = incoming_msg
            elif current_state_name == 'ASK_DETAILS': session['data']['details'] = incoming_msg
            elif current_state_name == 'ASK_BOOKING': session['data']['details'] = incoming_msg

        if next_state_name:
            session['current_state'] = next_state_name
            next_state = Config.FLOW_STATES.get(next_state_name)
            
            if next_state.get('action') == 'save_lead':
                google_mgr.save_lead(sender, session['data'])
                twilio_mgr.send_whatsapp(Config.LAWYER_PHONE, f"⚖️ New Lead:\n{session['data']}")
                del user_sessions[sender]
                
            elif next_state.get('action') == 'book_meeting':
                google_mgr.book_event(f"Meeting: {sender}", session['data'].get('details'))
                google_mgr.save_lead(sender, session['data'])
                twilio_mgr.send_whatsapp(Config.LAWYER_PHONE, f"📅 Meeting Req:\n{session['data']}")
                del user_sessions[sender]

            twilio_mgr.send_interactive_message(sender, next_state['message'], next_state.get('options', []))
        
        else:
            twilio_mgr.send_whatsapp(sender, "אנא השב עם מספר האפשרות (למשל: 1).")

    except Exception as e:
        logger.error(f"CRITICAL ERROR: {e}")
        
    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(port=5000, debug=True)