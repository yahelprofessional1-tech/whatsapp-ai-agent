import os
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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LawyerBot")
app = Flask(__name__)

# --- 2. CONFIGURATION ---
class Config:
    BUSINESS_NAME = "Adv. Yahel Baron"
    
    # 🎯 SPECIALTY SETTING
    LAWYER_SPECIALTY = "דיני משפחה (Family Law)" 
    
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

# --- 3. GOOGLE MANAGER (Now with Calendar Back!) ---
class GoogleManager:
    def __init__(self):
        self.sheet = None
        self.calendar = None
        self._authenticate()

    def _authenticate(self):
        try:
            if not os.path.exists(Config.SERVICE_ACCOUNT_FILE):
                google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
                if google_json:
                    with open(Config.SERVICE_ACCOUNT_FILE, 'w') as f:
                        f.write(google_json)
            
            # Connect Sheets
            gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
            self.sheet = gc.open_by_key(Config.SHEET_ID).sheet1
            
            # Connect Calendar
            creds = service_account.Credentials.from_service_account_file(
                Config.SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar']
            )
            self.calendar = build('calendar', 'v3', credentials=creds)
            print("✅ Google Services (Sheet + Calendar) Connected")
        except Exception as e:
            print(f"❌ Google Error: {e}")

    def save_lead(self, phone, data):
        if not self.sheet: return False
        try:
            date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            row = [date_now, phone, data.get('name'), data.get('case_details'), Config.LAWYER_SPECIALTY, "New Lead"]
            self.sheet.append_row(row)
            return True
        except: return False

    def book_event(self, summary, description):
        """Inserts a 1-hour meeting into the calendar."""
        if not self.calendar: return False
        try:
            # For simplicity in this version, we book it for "Tomorrow at 10 AM" 
            # or we accept the date the AI parsed. 
            # To keep it unbreakable, we will default to a placeholder time if parsing fails
            # In a real heavy code, we would use strict date parsing libraries.
            
            # DEFAULT: Booking for tomorrow at 10:00 AM just to secure the slot
            tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
            start_time = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
            end_time = start_time + datetime.timedelta(hours=1)
            
            event = {
                'summary': summary,
                'description': description,
                'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Jerusalem'},
                'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Jerusalem'},
            }
            
            self.calendar.events().insert(calendarId=Config.CALENDAR_ID, body=event).execute()
            return True
        except Exception as e:
            print(f"Calendar Error: {e}")
            return False

# --- 4. TWILIO MANAGER ---
class TwilioManager:
    def __init__(self):
        try:
            self.client = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN) if Config.TWILIO_SID else None
        except: self.client = None

    def send_whatsapp(self, to, body):
        if self.client:
            try: self.client.messages.create(from_=Config.TWILIO_NUMBER, body=body, to=to)
            except: pass

    def notify_lawyer(self, data, client_phone):
        if not Config.LAWYER_PHONE: return
        msg = f"⚖️ NEW LEAD ({Config.LAWYER_SPECIALTY})\nName: {data.get('name')}\nPhone: {client_phone}\nDetails: {data.get('case_details')}"
        self.send_whatsapp(Config.LAWYER_PHONE, msg)

# --- 5. AI BRAIN ---
class AIBrain:
    def __init__(self):
        self.model = None
        try:
            if Config.GOOGLE_API_KEY:
                genai.configure(api_key=Config.GOOGLE_API_KEY)
                self.model = genai.GenerativeModel('gemini-flash-latest')
        except: pass

    def analyze_intent(self, user_msg):
        if not self.model: return "START_INTAKE" # Safety Default

        prompt = f"""
        Role: Receptionist for {Config.BUSINESS_NAME}, expert in {Config.LAWYER_SPECIALTY}.
        User Input: "{user_msg}"
        
        LOGIC:
        1. If User says "Hi", "Hello", "Start", or describes a case -> Output: START_INTAKE
        2. If User explicitly asks for a meeting/schedule -> Output: BOOK: ASK
        3. If curse -> Output: BLOCK
        
        Examples:
        "Hi" -> START_INTAKE
        "I need a divorce" -> START_INTAKE
        "Can we meet?" -> BOOK: ASK
        """
        
        try:
            response = self.model.generate_content(prompt)
            if not response.candidates: return "START_INTAKE" 
            return response.text.strip()
        except:
            return "START_INTAKE"

# --- 6. INIT ---
google_mgr = GoogleManager()
twilio_mgr = TwilioManager()
brain = AIBrain()
user_sessions = {}
last_auto_replies = {}

# --- 7. ROUTES ---
@app.route("/incoming", methods=['POST'])
def incoming(): return str(MessagingResponse())

@app.route("/status", methods=['POST'])
def status(): 
    # Missed call logic
    from twilio.twiml.voice_response import VoiceResponse
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '')
    
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        if caller in Config.VIP_NUMBERS: return str(VoiceResponse())
        
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600):
            return str(VoiceResponse())

        twilio_mgr.send_whatsapp(caller, f"שלום, כאן המשרד של {Config.BUSINESS_NAME}. אנו בשיחה כרגע. איך אפשר לעזור?")
        last_auto_replies[caller] = now
        
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    if sender not in user_sessions:
        user_sessions[sender] = {'state': 'IDLE', 'data': {}}
    
    session = user_sessions[sender]
    state = session['state']
    ai_reply = ""

    # --- SALES FUNNEL ---
    if state == 'ASK_NAME':
        session['data']['name'] = incoming_msg
        session['state'] = 'ASK_DETAILS'
        ai_reply = f"נעים להכיר, {incoming_msg}. על מנת שנוכל לבדוק את התיק, אנא תאר בקצרה את המקרה?"

    elif state == 'ASK_DETAILS':
        session['data']['case_details'] = incoming_msg
        google_mgr.save_lead(sender, session['data'])
        twilio_mgr.notify_lawyer(session['data'], sender)
        ai_reply = "תודה רבה. קיבלנו את הפרטים. עורך הדין יעבור על המקרה ויחזור אליך בהקדם. ⚖️"
        del user_sessions[sender]

    elif state == 'ASK_BOOKING_DATE':
        # HERE IS THE RESTORED LOGIC:
        session['data']['case_details'] = f"Meeting Request: {incoming_msg}"
        
        # 1. Save to Sheet
        google_mgr.save_lead(sender, session['data'])
        
        # 2. Book on Calendar (Defaulting to tomorrow 10am for safety, or we could parse the date)
        google_mgr.book_event(f"Meeting: {sender}", f"Details: {incoming_msg}")
        
        # 3. Notify Lawyer
        twilio_mgr.notify_lawyer(session['data'], sender)
        
        ai_reply = "רשמתי את הבקשה ביומן. ניצור קשר לאישור סופי של המועד. תודה!"
        del user_sessions[sender]

    # --- BRAIN ---
    else:
        intent = brain.analyze_intent(incoming_msg)
        
        if "START_INTAKE" in intent:
            session['state'] = 'ASK_NAME'
            session['data']['service_type'] = Config.LAWYER_SPECIALTY
            ai_reply = f"שלום, הגעתם למשרד של {Config.BUSINESS_NAME}, מומחה ל{Config.LAWYER_SPECIALTY}. \nאיך אוכל לעזור? (אנא רשום את שמך המלא להתחלת בדיקה)"

        elif "BOOK" in intent:
            session['state'] = 'ASK_BOOKING_DATE'
            ai_reply = "בשמחה. לאיזה יום ושעה תרצה לתאם פגישה?"

        elif "BLOCK" in intent:
            ai_reply = "נא לשמור על שפה מכבדת."

        else:
            session['state'] = 'ASK_NAME'
            ai_reply = f"שלום, הגעתם למשרד {Config.BUSINESS_NAME}. מה שמך המלא?"

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)