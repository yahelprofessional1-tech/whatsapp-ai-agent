import os
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
BUSINESS_NAME = "Israeli Law Firm"
SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
MENU_ITEMS = "דיני עבודה, דיני משפחה, תעבורה, מקרקעין, פלילי, הוצאה לפועל" 

# 1. LAWYER PHONE
LAWYER_PHONE = os.getenv('LAWYER_PHONE') 

# 2. VIP LIST 
VIP_NUMBERS = [
    LAWYER_PHONE,
    "whatsapp:+972500000000", 
]

# 3. SPAM PROTECTION
last_auto_replies = {}
COOL_DOWN_HOURS = 24 

# --- MEMORY ---
user_sessions = {}

# --- CREDENTIALS ---
if not os.path.exists('credentials.json'):
    google_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if google_json:
        with open('credentials.json', 'w') as f:
            f.write(google_json)

GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = 'credentials.json'

# --- SETUP CLIENTS ---
try:
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-flash-latest')
except: print("AI Error")

try:
    if TWILIO_SID and TWILIO_TOKEN:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
except: print("Twilio Error")

calendar_service = None
sheet_service = None
try:
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        cal_scopes = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=cal_scopes)
        calendar_service = build('calendar', 'v3', credentials=creds)
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sheet_service = gc.open_by_key(SHEET_ID).sheet1
        print("✅ Services Connected!")
except Exception as e: print(f"❌ Google Error: {e}")

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

def send_report_to_lawyer(data, client_phone):
    if not client or not LAWYER_PHONE: return
    report = f"""
⚖️ *NEW CLIENT CASE*
👤 *Name:* {data.get('name')}
📞 *Phone:* {client_phone}
📂 *Category:* {data.get('service_type')}
📝 *Details:* "{data.get('case_details')}"
    """
    try:
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, body=report, to=LAWYER_PHONE)
    except: pass

def save_lead_to_sheet(phone, data):
    if not sheet_service: return False
    try:
        date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [date_now, phone, data.get('name'), data.get('case_details'), data.get('service_type'), "New Lead"]
        sheet_service.append_row(row)
        return True
    except: return False

# --- ROUTE 1: MISSED CALLS ---
@app.route("/incoming", methods=['POST'])
def incoming_call():
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

@app.route("/status", methods=['POST'])
def call_status():
    status = request.values.get('DialCallStatus', '')
    caller = request.values.get('From', '') 
    
    if status in ['no-answer', 'busy', 'failed', 'canceled']:
        if caller in VIP_NUMBERS:
            return str(VoiceResponse())

        now = datetime.datetime.now()
        last_time = last_auto_replies.get(caller)
        if last_time and (now - last_time).total_seconds() < (COOL_DOWN_HOURS * 3600):
            return str(VoiceResponse()) 
        
        msg = "שלום, הגעתם למשרד עורכי דין. אנו כרגע בשיחה. איך אפשר לעזור?"
        try:
            client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, body=msg, to=caller)
            last_auto_replies[caller] = now
        except: pass
            
    from twilio.twiml.voice_response import VoiceResponse
    return str(VoiceResponse())

# --- ROUTE 2: WHATSAPP BRAIN ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    if sender not in user_sessions:
        user_sessions[sender] = {'state': 'IDLE', 'data': {}}
    
    session = user_sessions[sender]
    state = session['state']
    ai_reply = ""

    # FLOW: NAME -> DETAILS -> SAVE
    if state == 'ASK_NAME':
        session['data']['name'] = incoming_msg
        session['state'] = 'ASK_DETAILS' 
        ai_reply = f"נעים להכיר, {incoming_msg}. על מנת שנוכל לחזור אליך, אנא תאר בקצרה את נושא הפנייה?"

    elif state == 'ASK_DETAILS':
        session['data']['case_details'] = incoming_msg
        save_lead_to_sheet(sender, session['data'])
        send_report_to_lawyer(session['data'], sender)
        ai_reply = "תודה רבה. קיבלנו את הפרטים ועורך דין מטעמנו יצור קשר בהקדם."
        del user_sessions[sender]

    else:
        # LOGIC GATE (UNBREAKABLE TEXT VERSION)
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tool_prompt = f"""
        Current Time: {current_time}
        User Message: "{incoming_msg}"
        VALID SERVICES: {MENU_ITEMS}
        
        INSTRUCTIONS:
        1. Decide if the user needs a SERVICE, or if they are just CHATTING.
        2. If they need a service (like Family Law, Labor Law, or a general legal consultation), output: SERVICE: [Name of Service]
        3. If they are just saying "Hi" or telling a story without a clear request, output: CHAT: [Your polite Hebrew response]
        4. If they are cursing, output: BLOCK
        
        EXAMPLES:
        User: "אני צריך עזרה עם גירושין"
        Output: SERVICE: דיני משפחה
        
        User: "היי"
        Output: CHAT: שלום, הגעתם למשרד עורכי דין. באיזה נושא משפטי אפשר לעזור?
        
        User: [Long story about being fired]
        Output: SERVICE: דיני עבודה
        """
        
        try:
            # 1. GET RAW TEXT RESPONSE
            raw_response = model.generate_content(tool_prompt).text.strip()
            
            # 2. PARSE WITH SIMPLE TEXT LOGIC (No JSON crashes!)
            if raw_response.startswith("SERVICE:"):
                # Extract the service name
                service_item = raw_response.replace("SERVICE:", "").strip()
                session['state'] = 'ASK_NAME'
                session['data']['service_type'] = service_item
                ai_reply = f"אשמח לעזור בנושא {service_item}. \nכדי שנתקדם, מה שמך המלא?"
            
            elif raw_response.startswith("CHAT:"):
                # Just send the chat response directly
                ai_reply = raw_response.replace("CHAT:", "").strip()
            
            elif raw_response.startswith("BLOCK"):
                ai_reply = "נא לשמור על שפה מכבדת."
            
            else:
                # Fallback if the AI forgets the format
                ai_reply = "לא הייתי בטוחה שהבנתי. באיזה תחום משפטי מדובר?"

        except Exception as e:
            print(f"Logic Error: {e}")
            ai_reply = "תקלה זמנית במערכת. אנא נסו שוב."

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)