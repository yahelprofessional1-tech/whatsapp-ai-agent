import google.generativeai as genai

# --- PASTE YOUR KEY HERE ---
GOOGLE_API_KEY = "AIzaSyAMRCNOG0rMTxQ8J43XX_13COIBUdSQbqY" 

genai.configure(api_key=GOOGLE_API_KEY)

print("Checking available models for your key...")
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"- {m.name}")
except Exception as e:
    print(f"Error: {e}")



    