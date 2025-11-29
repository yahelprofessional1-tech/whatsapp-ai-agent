import os
import json
import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai
from dotenv import load_dotenv # <--- New line
load_dotenv()                  # <--- New line

import os
import json
# ... rest of your code ...
# --- CALENDAR IMPORTS ---
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
MY_REAL_PHONE = os.getenv('MY_REAL_PHONE')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')

# Calendar Config
SERVICE_ACCOUNT_FILE = 'credentials.json'
CALENDAR_ID = os.getenv('CALENDAR_ID')
# --- SETUP CLIENTS ---
# 1. AI
try:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')
except:
    print("AI Warning")

# 2. Twilio
client = Client(TWILIO_SID, TWILIO_TOKEN)

# 3. Calendar
try:
    SCOPES = ['https://www.googleapis.com/auth/calendar']
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    calendar_service = build('calendar', 'v3', credentials=creds)
    print("✅ Calendar Connected!")
except Exception as e:
    print(f"❌ Calendar Error: {e}")

# --- HELPER FUNCTION: BOOK MEETING ---
def book_meeting(event_summary, event_time_iso):
    """
    Writes to Google Calendar.
    event_time_iso must be string like: '2025-12-01T15:00:00'
    """
    try:
        # Convert string to datetime object to calculate end time (1 hour later)
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
    # (Same logic as before - we just forward calls)
    from twilio.twiml.voice_response import VoiceResponse, Dial
    resp = VoiceResponse()
    dial = Dial(action='/status', timeout=20) 
    dial.number(MY_REAL_PHONE)
    resp.append(dial)
    return str(resp)

@app.route("/status", methods=['POST'])
def call_status():
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '') 
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        # Send first message (Hebrew)
        send_whatsapp(caller, "היי, הגעתם לאטליז בוארון. שמי אליס. איך אני יכולה לעזור?")
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

# --- ROUTE 2: WHATSAPP BRAIN (THE UPGRADE) ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    # Get current time so the AI knows what 'tomorrow' means
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"User: {incoming_msg}")

    # --- STEP 1: DETECT INTENT (Does user want to book?) ---
    # We ask Gemini to act as a data extractor first.
    tool_prompt = f"""
    Current Time: {current_time}
    User Message: "{incoming_msg}"
    
    Analyze the user's message.
    1. If they want to schedule/book/reserve something, extract the DATE and TIME in ISO format (YYYY-MM-DDTHH:MM:SS).
    2. If they are just chatting, return "CHAT".
    
    Output format: JSON ONLY.
    Example 1: {{"action": "book", "iso_time": "2025-12-05T14:00:00", "topic": "Meeting"}}
    Example 2: {{"action": "chat"}}
    """
    
    try:
        # Ask AI to analyze intent
        tool_response = model.generate_content(tool_prompt).text
        # Clean up AI response (sometimes it adds ```json markers)
        tool_response = tool_response.replace('```json', '').replace('```', '').strip()
        data = json.loads(tool_response)
        
        ai_reply = ""
        
        # --- CASE A: BOOKING ---
        if data.get("action") == "book":
            iso_time = data.get("iso_time")
            topic = f"Meeting with {sender}"
            
            # Trigger the Python Function!
            success = book_meeting(topic, iso_time)
            
            if success:
                ai_reply = f"מעולה, קבעתי לך פגישה לתאריך {iso_time}. נתראה!"
            else:
                ai_reply = "הייתה לי בעיה לקבוע את הפגישה. נסה שוב או תתקשר."

        # --- CASE B: JUST CHATTING ---
        else:
            # Normal Alice Logic
            chat_prompt = f"""
            You are Alice (אליס), secretary at 'Boaron Butchery'.
            
            INSTRUCTIONS:
            1. Reply in HEBREW ONLY(Exept if the user speeks to you in english). 
            2. Use Hebrew script (Aleph-Bet) exclusively. 
            3. Do NOT use English characters or transliteration.
            4. Keep the tone calm, human, and short.
            
            User said: {incoming_msg}
            """
            response = model.generate_content(chat_prompt)
            ai_reply = response.text

    except Exception as e:
        print(f"Error: {e}")
        ai_reply = "אני בודקת..."

    # Send result back
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