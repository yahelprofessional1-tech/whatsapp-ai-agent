import google.generativeai as genai
import os
from dotenv import load_dotenv
load_dotenv()
# Paste your NEW, SAFE API key here inside the quotes
genai.configure(api_key="gmi") 

model = genai.GenerativeModel('gemini-1.5-flash')
response = model.generate_content("Hello! How are you?")
print(response.text)    