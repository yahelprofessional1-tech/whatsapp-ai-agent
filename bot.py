import os
import json
import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai

# --- GOOGLE IMPORTS ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread # <--- NEW for Sheets

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# --- CLOUD FIX: RE-CREATE CREDENTIALS FILE ---
if not os.path.exists('credentials.json'):
    google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if google_json:
        with open('credentials.json', 'w') as f:
            f.write(google_json)

# --- CONFIGURATION ---
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
MY_REAL_PHONE = os.getenv('MY_REAL_PHONE') 
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = 'credentials.json'
SHEET_NAME = "Butcher Shop Orders" # <--- Must match your Sheet Name exactly

# --- SETUP CLIENTS ---
# 1. AI
try:
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest') # Safe model
except Exception as e:
    print(f"AI Warning: {e}")

# 2. Twilio
try:
    if TWILIO_SID and TWILIO_TOKEN:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
except Exception as e:
    print(f"Twilio Warning: {e}")

# 3. Google Services (Calendar + Sheets)
calendar_service = None
sheet_service = None

try:
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        # Setup Calendar
        cal_scopes = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=cal_scopes)
        calendar_service = build('calendar', 'v3', credentials=creds)
        
        # Setup Sheets
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sheet_service = gc.open(SHEET_NAME).sheet1
        print("✅ Google Services Connected!")
    else:
        print("❌ Error: credentials.json missing.")
except Exception as e:
    print(f"❌ Google Error: {e}")

# --- HELPER 1: BOOK MEETING ---
def book_meeting(event_summary, event_time_iso):
    if not calendar_service: return False
    try:
        start_dt = datetime.datetime.fromisoformat(event_time_iso)
        end_dt = start_dt + datetime.timedelta(hours=1)
        event = {
            'summary': event_summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Jerusalem'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Jerusalem'},
        }
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return True
    except Exception as e:
        print(f"Booking failed: {e}")
        return False

# --- HELPER 2: WRITE ORDER TO SHEET ---
def write_order(customer_phone, order_items):
    if not sheet_service: return False
    try:
        # Row: Date | Phone | Items | Status
        date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        sheet_service.append_row([date_now, customer_phone, order_items, "Pending"])
        return True
    except Exception as e:
        print(f"Sheet failed: {e}")
        return False

# --- ROUTE 1: MISSED CALLS ---
@app.route("/incoming", methods=['POST'])
def incoming_call():
    from twilio.twiml.voice_response import VoiceResponse, Dial
    resp = VoiceResponse()
    dial = Dial(action='/status', timeout=20) 
    if MY_REAL_PHONE: dial.number(MY_REAL_PHONE)
    resp.append(dial)
    return str(resp)

@app.route("/status", methods=['POST'])
def call_status():
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '') 
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        send_whatsapp(caller, "היי, הגעתם לאטליז בוארון. שמי אליס. איך אני יכולה לעזור?")
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

# --- ROUTE 2: WHATSAPP BRAIN ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"User: {incoming_msg}")

    # --- STEP 1: DETECT INTENT ---
    tool_prompt = f"""
    Current Time: {current_time}
    User Message: "{incoming_msg}"
    
    Analyze intent:
    1. BOOKING (Meeting/Visit): Extract ISO time. Return action="book".
    2. ORDER (Buying meat/food): Extract the items list. Return action="order".
    3. CHAT (Questions): Return action="chat".
    
    Output JSON ONLY:
    Ex 1: {{"action": "book", "iso_time": "2025-12-05T14:00:00"}}
    Ex 2: {{"action": "order", "items": "2kg Entrecote and 10 Kebabs"}}
    Ex 3: {{"action": "chat"}}
    """
    
    try:
        raw = model.generate_content(tool_prompt).text
        clean_json = raw.replace('```json', '').replace('```', '').strip()
        
        try:
            data = json.loads(clean_json)
            action = data.get("action", "chat")
        except:
            action = "chat"
        
        ai_reply = ""
        
        # --- ACTION: BOOKING ---
        if action == "book":
            iso_time = data.get("iso_time")
            if book_meeting(f"Meeting: {sender}", iso_time):
                ai_reply = f"קבעתי לך פגישה לתאריך {iso_time}. נתראה!"
            else:
                ai_reply = "הייתה תקלה ביומן."

        # --- ACTION: ORDER ---
        elif action == "order":
            items = data.get("items")
            if write_order(sender, items):
                ai_reply = f"קיבלתי את ההזמנה שלך: {items}. נעביר אותה להכנה!"
            else:
                ai_reply = "הייתה תקלה ברישום ההזמנה."

        # --- ACTION: CHAT ---
        else:
            chat_prompt = f"""
            You are Alice (אליס), secretary at 'Boaron Butchery'.
            Reply in Hebrew. Calm, human tone. Short answers.
            User said: {incoming_msg}
            """
            response = model.generate_content(chat_prompt)
            ai_reply = response.text

    except Exception as e:
        print(f"Error: {e}")
        ai_reply = "אני בודקת..."

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

def send_whatsapp(to_number, body_text):
    try:
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, body=body_text, to=to_number)
    except: pass

if __name__ == "__main__":
    app.run(port=5000, debug=True)