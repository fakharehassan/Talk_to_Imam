import os
from google import genai

key = os.environ.get("GEMINI_API_KEY")
print(f"Key as read by Python: '{key}'")  # check for None, or unexpected characters

client = genai.Client(api_key=key)
response = client.models.generate_content(model="gemini-3.1-flash-lite", contents="Say hello")
print(response.text)