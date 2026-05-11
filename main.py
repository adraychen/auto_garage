import os
import time
import threading
import base64
from email.message import EmailMessage
from fastapi import FastAPI
from supabase import create_client, Client
from pydantic_ai import Agent
from pydantic_ai.models.groq import GroqModel
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = FastAPI()

# --- 1. CONFIGURATION ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # Use the 'service_role' key

# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
model = GroqModel('llama-3.3-70b-versatile')

# --- 2. GMAIL SERVICE ---
def get_gmail_service():
    # token.json must be added as a 'Secret File' in Render
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json')
        return build('gmail', 'v1', credentials=creds)
    raise Exception("Critical Error: token.json not found in environment.")

# --- 3. TOOLS & AGENT ---
def get_service_price(query: str):
    """Queries Supabase for garage service prices."""
    try:
        response = supabase.table("garage_services") \
            .select("service_name, price") \
            .ilike("service_name", f"%{query}%") \
            .execute()
        
        if not response.data:
            return "Service not found in database. Tell the customer to contact the shop for a quote."
        
        return "\n".join([f"{item['service_name']}: ${item['price']}" for item in response.data])
    except Exception as e:
        return f"Error accessing price database: {str(e)}"

agent = Agent(
    model, 
    system_prompt=(
        "You are the AI Assistant for an Auto Garage in Ontario. "
        "Your goal is to assist customers with price inquiries and bookings. "
        "Use the 'tool_price_lookup' to get accurate pricing. "
        "Always be professional and mention that prices are subject to HST."
    )
)

@agent.tool
def tool_price_lookup(ctx, service_name: str):
    return get_service_price(service_name)

# --- 4. THE BACKGROUND ENGINE ---
def process_emails():
    # Wait for the server to spin up
    time.sleep(15)
    print("Agent Thread Started: Listening for Gmail inquiries...")
    
    while True:
        try:
            service = get_gmail_service()
            results = service.users().messages().list(userId='me', q='is:unread').execute()
            messages = results.get('messages', [])

            for m in messages:
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                
                # Extract details
                headers = msg.get('payload', {}).get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                sender = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                snippet = msg.get('snippet', "")

                # Run Agent Logic
                result = agent.run_sync(f"From: {sender}\nSubject: {subject}\nMessage: {snippet}")

                # Create Draft
                email_msg = EmailMessage()
                email_msg.set_content(result.output)
                email_msg['To'] = sender
                email_msg['Subject'] = f"Re: {subject}"
                raw_draft = base64.urlsafe_b64encode(email_msg.as_bytes()).decode()
                
                service.users().drafts().create(userId='me', body={'message': {'raw': raw_draft}}).execute()

                # Mark as Read (Remove UNREAD label)
                service.users().messages().batchModify(
                    userId='me', 
                    body={'ids': [m['id']], 'removeLabelIds': ['UNREAD']}
                ).execute()
                
                print(f"Success: Draft created for {sender}")

        except Exception as e:
            print(f"Background Loop Error: {e}")
        
        time.sleep(120) # Poll every 2 minutes

# --- 5. ENDPOINTS ---
@app.get("/")
def home():
    # cron-job.org hits this to keep the service awake
    return {"status": "Garage Agent Active", "location": "Markham/GTA", "engine": "Groq Llama 3.3"}

# Start background logic
threading.Thread(target=process_emails, daemon=True).start()
