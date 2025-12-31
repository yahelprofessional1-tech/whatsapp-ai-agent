import os
import json
import datetime
import time
import logging
import re
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread
from dotenv import load_dotenv

# --- 1. SYSTEM SETUP & LOGGING ---
load_dotenv()

# Configure professional logging (timestamps, error levels)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("LawyerBot")

app = Flask(__name__)

# --- 2. CONFIGURATION CLASS ---
class Config:
    BUSINESS_NAME = "Israeli Law Firm"
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM"
    MENU_ITEMS = "דיני עבודה, דיני משפחה, תעבורה, מקרקעין, פלילי, הוצאה לפועל"
    
    # Secrets
    LAWYER_PHONE = os.getenv('LAWYER_PHONE')
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = 'whatsapp:+14155238886'
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    
    # Logic Settings
    VIP_NUMBERS = [LAWYER_PHONE, "whatsapp:+972500000000"] # Add family here
    COOL_DOWN_HOURS = 24

# --- 3. GOOGLE SERVICES MANAGER ---
class GoogleManager:
    def __init__(self):
        self.creds = None
        self.sheet = None
        self.calendar = None
        self._authenticate()

    def _authenticate(self):
        """Internal method to handle authentication safely."""
        try:
            if not os.path.exists(Config.SERVICE_ACCOUNT_FILE):
                google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
                if google_json:
                    with open(Config.SERVICE_ACCOUNT_FILE, 'w') as f:
                        f.write(google_json)
                else:
                    logger.critical("❌ No Google Credentials found!")
                    return

            # Connect to Sheets
            gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
            self.sheet = gc.open_by_key(Config.SHEET_ID).sheet1
            
            # Connect to Calendar
            cal_scopes = ['https://www.googleapis.com/auth/calendar']
            self.creds = service_account.Credentials.from_service_account_file(
                Config.SERVICE_ACCOUNT_FILE, scopes=cal_scopes
            )
            self.calendar = build('calendar', 'v3', credentials=self.creds)
            
            logger.info("✅ Google Services Connected Successfully")
        except Exception as e:
            logger.error(f"❌ Google Auth Failed: {e}")

    def save_lead(self, phone, data):
        """Saves a lead to the sheet with retry logic."""
        if not self.sheet: return False
        try:
            date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            row = [
                date_now,
                phone,
                data.get('name', 'N/A'),
                data.get('case_details', 'N/A'),
                data.get('service_type', 'General'),
                "New Lead"
            ]
            self.sheet.append_row(row)
            logger.info(f"📝 Saved lead: {data.get('name')}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to save row: {e}")
            return False

    def book_event(self, summary, iso_time):
        """Books an event on the calendar."""
        if not self.calendar: return False
        try:
            start_dt = datetime.datetime.fromisoformat(iso_time)
            end_dt = start_dt + datetime.timedelta(hours=1)
            event = {
                'summary': summary,
                'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Jerusalem'},
                'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Jerusalem'},
            }
            self.calendar.events().insert(calendarId=Config.CALENDAR_ID, body=event).execute()
            logger.info(f"📅 Meeting booked for {iso_time}")
            return True
        except Exception as e:
            logger.error(f"❌ Calendar Error: {e}")
            return False

# --- 4. TWILIO MANAGER ---
class TwilioManager:
    def __init__(self):
        try:
            if Config.TWILIO_SID and Config.TWILIO_TOKEN:
                self.client = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN)
            else:
                self.client = None
        except Exception as e:
            logger.error(f"❌ Twilio Init Error: {e}")
            self.client = None

    def send_whatsapp(self, to_number, body_text):
        if not self.client: return
        try:
            self.client.messages.create(
                from_=Config.TWILIO_NUMBER,
                body=body_text,
                to=to_number
            )
        except Exception as e:
            logger.error(f"❌ Failed to send WhatsApp: {e}")

    def notify_lawyer(self, data, client_phone):
        """Sends the polished case file to the lawyer."""
        if not Config.LAWYER_PHONE: return
        
        report = f"""
⚖️ *NEW CLIENT CASE FILE*
📅 *Date:* {datetime.datetime.now().strftime("%d/%m/%Y")}

👤 *Client Details*
• *Name:* {data.get('name')}
• *Phone:* {client_phone}

📂 *Category:* {data.get('service_type')}

📝 *Case Description*
"{data.get('case_details')}"

🔻 *Action:* Pending Review
        """
        self.send_whatsapp(Config.LAWYER_PHONE, report)

# --- 5. THE AI BRAIN ---
class AIBrain:
    def __init__(self):
        try:
            if Config.GOOGLE_API_KEY:
                genai.configure(api_key=Config.GOOGLE_API_KEY)
                # Using the stable model as requested
                self.model = genai.GenerativeModel('gemini-flash-latest')
            else:
                self.model = None
        except Exception as e:
            logger.error(f"❌ AI Init Error: {e}")
            self.model = None

    def analyze_intent(self, user_msg):
        """
        Analyzes the user message and returns a structured command.
        Uses TEXT parsing to avoid JSON crashes.
        """
        if not self.model: return "ERROR"
        
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # EXTENSIVE PROMPT FOR MAXIMUM INTEL
        prompt = f"""
        CONTEXT:
        You are the receptionist for "{Config.BUSINESS_NAME}".
        Current Time: {current_time}
        Valid Services: {Config.MENU_ITEMS}
        
        USER MESSAGE: "{user_msg}"
        
        TASK:
        Classify the user's intent into exactly one of these categories:
        
        1. SERVICE (The user needs legal help/advice)
           Output: SERVICE: [Name of the Service]
           
        2. BOOK (The user explicitly asks to meet/schedule/come in)
           Output: BOOK: ASK
           
        3. CHAT (The user says "Hi", "Thanks", or general questions without a case yet)
           Output: CHAT: [Write a polite Hebrew response]
           
        4. BLOCK (The user is using offensive language)
           Output: BLOCK
           
        RULES:
        - If they tell a story about being fired -> SERVICE: דיני עבודה
        - If they mention divorce/kids -> SERVICE: דיני משפחה
        - If they ask "Can I make an appointment?" -> BOOK: ASK
        - Do not output JSON. Output the text format above.
        """
        
        try:
            response = self.model.generate_content(prompt)
            if not response.candidates:
                return "CHAT: שלום, איך אפשר לעזור?"
            
            return response.text.strip()
        except Exception as e:
            logger.error(f"Brain Error: {e}")
            return "CHAT: שלום, באיזה נושא אפשר לעזור?"

# --- 6. INITIALIZATION ---
# Initialize the engines once
google_mgr = GoogleManager()
twilio_mgr = TwilioManager()
brain = AIBrain()

# Memory Storage
user_sessions = {}
last_auto_replies = {}

# --- 7. ROUTE HANDLERS ---

@app.route("/incoming", methods=['POST'])
def incoming_call():
    """Handles incoming calls (voice)."""
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

@app.route("/status", methods=['POST'])
def call_status():
    """Handles Missed Call Logic with VIP filtering."""
    from twilio.twiml.voice_response import VoiceResponse
    
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '')
    
    # We only care if the call failed/busy/no-answer
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        
        # 1. VIP CHECK
        if caller in Config.VIP_NUMBERS:
            logger.info(f"💎 VIP Call detected ({caller}). Staying silent.")
            return str(VoiceResponse())

        # 2. COOL DOWN CHECK
        now = datetime.datetime.now()
        last_time = last_auto_replies.get(caller)
        if last_time:
            hours_diff = (now - last_time).total_seconds() / 3600
            if hours_diff < Config.COOL_DOWN_HOURS:
                logger.info(f"⏳ Skipping {caller} (Cool down active).")
                return str(VoiceResponse())
        
        # 3. SEND MESSAGE
        msg = "שלום, הגעתם למשרד עורכי דין. אנו כרגע בשיחה. איך אפשר לעזור?"
        twilio_mgr.send_whatsapp(caller, msg)
        last_auto_replies[caller] = now
        logger.info(f"📞 Sent missed call text to {caller}")

    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    """The Main Logic Engine."""
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    # Initialize Session
    if sender not in user_sessions:
        user_sessions[sender] = {'state': 'IDLE', 'data': {}}
    
    session = user_sessions[sender]
    state = session['state']
    ai_reply = ""
    
    logger.info(f"📩 Msg from {sender}: {incoming_msg} | State: {state}")

    # --- STATE MACHINE ---
    
    # 1. SALES FUNNEL: GET NAME
    if state == 'ASK_NAME':
        session['data']['name'] = incoming_msg
        session['state'] = 'ASK_DETAILS'
        ai_reply = f"נעים להכיר, {incoming_msg}. על מנת שנוכל לחזור אליך, אנא תאר בקצרה את נושא הפנייה?"

    # 2. SALES FUNNEL: GET DETAILS & SAVE
    elif state == 'ASK_DETAILS':
        session['data']['case_details'] = incoming_msg
        
        # SAVE TO DB
        success = google_mgr.save_lead(sender, session['data'])
        
        # NOTIFY BOSS
        twilio_mgr.notify_lawyer(session['data'], sender)
        
        if success:
            ai_reply = "תודה רבה. קיבלנו את הפרטים ועורך דין מטעמנו יצור קשר בהקדם."
        else:
            ai_reply = "תודה. הפרטים נרשמו אצלנו."
            
        # Clear session
        del user_sessions[sender]

    # 3. SALES FUNNEL: BOOKING DATE
    elif state == 'ASK_BOOKING_DATE':
        # Simple Logic: Just assume they gave a date for now, or ask AI to parse date
        # For robustness, we will send this to the lawyer to handle manually or implement complex date parsing later.
        session['data']['case_details'] = f"Meeting Request: {incoming_msg}"
        session['data']['service_type'] = "פגישה"
        
        google_mgr.save_lead(sender, session['data'])
        twilio_mgr.notify_lawyer(session['data'], sender)
        
        ai_reply = "רשמתי את הבקשה. נחזור אליך לאישור סופי של השעה."
        del user_sessions[sender]

    # 4. MAIN INTELLIGENCE (IDLE STATE)
    else:
        # Ask the Brain what to do
        intent_response = brain.analyze_intent(incoming_msg)
        
        # Parse the Brain's command
        if intent_response.startswith("SERVICE:"):
            service_item = intent_response.replace("SERVICE:", "").strip()
            session['state'] = 'ASK_NAME'
            session['data']['service_type'] = service_item
            ai_reply = f"אשמח לעזור בנושא {service_item}. \nכדי שנתקדם, מה שמך המלא?"

        elif intent_response.startswith("BOOK:"):
            session['state'] = 'ASK_BOOKING_DATE'
            ai_reply = "בשמחה. לאיזה יום ושעה תרצה לתאם פגישה?"

        elif intent_response.startswith("CHAT:"):
            ai_reply = intent_response.replace("CHAT:", "").strip()

        elif "BLOCK" in intent_response:
            ai_reply = "נא לשמור על שפה מכבדת."

        else:
            # Fallback for weird AI outputs
            ai_reply = intent_response

    # Send response back to Twilio
    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)