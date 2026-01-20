import os
import json
import datetime
import logging
import smtplib
from email.message import EmailMessage
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread
from dotenv import load_dotenv

# --- 1. הגדרות מערכת ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ButcheryBot")
app = Flask(__name__)

class Config:
    BUSINESS_NAME = "האטליז של אבא" 
    
    # הודעת הפתיחה
    WELCOME_TEXT = "אהלן! 🥩 הגעתם לבוט ההזמנות של האטליז. אפשר להזמין כאן בשר טרי, עופות וכל מה שצריך. מה תרצו להזמין היום?"

    CONTENT_SID = "HX28b3beac873cd8dba0852c183b8bf0ea" 

    _raw_phone = os.getenv('LAWYER_PHONE', '')
    if _raw_phone and not _raw_phone.startswith('whatsapp:'):
        OWNER_PHONE = f"whatsapp:{_raw_phone}"
    else:
        OWNER_PHONE = _raw_phone

    EMAIL_SENDER = os.getenv('EMAIL_SENDER')
    _raw_pass = os.getenv('EMAIL_PASSWORD', '')
    EMAIL_PASSWORD = _raw_pass.replace(" ", "").strip()
    OWNER_EMAIL = os.getenv('LAWYER_EMAIL')
    
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    TWILIO_SID = os.getenv('TWILIO_SID')
    TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
    TWILIO_NUMBER = os.getenv('WHATSAPP_NUMBER')
    
    SHEET_ID = "1_lB_XgnugPu8ZlblgMsyaCHd7GmHvq4NdzKuCguUFDM" 
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    VIP_NUMBERS = [OWNER_PHONE]
    COOL_DOWN_HOURS = 24

def create_credentials():
    if not os.path.exists(Config.SERVICE_ACCOUNT_FILE):
        json_content = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if json_content:
            with open(Config.SERVICE_ACCOUNT_FILE, 'w') as f:
                f.write(json_content)

create_credentials()

twilio_mgr = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN) if Config.TWILIO_SID else None

# --- 2. כלים (שמירת הזמנה מורחבת) ---

def send_email_order(name, order_summary, method, address, timing, phone):
    if not Config.EMAIL_SENDER or not Config.EMAIL_PASSWORD:
        return "Skipped"
    
    subject_line = f"🥩 הזמנה חדשה ({method}): {name}"
    
    body = f"""
    שם לקוח: {name}
    טלפון: {phone}
    סוג: {method}
    כתובת/פרטים: {address}
    זמן מבוקש: {timing}

    --- פירוט ההזמנה ---
    {order_summary}
    """
    
    msg = EmailMessage()
    msg['Subject'] = subject_line
    msg['From'] = Config.EMAIL_SENDER
    msg['To'] = Config.OWNER_EMAIL
    msg.set_content(body)
    
    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo()
            smtp.login(Config.EMAIL_SENDER, Config.EMAIL_PASSWORD)
            smtp.send_message(msg)
        return "Email Sent"
    except: return "Email Failed"

# פונקציית השמירה מקבלת עכשיו את כל הפרטים החדשים
def save_order(name: str, order_details: str, method: str, address: str, timing: str, phone: str):
    """
    Saves the full butchery order with delivery details.
    """
    try:
        clean_phone = phone.replace("whatsapp:", "").replace("+", "")
        wa_link = f"https://wa.me/{clean_phone}"

        # שמירה בגיליון אקסל (הוספנו עמודות)
        if os.path.exists(Config.SERVICE_ACCOUNT_FILE):
            try:
                gc = gspread.service_account(filename=Config.SERVICE_ACCOUNT_FILE)
                sheet = gc.open_by_key(Config.SHEET_ID).sheet1
                # עמודות: תאריך | סוג | שם | טלפון | משלוח/איסוף | כתובת | זמן | פירוט
                sheet.append_row([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), 
                    "MEAT_ORDER", 
                    name, 
                    clean_phone, 
                    method, 
                    address, 
                    timing, 
                    order_details
                ])
            except: pass 

        send_email_order(name, order_details, method, address, timing, clean_phone)
        
        # הודעת וואטסאפ לאבא - ברורה ומסודרת
        if twilio_mgr and Config.OWNER_PHONE:
            
            type_icon = "🛵" if "משלוח" in method else "🛍️"
            
            msg_body = f"""🥩 *הזמנה חדשה נכנסה!*
👤 *שם:* {name}
📞 *טלפון:* {clean_phone}

{type_icon} *סוג:* {method}
📍 *לאן/איפה:* {address}
⏰ *מתי:* {timing}

🔪 *רשימת קניות:*
{order_details}

👇 *לסיום ואישור:*
{wa_link}"""
            
            twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=msg_body, to=Config.OWNER_PHONE)
            
        return f"ORDER SAVED for {name}."
    except Exception as e: return f"Error: {str(e)[:100]}"

# --- 3. AI AGENT (SMART BUTCHER) ---
class GeminiAgent:
    def __init__(self):
        genai.configure(api_key=Config.GOOGLE_API_KEY)
        self.tools = [save_order]
        
        # המוח החדש: מנהל שיחה שלמה עד שיש את כל הפרטים
        self.system_instruction = f"""
        You are the smart assistant for "{Config.BUSINESS_NAME}" (Butcher Shop).
        Language: HEBREW ONLY.

        **GOAL:**
        Collect a full order from the client. Do NOT call `save_order` until you have ALL details.

        **REQUIRED DETAILS TO COLLECT:**
        1. **Order Items:** (Meat, Chicken, amounts, cuts).
        2. **Confirmation:** Ask "Is that everything?" ("זה הכל?") before moving on.
        3. **Method:** Delivery ("משלוח") or Pickup ("איסוף עצמי").
        4. **Logistics:**
           - If Delivery -> Ask for **Address**.
           - If Pickup -> Skip Address (set as "At the shop").
        5. **Timing:** When do they want it?
        6. **Name:** Client's name.

        **CONVERSATION FLOW (Example):**
        1. User: "I want 2kg steak."
        2. You: "Got it. 2kg steak. **Anything else?**"
        3. User: "No that's it."
        4. You: "Great. **Delivery or Pickup?**"
        5. User: "Delivery."
        6. You: "**Where to** and **what time**?"
        7. User: "Herzl 15 Netanya, at 5 PM."
        8. You: "Perfect. **What is your name?**"
        9. User: "Moshe."
        10. You: "Thanks Moshe. Sending the order now."
        -> CALL `save_order(...)`

        **TRAINING EXAMPLES:**

        --- Ex 1: Pickup Flow ---
        User: "תכין לי קילו טחון."
        You: "בכיף. להוסיף עוד משהו או שזה הכל?"
        User: "זהו."
        You: "סבבה. משלוח או איסוף עצמי?"
        User: "איסוף."
        You: "מתי תגיע לקחת?"
        User: "עוד שעה."
        You: "סגור. על איזה שם?"
        User: "דוד."
        You: "תודה דוד, רשמתי."
        (Tool: save_order("דוד", "1 קילו טחון", "איסוף", "בחנות", "עוד שעה", phone))

        --- Ex 2: Delivery Flow ---
        User: "צריך משלוח של 10 שיפודים וקבב."
        You: "רשמתי. תרצה עוד משהו?"
        User: "לא."
        You: "לאן המשלוח ומתי תרצה אותו?"
        User: "לרחוב הגפן 3, בשעה 14:00."
        You: "מעולה. מה השם?"
        User: "רונית."
        You: "תודה רונית, המשלוח בדרך לטיפול."
        (Tool: save_order("רונית", "10 שיפודים, קבב", "משלוח", "הגפן 3", "14:00", phone))
        -------------------------------------------
        
        **RULES:**
        - Be friendly ("Ahlan", "Sababa").
        - Don't guess details. ASK for them.
        - Only save at the very end.
        """
        
        self.model = genai.GenerativeModel('gemini-2.0-flash', tools=self.tools)
        self.active_chats = {}

    def chat(self, user_id, user_msg):
        if user_id not in self.active_chats:
            self.active_chats[user_id] = self.model.start_chat(enable_automatic_function_calling=True)
            self.active_chats[user_id].send_message(f"SYSTEM INSTRUCTION: {self.system_instruction}")
            
        context_msg = f"[System Data - Phone: {user_id}] User says: {user_msg}"
        
        try:
            response = self.active_chats[user_id].send_message(context_msg)
            # אם הבוט החזיר תשובה ריקה, זה אומר שהוא שמר את ההזמנה
            if not response.text:
                return "ההזמנה נקלטה ונשלחה לאטליז! תודה רבה."
            return response.text
        except Exception as e:
            logger.error(f"AI Error: {e}")
            return "נקלט. תודה."

# --- 4. LOGIC ---
agent = GeminiAgent()
last_auto_replies = {} 

@app.route("/status", methods=['POST'])
def status(): 
    status = request.values.get('DialCallStatus', '')
    raw_caller = request.values.get('From', '')
    if raw_caller and not raw_caller.startswith('whatsapp:'): caller = f"whatsapp:{raw_caller}"
    else: caller = raw_caller

    if status in ['no-answer', 'busy', 'failed', 'canceled'] or request.values.get('CallStatus') == 'ringing':
        if caller in Config.VIP_NUMBERS: return str(VoiceResponse())
        now = datetime.datetime.now()
        last = last_auto_replies.get(caller)
        if last and (now - last).total_seconds() < (Config.COOL_DOWN_HOURS * 3600): return str(VoiceResponse())
        try:
            twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, to=caller, body=Config.WELCOME_TEXT)
            last_auto_replies[caller] = now
        except: pass
    return str(VoiceResponse())

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')
    
    if incoming_msg.lower() == "reset":
        if sender in agent.active_chats: del agent.active_chats[sender]
        send_msg(sender, Config.WELCOME_TEXT)
        return str(MessagingResponse())

    try:
        reply = agent.chat(sender, incoming_msg)
        send_msg(sender, reply)
    except Exception as e:
        logger.error(f"AI Crash: {e}")
        send_msg(sender, "משהו השתבש, נסה שוב.")
        
    return str(MessagingResponse())

def send_msg(to, body):
    if twilio_mgr: twilio_mgr.messages.create(from_=Config.TWILIO_NUMBER, body=body, to=to)

@app.route("/", methods=['GET'])
def keep_alive(): return "ButcheryBot V2 is Ready!", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)