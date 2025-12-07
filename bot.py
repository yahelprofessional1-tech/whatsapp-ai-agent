import os
import json
import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai

# --- CALENDAR IMPORTS ---
from google.oauth2 import service_account
from googleapiclient.discovery import build

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# --- CLOUD FIX: RE-CREATE CREDENTIALS FILE ---
if not os.path.exists('credentials.json'):
    google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if google_json:
        print("Creating credentials.json from Environment Variable...")
        with open('credentials.json', 'w') as f:
            f.write(google_json)
    else:
        print("WARNING: GOOGLE_CREDENTIALS_JSON not found!")

# --- CONFIGURATION ---
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
MY_REAL_PHONE = os.getenv('MY_REAL_PHONE') 
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = 'credentials.json'

# --- SETUP CLIENTS ---
# 1. AI
try:
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest')
    else:
        print("AI Error: Missing Google API Key")
except Exception as e:
    print(f"AI Warning: {e}")

# 2. Twilio
try:
    if TWILIO_SID and TWILIO_TOKEN:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
except Exception as e:
    print(f"Twilio Warning: {e}")

# 3. Calendar
calendar_service = None
try:
    SCOPES = ['https://www.googleapis.com/auth/calendar']
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        calendar_service = build('calendar', 'v3', credentials=creds)
        print("✅ Calendar Connected!")
    else:
        print("❌ Calendar Error: credentials.json still missing.")
except Exception as e:
    print(f"❌ Calendar Error: {e}")

# --- HELPER FUNCTION: BOOK MEETING ---
def book_meeting(event_summary, event_time_iso):
    if not calendar_service:
        return False
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

# --- ROUTE 1: MISSED CALLS ---
@app.route("/incoming", methods=['POST'])
def incoming_call():
    from twilio.twiml.voice_response import VoiceResponse, Dial
    resp = VoiceResponse()
    dial = Dial(action='/status', timeout=20) 
    if MY_REAL_PHONE:
        dial.number(MY_REAL_PHONE)
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
# שיפרנו את ההוראות כדי שהבוט לא יתבלבל
    tool_prompt = f"""
    Current Time: {current_time}
    User Message: "{incoming_msg}"
    
    Analyze the user's message.
    1. If they want to schedule/book/reserve, extract ISO time.
    2. If they are just chatting (hello, question, etc.), return action="chat".
    
    Output format: JSON ONLY. Do not write normal text.
    
    Example 1 (Booking): {{"action": "book", "iso_time": "2025-12-05T14:00:00"}}
    Example 2 (Chatting): {{"action": "chat"}}
    """
    
    try:
        tool_response = model.generate_content(tool_prompt).text
        tool_response = tool_response.replace('```json', '').replace('```', '').strip()
        data = json.loads(tool_response)
        
        ai_reply = ""
        if data.get("action") == "book":
            iso_time = data.get("iso_time")
            topic = f"Meeting with {sender}"
            success = book_meeting(topic, iso_time)
            if success:
                ai_reply = f"מעולה, קבעתי לך פגישה לתאריך {iso_time}. נתראה!"
            else:
                ai_reply = "הייתה לי בעיה לקבוע את הפגישה. נסה שוב או תתקשר."
        else:
            chat_prompt = f"""
            You are Alice (אליס), secretary at 'Boaron Butchery'.
            INSTRUCTIONS:
            1. Reply in HEBREW ONLY. 
            2. Use Hebrew script. 
            3. Keep it short.
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
        if not to_number.startswith('whatsapp:'):
            to_number = f"whatsapp:{to_number}"
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, body=body_text, to=to_number)
    except Exception as e:
        print(e)

if __name__ == "__main__":
    app.run(port=5000, debug=True)