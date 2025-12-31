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

# --- 1. SYSTEM SETUP ---
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LawyerBot")
app = Flask(__name__)

# --- 2. CONFIGURATION ---
class Config:
    BUSINESS_NAME = "Israeli Law Firm"
    # Using the ID you provided in the screenshots
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    MENU_ITEMS = "דיני עבודה, דיני משפחה, תעבורה, מקרקעין, פלילי, הוצאה לפועל"
    
    LAWYER_PHONE = os.getenv('LAWYER_PHONE')
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = 'whatsapp:+14155238886'
    CALENDAR_ID = os.getenv('CALENDAR_ID')
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    
    VIP_NUMBERS = [LAWYER_PHONE, "whatsapp:+972500000000"]
    COOL_DOWN_HOURS = 24

# --- 3. GOOGLE MANAGER ---
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
            
            gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
            self.sheet = gc.open_by_key(Config.SHEET_ID).sheet1
            
            creds = service_account.Credentials.from_service_account_file(
                Config.SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar']
            )
            self.calendar = build('calendar', 'v3', credentials=creds)
            print("✅ Google Connected")
        except Exception as e:
            print(f"❌ Google Error: {e}")

    def save_lead(self, phone, data):
        if not self.sheet: return False
        try:
            date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            row = [date_now, phone, data.get('name'), data.get('case_details'), data.get('service_type'), "New Lead"]
            self.sheet.append_row(row)
            return True
        except: return False

# --- 4. TWILIO MANAGER ---
class TwilioManager:
    def __init__(self):
        try:
            if Config.TWILIO_SID:
                self.client = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN)
            else: self.client = None
        except: self.client = None

    def send_whatsapp(self, to, body):
        if self.client:
            try: self.client.messages.create(from_=Config.TWILIO_NUMBER, body=body, to=to)
            except: pass

    def notify_lawyer(self, data, client_phone):
        if not Config.LAWYER_PHONE: return
        msg = f"⚖️ NEW CASE\nName: {data.get('name')}\nPhone: {client_phone}\nType: {data.get('service_type')}\nDetails: {data.get('case_details')}"
        self.send_whatsapp(Config.LAWYER_PHONE, msg)

# --- 5. AI BRAIN (DEBUG MODE ENABLED) ---
class AIBrain:
    def __init__(self):
        self.model = None
        try:
            if Config.GOOGLE_API_KEY:
                genai.configure(api_key=Config.GOOGLE_API_KEY)
                # Reverting to the model that worked for you in the Butcher shop
                self.model = genai.GenerativeModel('gemini-flash-latest')
            else:
                print("❌ ERROR: GOOGLE_API_KEY is missing from .env!")
        except Exception as e:
            print(f"❌ AI Init Error: {e}")

    def analyze_intent(self, user_msg):
        # ⚠️ DEBUG CHECK: IS API KEY LOADED?
        if not self.model:
            return "CHAT: ⚠️ Error: AI Brain is missing (Check API Key)"

        prompt = f"""
        Act as a receptionist for {Config.BUSINESS_NAME}.
        Services: {Config.MENU_ITEMS}
        User says: "{user_msg}"
        
        Classify intent:
        1. SERVICE: [Service Name] (for legal help)
        2. BOOK: ASK (for meetings)
        3. CHAT: [Hebrew Response] (for greetings/chit-chat)
        4. BLOCK (for curses)
        
        Examples:
        "Divorce" -> SERVICE: דיני משפחה
        "Hi" -> CHAT: שלום, איך אפשר לעזור?
        """
        
        try:
            response = self.model.generate_content(prompt)
            if not response.candidates:
                return "CHAT: ⚠️ Error: AI returned empty response (Safety Filter?)"
            return response.text.strip()
        except Exception as e:
            # 🚨 THIS WILL PRINT THE ERROR TO WHATSAPP 🚨
            return f"CHAT: ⚠️ CRITICAL ERROR: {str(e)}"

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
def status(): return str(MessagingResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    if sender not in user_sessions:
        user_sessions[sender] = {'state': 'IDLE', 'data': {}}
    
    session = user_sessions[sender]
    state = session['state']
    ai_reply = ""

    if state == 'ASK_NAME':
        session['data']['name'] = incoming_msg
        session['state'] = 'ASK_DETAILS'
        ai_reply = f"נעים להכיר, {incoming_msg}. על מנת שנוכל לחזור אליך, אנא תאר בקצרה את נושא הפנייה?"

    elif state == 'ASK_DETAILS':
        session['data']['case_details'] = incoming_msg
        google_mgr.save_lead(sender, session['data'])
        twilio_mgr.notify_lawyer(session['data'], sender)
        ai_reply = "תודה רבה. קיבלנו את הפרטים ועורך דין מטעמנו יצור קשר בהקדם."
        del user_sessions[sender]

    elif state == 'ASK_BOOKING_DATE':
        session['data']['case_details'] = f"Meeting Request: {incoming_msg}"
        session['data']['service_type'] = "פגישה"
        google_mgr.save_lead(sender, session['data'])
        twilio_mgr.notify_lawyer(session['data'], sender)
        ai_reply = "רשמתי את הבקשה. נחזור אליך לאישור סופי של השעה."
        del user_sessions[sender]

    else:
        # BRAIN ANALYSIS
        intent_response = brain.analyze_intent(incoming_msg)
        
        if intent_response.startswith("SERVICE:"):
            service = intent_response.replace("SERVICE:", "").strip()
            session['state'] = 'ASK_NAME'
            session['data']['service_type'] = service
            ai_reply = f"אשמח לעזור בנושא {service}. \nכדי שנתקדם, מה שמך המלא?"

        elif intent_response.startswith("BOOK:"):
            session['state'] = 'ASK_BOOKING_DATE'
            ai_reply = "בשמחה. לאיזה יום ושעה תרצה לתאם פגישה?"

        elif intent_response.startswith("CHAT:"):
            ai_reply = intent_response.replace("CHAT:", "").strip()

        elif "BLOCK" in intent_response:
            ai_reply = "נא לשמור על שפה מכבדת."

        else:
            ai_reply = intent_response

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)