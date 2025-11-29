from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# --- CONFIGURATION ---
# 1. The robot file you downloaded
SERVICE_ACCOUNT_FILE = 'credentials.json'

# 2. YOUR REAL GMAIL ADDRESS (The one you shared the calendar from)
# IMPORTANT: Change this to your actual email!
CALENDAR_ID = 'yahel.professional1@gmail.com' 

# --- SETUP ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
service = build('calendar', 'v3', credentials=creds)

# --- CREATE A TEST EVENT ---
# We will book a meeting for "Tomorrow at 10:00 AM"
tomorrow = datetime.now() + timedelta(days=1)
start_time = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
end_time = tomorrow.replace(hour=11, minute=0, second=0, microsecond=0)

event = {
  'summary': 'Test Booking from Python 🐍',
  'location': 'Jerusalem',
  'description': 'If you see this, the robot works!',
  'start': {
    'dateTime': start_time.isoformat(),
    'timeZone': 'Asia/Jerusalem',
  },
  'end': {
    'dateTime': end_time.isoformat(),
    'timeZone': 'Asia/Jerusalem',
  },
}

print(f"Attempting to book a meeting for {start_time}...")

try:
    event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    print('Event created: %s' % (event.get('htmlLink')))
    print("✅ SUCCESS! Go check your Google Calendar.")
except Exception as e:
    print(f"❌ ERROR: {e}")