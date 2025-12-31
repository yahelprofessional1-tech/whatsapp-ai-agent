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
import gspread

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
SHEET_NAME = "Butcher Shop Orders"

# --- STRICT MENU ---
MENU_ITEMS = "בשר בקר, עוף, הודו, אווז"

# --- MEMORY STORAGE (The State Machine) ---
# Tracks where the user is: 'IDLE', 'ASK_NAME', 'ASK_ADDRESS'
user_sessions = {}

# --- SETUP CLIENTS ---
# 1. AI
try:
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest') 
except Exception as e:
    print(f"AI Warning: {e}")

# 2. Twilio
try:
    if TWILIO_SID and TWILIO_TOKEN:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
except Exception as e:
    print(f"Twilio Warning: {e}")

# 3. Google Services
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

# --- HELPER: SAVE FULL ORDER TO SHEET ---
def save_order_to_sheet(phone, data):
    if not sheet_service: return False
    try:
        date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        # Row: Date | Phone | Name | Address | Items | Status
        row = [
            date_now, 
            phone, 
            data.get('name'), 
            data.get('address'), 
            data.get('items'), 
            "Pending"
        ]
        sheet_service.append_row(row)
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
        # Note: We can't start a session from a missed call easily, just send greeting
        send_whatsapp(caller, "היי, הגעתם לאטליז בוארון. שמי אליס. איך אני יכולה לעזור?")
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

# --- ROUTE 2: WHATSAPP BRAIN (WITH MEMORY) ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    print(f"User: {incoming_msg}")

    # 1. CREATE SESSION IF NEW
    if sender not in user_sessions:
        user_sessions[sender] = {'state': 'IDLE', 'data': {}}
    
    session = user_sessions[sender]
    state = session['state']
    ai_reply = ""

    # --- STATE 1: ASKING FOR NAME ---
    if state == 'ASK_NAME':
        session['data']['name'] = incoming_msg
        session['state'] = 'ASK_ADDRESS' # Next Step
        ai_reply = f"נעים להכיר, {incoming_msg}. לאיזו כתובת לשלוח את ההזמנה?"

    # --- STATE 2: ASKING FOR ADDRESS ---
    elif state == 'ASK_ADDRESS':
        session['data']['address'] = incoming_msg
        
        # FINISH: Save to Sheet
        items = session['data'].get('items')
        name = session['data'].get('name')
        address = incoming_msg
        
        if save_order_to_sheet(sender, session['data']):
            ai_reply = f"תודה {name}! הזמנתך ({items}) לכתובת {address} התקבלה בהצלחה."
        else:
            ai_reply = "הייתה תקלה טכנית בשמירת ההזמנה. נא להתקשר."
        
        # Reset User (Clear memory so they can order again later)
        del user_sessions[sender]

    # --- STATE 3: IDLE (NORMAL CHAT) ---
    else:
        # Check if they want to order
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tool_prompt = f"""
        Current Time: {current_time}
        User Message: "{incoming_msg}"
        VALID MENU: {MENU_ITEMS}
        
        INSTRUCTIONS:
        1. If user wants to order VALID items -> Return JSON: {{"action": "order", "items": "..."}}
        2. If user wants to schedule/book -> Return JSON: {{"action": "book", "iso_time": "..."}}
        3. If user chats -> Return JSON: {{"action": "chat"}}
        4. If offensive -> Return JSON: {{"action": "block"}}
        """
        
        try:
            raw = model.generate_content(tool_prompt).text
            clean_json = raw.replace('```json', '').replace('```', '').strip()
            
            try:
                data = json.loads(clean_json)
                action = data.get("action", "chat")
            except:
                action = "chat"

            if action == "block":
                ai_reply = "נא לשמור על שפה מכבדת."

            # START ORDER FLOW
            elif action == "order":
                items = data.get("items")
                # Save items to memory
                session['state'] = 'ASK_NAME'
                session['data']['items'] = items
                ai_reply = f"בשמחה, רשמתי {items}. \nכדי להשלים את ההזמנה, מה השם שלך?"

            # CALENDAR BOOKING (No memory needed for now)
            elif action == "book":
                iso_time = data.get("iso_time")
                if book_meeting(f"Meeting: {sender}", iso_time):
                     # Just checking if the helper exists in this scope, yes it does
                    from googleapiclient.discovery import build # Re-import just in case inside function not needed
                    ai_reply = f"קבעתי לך פגישה לתאריך {iso_time}. נתראה!"
                else:
                    ai_reply = "הייתה תקלה ביומן."

            # NORMAL CHAT
            else:
                chat_prompt = f"""
                You are Alice (אליס), secretary at 'Boaron Butchery'.
                INSTRUCTIONS:
                1. Reply in HEBREW ONLY.
                2. Be direct and polite.
                3. Do NOT list the full menu unless asked.
                4. If they say "Hi", ask "What would you like to order?".
                User said: {incoming_msg}
                """
                ai_reply = model.generate_content(chat_prompt).text
                
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