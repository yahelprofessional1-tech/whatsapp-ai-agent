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
    # 📝 THE MENU FILE (Change this for different clients!)
    FLOW_FILE = "Flows/FamilyLaw.json" 
    
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    LAWYER_PHONE = os.getenv('LAWYER_PHONE')
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = 'whatsapp:+14155238886'
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    
    # 🛡️ PROTECTION
    VIP_NUMBERS = [LAWYER_PHONE, "whatsapp:+972500000000"]
    COOL_DOWN_HOURS = 24

# --- 2. FLOW ENGINE (The "Template" Brain) ---
def get_flow_data():
    """Reads the JSON menu file safely."""
    if not os.path.exists(Config.FLOW_FILE):
        logger.error(f"❌ Missing Flow File: {Config.FLOW_FILE}")
        return None
    with open(Config.FLOW_FILE, encoding="utf-8") as f:
        return json.load(f)["states"]

def get_state(state_name):
    flow = get_flow_data()
    return flow.get(state_name) if flow else None

# --- 3. GOOGLE MANAGER (Sheets + Calendar) ---
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
            
            # Connect Sheets
            gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
            self.sheet = gc.open_by_key(Config.SHEET_ID).sheet1
            
            # Connect Calendar
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

# --- 4. TWILIO MANAGER (Messages + Lists) ---
class TwilioManager:
    def __init__(self):
        try: self.client = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN)
        except: self.client = None

    def send_whatsapp(self, to, body):
        if self.client: 
            try: self.client.messages.create(from_=Config.TWILIO_NUMBER, body=body, to=to)
            except: pass

    def send_interactive_message(self, to, body_text, options):
        """Smartly decides between Buttons (1-3) or List Menu (>3)"""
        if not self.client: return
        
        try:
            # 1. LIST MENU (For many options)
            if len(options) > 3:
                rows = [{"id": opt["label"], "title": opt["label"][:24], "description": ""} for opt in options]
                list_payload = {
                    "type": "list",
                    "header": {"type": "text", "text": "תפריט שירותים"},
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
            
            # 2. BUTTONS (For 1-3 options)
            elif len(options) > 0:
                buttons = [{"type": "reply", "reply": {"id": opt["label"], "title": opt["label"]}} for opt in options]
                button_payload = {
                    "type": "button",
                    "parameters": {"display_text": body_text, "buttons": buttons}
                }
                self.client.messages.create(
                    from_=Config.TWILIO_NUMBER, to=to, body=body_text,
                    persistent_action=[json.dumps(button_payload)]
                )
            
            # 3. PLAIN TEXT (No options)
            else:
                self.send_whatsapp(to, body_text)
                
        except Exception as e:
            logger.error(f"Twilio Send Error: {e}")
            self.send_whatsapp(to, body_text) # Fallback

# --- 5. LOGIC & ROUTES ---
google_mgr = GoogleManager()
twilio_mgr = TwilioManager()
user_sessions = {}
last_auto_replies = {}

@app.route("/incoming", methods=['POST'])
def incoming(): return str(MessagingResponse())

@app.route("/status", methods=['POST'])
def status(): 
    """Handles Missed Calls + VIP Logic + Cool Down"""
    from twilio.twiml.voice_response import VoiceResponse
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '')
    
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        # 1. VIP Check
        if caller in Config.VIP_NUMBERS: 
            logger.info(f"💎 VIP Call ({caller}) - Silent.")
            return str(VoiceResponse())
        
        # 2. Cool Down Check
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600):
            logger.info(f"⏳ Cool Down Active for {caller}")
            return str(VoiceResponse())

        # 3. Send "We're Busy" Message
        # We start the Menu Flow immediately for missed calls!
        start_state = get_state('START')
        twilio_mgr.send_interactive_message(caller, "הגעתם למשרד, אנו בשיחה.\n" + start_state['message'], start_state.get('options', []))
        
        last_auto_replies[caller] = now
        
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    # 1. Start Session
    if sender not in user_sessions:
        user_sessions[sender] = {'current_state': 'START', 'data': {}}
        # Send Menu
        state = get_state('START')
        twilio_mgr.send_interactive_message(sender, state['message'], state.get('options', []))
        return str(MessagingResponse())

    session = user_sessions[sender]
    current_state_name = session['current_state']
    state_data = get_state(current_state_name)

    # 2. Logic: Did they click a button or type text?
    next_state_name = None
    
    # A. Check Buttons
    options = state_data.get('options', [])
    for opt in options:
        if incoming_msg == opt['label'] or incoming_msg == opt.get('id'):
            next_state_name = opt['next']
            session['data']['topic'] = opt['label'] # Save topic
            break
    
    # B. Check Text (if allowed)
    if not next_state_name and state_data.get('allow_free_text'):
        next_state_name = state_data.get('next')
        # Contextual Saving
        if current_state_name == 'ASK_NAME': session['data']['name'] = incoming_msg
        elif current_state_name == 'ASK_DETAILS': session['data']['details'] = incoming_msg
        elif current_state_name == 'ASK_BOOKING': session['data']['details'] = incoming_msg

    # 3. Transition to Next State
    if next_state_name:
        session['current_state'] = next_state_name
        next_state = get_state(next_state_name)
        
        # ACTIONS
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
        # Invalid Input
        twilio_mgr.send_whatsapp(sender, "אנא בחר אחת מהאפשרויות בתפריט.")

    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(port=5000, debug=True)