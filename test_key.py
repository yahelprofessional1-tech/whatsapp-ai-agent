import google.generativeai as genai

# 1. Paste your exact key inside the quotes below
TEST_API_KEY = "AIzaSyCM3Fd79H2FbSOU_DrXXsdDM4jFBN3mJzI" 

genai.configure(api_key=TEST_API_KEY)

print("Testing Gemini API connection...")

try:
    # Testing the 2.0 model specifically
    model = genai.GenerativeModel('gemini-2.5-flash')
    response = model.generate_content("This is a test. Reply with exactly one word: 'Alive'.")
    
    print("\n✅ SUCCESS! The key is fully active.")
    print(f"🤖 Bot says: {response.text}")
    
except Exception as e:
    print("\n❌ FAILURE! Google is blocking the key.")
    print(f"Exact Error: {e}")