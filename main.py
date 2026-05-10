import os
import time
import threading
import base64
from email.message import EmailMessage
from fastapi import FastAPI
from supabase import create_client
from pydantic_ai import Agent
from pydantic_ai.models.groq import GroqModel
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = FastAPI()

# --- 1. CONFIGURATION & CLIENTS ---
# Set these in Render's Environment Variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

db = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- 2. GMAIL SETUP ---
def get_gmail_service():
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json')
        return build('gmail', 'v1', credentials=creds)
    raise Exception("token.json not found! Add it as a Secret File in Render.")

# --- 3. AGENT TOOLS ---
def get_service_price(query: str):
    response = db.table("garage_services").select("service_name, price").ilike("service_name", f"%{query}%").execute()
    if not response.data:
        return "Service not listed. Advise the customer to call for a quote."
    return "\n".join([f"{item['service_name']}: ${item['price']}" for item in response.data])

# --- 4. THE AI AGENT (Using Groq) ---
# llama-3.3-70b-versatile is excellent for reasoning and tool-calling
model = GroqModel('llama-3.3-70b-versatile', api_key=GROQ_API_KEY)
agent = Agent(model, system_prompt="You are an Auto Garage Assistant. Use tools to find prices. Draft professional replies.")

@agent.tool
def tool_price_lookup(ctx, service_name: str):
    """Search for the price of a specific garage service."""
    return get_service_price(service_name)

# --- 5. LOGIC LOOP ---
def process_emails():
    # Wait a few seconds for the web server to start
    time.sleep(10)
    service = get_gmail_service()
    while True:
        try:
            # Search for unread emails in the inbox
            results = service.users().messages().list(userId='me', q='is:unread').execute()
            messages = results.get('messages', [])

            for m in messages:
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                
                # Extract simple details
                snippet = msg.get('snippet')
                headers = msg.get('payload', {}).get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                sender = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown Sender")

                # Run Agent reasoning
                result = agent.run_sync(f"From: {sender}\nSubject: {subject}\nContent: {snippet}")

                # Create Draft
                email_msg = EmailMessage()
                email_msg.set_content(result.data)
                email_msg['To'] = sender
                email_msg['Subject'] = f"Re: {subject}"
                raw_draft = base64.urlsafe_b64encode(email_msg.as_bytes()).decode()
                service.users().drafts().create(userId='me', body={'message': {'raw': raw_draft}}).execute()

                # Mark as Read
                service.users().messages().batchModify(userId='me', body={'ids': [m['id']], 'removeLabelIds': ['UNREAD']}).execute()
                print(f"Groq-powered draft created for: {sender}")

        except Exception as e:
            print(f"Loop Error: {e}")
        
        time.sleep(120) # Checks every 2 minutes

# --- 6. WEB ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "active", "provider": "Groq", "service": "Garage Agent"}

# Start background thread
threading.Thread(target=process_emails, daemon=True).start()
