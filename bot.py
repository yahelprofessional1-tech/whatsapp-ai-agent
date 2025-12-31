import os
import json
import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- CONFIGURATION ---
BUSINESS_NAME = "Boaron Butchery"
# WE USE THE ID NOW (This fixes the "Technical Error")
SHEET_ID = "1GuXkaBAUfswXwA1uwytrouqhepOASyW35h4GVaC5bQ0" 
MENU_ITEMS = "בשר בקר, עוף, הודו, אווז" 

# --- MEMORY STORAGE (State Machine) ---
# Tracks: 'IDLE', 'ASK_NAME', 'ASK_ADDRESS'
user_sessions = {}

# --- CREDENTIALS ---
if not os.path.exists('credentials.json'):
    google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if google_json:
        with open('credentials.json', 'w') as f:
            f.write(google_json)

TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = 'credentials.json'

# --- 1. SETUP AI ---
try:
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest')
except: print("AI Error")

# --- 2. SETUP GOOGLE SERVICES ---
calendar_service = None
sheet_service = None

try:
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        # Calendar
        cal_scopes = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=cal_scopes)
        calendar_service = build('calendar', 'v3', credentials=creds)
        
        # Sheets (OPEN BY ID FIX)
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sheet_service = gc.open_by_key(SHEET_ID).sheet1
        print("✅ Services Connected!")
except Exception as e: 
    print(f"❌ Google Error: {e}")

# --- HELPERS ---
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
    except: return False

def save_order_to_sheet(phone, data):
    if not sheet_service: return False
    try:
        date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        # Row: Date | Phone | Name | Address | Items | Status
        row = [date_now, phone, data.get('name'), data.get('address'), data.get('items'), "Pending"]
        sheet_service.append_row(row)
        return True
    except Exception as e:
        print(f"Write Error: {e}")
        return False

# --- ROUTES ---
@app.route("/incoming", methods=['POST'])
def incoming_call():
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    # 1. CREATE SESSION
    if sender not in user_sessions:
        user_sessions[sender] = {'state': 'IDLE', 'data': {}}
    
    session = user_sessions[sender]
    state = session['state']
    ai_reply = ""

    # --- STATE 1: ASK NAME ---
    if state == 'ASK_NAME':
        session['data']['name'] = incoming_msg
        session['state'] = 'ASK_ADDRESS' 
        ai_reply = f"נעים להכיר, {incoming_msg}. לאיזו כתובת לשלוח את ההזמנה?"

    # --- STATE 2: ASK ADDRESS ---
    elif state == 'ASK_ADDRESS':
        session['data']['address'] = incoming_msg
        
        # FINISH: Save
        if save_order_to_sheet(sender, session['data']):
            ai_reply = "תודה רבה! ההזמנה נרשמה בהצלחה. יום טוב!"
        else:
            ai_reply = "הייתה תקלה טכנית. נא להתקשר."
        
        del user_sessions[sender]

    # --- STATE 3: IDLE (STRICT MENU LOGIC) ---
    else:
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tool_prompt = f"""
        Current Time: {current_time}
        User Message: "{incoming_msg}"
        VALID MENU (HEBREW): {MENU_ITEMS}
        
        INSTRUCTIONS:
        1. FILTER: Is the user trying to order something? 
           - If YES: Check if items are in VALID MENU.
           - IF VALID -> Return action="order", items="...".
           - IF INVALID (Pizza, Elephant) -> Return action="chat" (Do NOT order).
        
        2. BOOKING: If asking to meet/schedule -> action="book".
        3. CHAT: General chat or invalid orders -> action="chat".
        4. BLOCK: Offensive -> action="block".
        
        Output JSON ONLY:
        Ex: {{"action": "order", "items": "2kg Entrecote"}}
        """
        
        try:
            raw = model.generate_content(tool_prompt).text
            clean_json = raw.replace('```json', '').replace('```', '').strip()
            data = json.loads(clean_json)
            action = data.get("action", "chat")
            
            if action == "block":
                ai_reply = "נא לשמור על שפה מכבדת."

            elif action == "book":
                iso_time = data.get("iso_time")
                if book_meeting(f"Meeting: {sender}", iso_time):
                    ai_reply = f"קבעתי לך פגישה לתאריך {iso_time}."
                else:
                    ai_reply = "הייתה תקלה ביומן."

            elif action == "order":
                # START SALES FUNNEL
                session['state'] = 'ASK_NAME'
                session['data']['items'] = data.get("items")
                ai_reply = f"בשמחה, רשמתי {data.get('items')}. \nכדי להשלים, מה השם שלך?"
            
            else: # Normal Chat (Direct)
                chat_prompt = f"""
                You are Alice at {BUSINESS_NAME}.
                Reply in Hebrew. Be direct.
                If they say "Hi", ask "What would you like to order?".
                User: {incoming_msg}
                """
                ai_reply = model.generate_content(chat_prompt).text
        except:
            ai_reply = "אני בודקת..."

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)