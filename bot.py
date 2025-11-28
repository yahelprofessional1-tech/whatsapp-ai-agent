import os
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import google.generativeai as genai

app = Flask(__name__)

# --- CONFIGURATION ---
# 1. Twilio Keys
TWILIO_SID = 'insert key'
TWILIO_TOKEN = 'insert key'
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886' # Sandbox Number

# 2. Your Real Phone (The Israeli one)
MY_REAL_PHONE = '+972587742596' 

# 3. Google Gemini Key
GOOGLE_API_KEY = "insert key"

# --- SHOP DETAILS ---
STORE_NAME = "אטליז בוארון"
ADDRESS = "היהלום 1 נתניה"
HOURS = "ימים א-ה מ06:00 -20:00 ימי שישי מ 06:00-16:00"
PRODUCTS = "אנטריקוט משובח,נתח האנגוס איכותי, סינטה, פילה בקר, בשר בקר טחון טרי מדי יום, עוף, כבש וקבבים ביתיים."

# --- SETUP AI ---
try:
    genai.configure(api_key=GOOGLE_API_KEY)
    # Using the fast 2.0 Flash model
    model = genai.GenerativeModel('gemini-flash-latest')
except Exception as e:
    print(f"Warning: AI Key might be wrong. {e}")

# --- SETUP TWILIO ---
client = Client(TWILIO_SID, TWILIO_TOKEN)

# --- ROUTE 1: VOICE CALLS (Missed Call Handler) ---
@app.route("/incoming", methods=['POST'])
def incoming_call():
    """Forward the call. If missed, mark as 'no-answer'."""
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
        print(f"MISSED CALL from {caller}! Sending First Message...")
        
        # --- NEW SHORT MESSAGE HERE ---
        msg = f"היי, הגעתם ל{STORE_NAME}, שמי אליס. איך אני יכולה לעזור לכם?"
        
        send_whatsapp(caller, msg)
    
    return str(VoiceResponse())

# --- ROUTE 2: WHATSAPP CHAT (The Brain) ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender = request.values.get('From', '')

    print(f"User ({sender}) said: {incoming_msg}")

    # THE NEW PERSONA (Alice - Calm & Human)
    prompt = f"""
    You are Alice (אליס), a secretary at the butcher shop "{STORE_NAME}".
    
    Your Tone:
    - You are CALM and RELAXED. Not "jumpy" or overly excited.
    - You are human, not a robot. Speak naturally.
    - Do NOT use exclamation marks (!) unless absolutely necessary.
    - Do NOT say "Shalom!" at the start of every message. Just answer the question directly.
    - Speak modern, casual Hebrew (Israelis don't talk like marketing brochures).
    
    Business Info:
    - Address: {ADDRESS}
    - Hours: {HOURS}
    - Products: {PRODUCTS}
    
    Rules:
    1. If they ask what you have, just list the main items calmly.
    2. If they ask for prices, say casually that it depends on weight and it's best to come in.
    3. Keep it short.
    
    Customer wrote: "{incoming_msg}"
    Write your reply as Alice in Hebrew:
    """
    
    try:
        response = model.generate_content(prompt)
        ai_reply = response.text
    except Exception as e:
        ai_reply = "אני בודקת, רגע..." # "I'm checking, one moment..." (More human fallback)
        print(f"AI Error: {e}")

    resp = MessagingResponse()
    resp.message(ai_reply)
    return str(resp)
def send_whatsapp(to_number, body_text):
    try:
        if not to_number.startswith('whatsapp:'):
            to_number = f"whatsapp:{to_number}"
        
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            body=body_text,
            to=to_number
        )
        print(f"Message sent to {to_number}")
    except Exception as e:
        print(f"Error sending message: {e}")

if __name__ == "__main__":
    app.run(port=5000, debug=True)