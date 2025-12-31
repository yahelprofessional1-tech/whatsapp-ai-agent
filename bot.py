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

# --- CONFIGURATION (LAWYER EDITION) ---
BUSINESS_NAME = "Israeli Law Firm"
SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
MENU_ITEMS = "דיני עבודה, דיני משפחה, תעבורה, מקרקעין, פלילי, הוצאה לפועל" 

# --- NEW: LAWYER CONTACT ---
# The bot will send the report HERE. 
# Make sure to add LAWYER_PHONE to your .env file!
LAWYER_PHONE = os.getenv('LAWYER_PHONE') 

# --- MEMORY STORAGE ---
user_sessions = {}

# --- CREDENTIALS ---
if not os.path.exists('credentials.json'):
    google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if google_json:
        with open('credentials.json', 'w') as f:
            f.write(google_json)

TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = 'credentials.json'

# --- 1. SETUP AI ---
try:
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest')
except: print("AI Error")

# --- 2. SETUP TWILIO ---
try:
    if TWILIO_SID and TWILIO_TOKEN:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
except: print("Twilio Error")

# --- 3. SETUP GOOGLE SERVICES ---
calendar_service = None
sheet_service = None

try:
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        # Calendar
        cal_scopes = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=cal_scopes)
        calendar_service = build('calendar', 'v3', credentials=creds)
        
        # Sheets (OPEN BY ID)
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sheet_service = gc.open_by_key(SHEET_ID).sheet1
        print("✅ Lawyer Services Connected!")
except Exception as e: 
    print(f"❌ Google Error: {e}")

# --- HELPER: BOOK MEETING ---
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

# --- HELPER: SEND REPORT TO LAWYER ---
def send_report_to_lawyer(data, client_phone):
    if not client or not LAWYER_PHONE: return
    
    # Clean "Digital Document" Format
    report = f"""
⚖️ *NEW CLIENT CASE FILE* 📅 *Date:* {datetime.datetime.now().strftime("%d/%m/%Y")}

👤 *Client Details*
• *Name:* {data.get('name')}
• *Phone:* {client_phone}

📂 *Category:* {data.get('service_type')}

📝 *Case Description*
"{data.get('case_details')}"

🔻 *Status:* Pending Review
    """
    
    try:
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            body=report,
            to=LAWYER_PHONE
        )
        print("✅ Report sent to Lawyer")
    except Exception as e:
        print(f"❌ Report Failed: {e}")

# --- HELPER: SAVE TO SHEET ---
def save_lead_to_sheet(phone, data):
    if not sheet_service: return False
    try:
        date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            date_now, 
            phone, 
            data.get('name'), 
            data.get('case_details'), 
            data.get('service_type'), 
            "New Lead"
        ]
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
        session['state'] = 'ASK_DETAILS' 
        ai_reply = f"נעים להכיר, {incoming_msg}. על מנת שנוכל לחזור אליך, אנא תאר בקצרה את נושא הפנייה?"

    # --- STATE 2: ASK CASE DETAILS ---
    elif state == 'ASK_DETAILS':
        session['data']['case_details'] = incoming_msg
        
        # 1. Save to Sheet
        save_lead_to_sheet(sender, session['data'])
        
        # 2. Notify Lawyer (Send Report)
        send_report_to_lawyer(session['data'], sender)
        
        ai_reply = "תודה רבה. קיבלנו את הפרטים ועורך דין מטעמנו יצור קשר בהקדם."
        del user_sessions[sender]

    # --- STATE 3: IDLE (MAIN LOGIC) ---
    else:
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        tool_prompt = f"""
        Current Time: {current_time}
        User Message: "{incoming_msg}"
        VALID SERVICES: {MENU_ITEMS}
        
        INSTRUCTIONS:
        1. FILTER: Is the user asking for legal advice? 
           - If YES: Check if it matches VALID SERVICES.
           - IF VALID -> Return action="service", item="[Service Name]".
           - IF VAGUE -> Return action="chat" (Ask for clarification).
        
        2. BOOKING: If asking to schedule a meeting -> action="book".
        3. CHAT: General chat -> action="chat".
        4. BLOCK: Offensive/Curses -> action="block".
        
        Output JSON ONLY:
        Ex: {{"action": "service", "item": "דיני משפחה"}}
        """
        
        try:
            raw = model.generate_content(tool_prompt).text
            clean_json = raw.replace('```json', '').replace('```', '').strip()
            data = json.loads(clean_json)
            action = data.get("action", "chat")
            
            # --- ACTION HANDLERS ---
            if action == "block":
                ai_reply = "נא לשמור על שפה מכבדת."

            elif action == "book":
                iso_time = data.get("iso_time")
                if book_meeting(f"Meeting: {sender}", iso_time):
                    ai_reply = f"נקבעה פגישה לתאריך {iso_time}."
                else:
                    ai_reply = "היומן כרגע מלא או לא זמין."

            elif action == "service":
                # START LEAD FUNNEL
                session['state'] = 'ASK_NAME'
                session['data']['service_type'] = data.get("item")
                ai_reply = f"אשמח לעזור בנושא {data.get('item')}. \nכדי שנתקדם, מה שמך המלא?"
            
            else: # Formal Chat
                chat_prompt = f"""
                You are a legal secretary at {BUSINESS_NAME}.
                Reply in Hebrew. Be formal, professional, and empathetic.
                If they say "Hi", ask: "באיזה נושא משפטי אפשר לעזור לך?".
                User: {incoming_msg}
                """
                ai_reply = model.generate_content(chat_prompt).text
        except:
            ai_reply = "אני בודקת את הנושא..."

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)