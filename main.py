import os
import time
import json
import threading
import base64
from datetime import datetime, timedelta
from io import BytesIO
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from fastapi import FastAPI
from supabase import create_client, Client
from pydantic_ai import Agent
from pydantic_ai.models.groq import GroqModel
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

app = FastAPI()

# ============================================================
# 1. CONFIGURATION
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ALERT_EMAIL  = os.getenv("ALERT_EMAIL")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
model = GroqModel('llama-3.3-70b-versatile')

# Shared store so process_emails can retrieve PDF after agent.run_sync
_invoice_store = {}

# ============================================================
# 2. GMAIL SERVICE
# ============================================================
def get_gmail_service():
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json')
        return build('gmail', 'v1', credentials=creds)
    raise Exception("Critical Error: token.json not found in environment.")


def get_email_body(msg):
    """Extract full plain-text body from a Gmail message."""
    payload = msg.get('payload', {})

    def decode(data):
        return base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='ignore')

    # Single-part message
    if payload.get('body', {}).get('data'):
        return decode(payload['body']['data'])

    # Multi-part message — prefer text/plain
    for part in payload.get('parts', []):
        if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
            return decode(part['body']['data'])

    return msg.get('snippet', '')


# ============================================================
# 3. PDF GENERATION
# ============================================================
def build_invoice_pdf(invoice_number, customer_name, customer_email,
                      line_items, subtotal, hst, total_due, due_date):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    el = []

    # --- Header ---
    el.append(Paragraph("<b>HomeBasics Co.</b>", styles['Title']))
    el.append(Paragraph("123 Main Street, Toronto, ON  M1A 1A1", styles['Normal']))
    el.append(Paragraph("orders@homebasics.co  |  (416) 555-0100", styles['Normal']))
    el.append(Spacer(1, 0.25 * inch))

    # --- Invoice meta ---
    el.append(Paragraph("<b>INVOICE</b>", styles['Heading1']))
    el.append(Paragraph(f"Invoice #:  {invoice_number}", styles['Normal']))
    el.append(Paragraph(f"Date:       {datetime.now().strftime('%B %d, %Y')}", styles['Normal']))
    el.append(Paragraph(f"Due Date:   {due_date}", styles['Normal']))
    el.append(Spacer(1, 0.2 * inch))

    # --- Bill to ---
    el.append(Paragraph("<b>Bill To:</b>", styles['Normal']))
    el.append(Paragraph(customer_name, styles['Normal']))
    el.append(Paragraph(customer_email, styles['Normal']))
    el.append(Spacer(1, 0.2 * inch))

    # --- Line items table ---
    header = ['Product', 'Qty', 'Unit Price', 'Amount']
    rows   = [header]
    for item in line_items:
        rows.append([
            item['product_name'],
            str(item['quantity']),
            f"${item['unit_price']:.2f}",
            f"${item['quantity'] * item['unit_price']:.2f}",
        ])
    rows.append(['', '', 'Subtotal',   f"${subtotal:.2f}"])
    rows.append(['', '', 'HST (13%)',  f"${hst:.2f}"])
    rows.append(['', '', 'Total Due',  f"${total_due:.2f}"])

    tbl = Table(rows, colWidths=[3 * inch, 0.75 * inch, 1.5 * inch, 1.5 * inch])
    tbl.setStyle(TableStyle([
        # Header row
        ('BACKGROUND',   (0, 0), (-1, 0),  colors.HexColor('#2C3E50')),
        ('TEXTCOLOR',    (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',     (0, 0), (-1, 0),  'Helvetica-Bold'),
        # Alternating rows
        ('ROWBACKGROUNDS', (0, 1), (-1, -4),
         [colors.white, colors.HexColor('#F4F4F4')]),
        # Totals
        ('FONTNAME',     (2, -3), (-1, -1), 'Helvetica-Bold'),
        ('LINEABOVE',    (2, -3), (-1, -3), 0.5, colors.grey),
        ('LINEABOVE',    (2, -1), (-1, -1), 1.5, colors.black),
        # Alignment
        ('ALIGN',        (1, 0),  (-1, -1), 'RIGHT'),
        # Border around data rows only
        ('BOX',          (0, 0),  (-1, -4), 0.5, colors.black),
    ]))
    el.append(tbl)
    el.append(Spacer(1, 0.3 * inch))

    # --- Payment notice ---
    el.append(Paragraph(
        f"<b>Payment is due by {due_date}.</b>  "
        f"Please reference invoice #{invoice_number} when making payment.",
        styles['Normal']
    ))
    el.append(Spacer(1, 0.15 * inch))
    el.append(Paragraph("Thank you for your business!", styles['Normal']))

    doc.build(el)
    buffer.seek(0)
    return buffer.read()


# ============================================================
# 4. TOOL FUNCTIONS
# ============================================================

# Keyword map — maps each canonical product name to its known aliases
PRODUCT_KEYWORDS = {
    "toothpaste": [
        "toothpaste", "colgate", "sensodyne", "mint paste", "teeth whitening",
        "cavity protection", "fluoride", "brushing paste", "gel paste",
        "sensitive teeth", "whitening paste", "gum protection", "the red tube",
        "the white tube", "dental paste",
    ],
    "toilet paper": [
        "toilet paper", "tissue roll", "bathroom tissue", "tp", "rolls",
        "soft tissue", "2-ply", "two-ply", "charmin", "scott", "wipes",
        "white rolls", "bathroom paper", "bathroom roll",
    ],
    "hand sanitizer": [
        "sanitizer", "sanitiser", "hand gel", "alcohol gel", "disinfectant",
        "purell", "covid gel", "hand rub", "hygiene gel", "clear gel",
        "hand wash gel", "antibacterial gel",
    ],
    "laundry detergent": [
        "detergent", "laundry", "washing liquid", "washing powder", "tide",
        "gain", "ariel", "clothes cleaner", "pods", "capsules",
        "stain remover", "laundry soap", "laundry stuff", "washing soap",
    ],
}


def check_inventory_fn(product_description: str, quantity: int) -> str:
    try:
        response = supabase.table("sales_order_products") \
            .select("id, name, price, sales_order_inventory(available_quantity, shelf_location)") \
            .execute()

        if not response.data:
            return json.dumps({"found": False, "message": "No products found in database."})

        desc_lower = product_description.lower()
        best_match = None

        for product in response.data:
            product_name_lower = product['name'].lower()
            for canonical, keywords in PRODUCT_KEYWORDS.items():
                if canonical in product_name_lower:
                    if any(kw in desc_lower for kw in keywords):
                        best_match = product
                        break
            if best_match:
                break

        if not best_match:
            return json.dumps({
                "found": False,
                "message": (
                    f"Could not identify a product from '{product_description}'. "
                    "Ask the customer to clarify what they need."
                )
            })

        inventory     = (best_match.get('sales_order_inventory') or [{}])[0]
        available_qty = inventory.get('available_quantity', 0)

        if available_qty < quantity:
            return json.dumps({
                "found": True,
                "available": False,
                "product_name": best_match['name'],
                "available_quantity": available_qty,
                "message": (
                    f"Insufficient stock for {best_match['name']}. "
                    f"Requested: {quantity}, Available: {available_qty}."
                )
            })

        return json.dumps({
            "found":              True,
            "available":          True,
            "product_id":         best_match['id'],
            "product_name":       best_match['name'],
            "unit_price":         float(best_match['price']),
            "available_quantity": available_qty,
            "shelf_location":     inventory.get('shelf_location', 'N/A'),
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


def create_order_fn(product_id: str, product_name: str, unit_price: float,
                    quantity: int, customer_email: str, customer_name: str) -> str:
    try:
        # Fetch inventory record
        inv = supabase.table("sales_order_inventory") \
            .select("id, available_quantity") \
            .eq("product_id", product_id) \
            .execute()

        if not inv.data:
            return json.dumps({"error": "Inventory record not found."})

        new_available = inv.data[0]['available_quantity'] - quantity
        if new_available < 0:
            return json.dumps({"error": "Not enough stock to hold."})

        # Reduce available quantity (hold the stock)
        supabase.table("sales_order_inventory") \
            .update({"available_quantity": new_available}) \
            .eq("id", inv.data[0]['id']) \
            .execute()

        # Create order record
        order = supabase.table("sales_order_orders") \
            .insert({
                "customer_email": customer_email,
                "customer_name":  customer_name,
                "product_id":     product_id,
                "quantity":       quantity,
                "unit_price":     unit_price,
                "status":         "pending",
            }) \
            .execute()

        return json.dumps({
            "success":      True,
            "order_id":     order.data[0]['id'],
            "product_name": product_name,
            "quantity":     quantity,
            "unit_price":   unit_price,
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


def generate_invoice_fn(orders_json: str, customer_email: str, customer_name: str) -> str:
    try:
        orders   = json.loads(orders_json)
        subtotal = round(sum(o['quantity'] * o['unit_price'] for o in orders), 2)
        hst      = round(subtotal * 0.13, 2)
        total    = round(subtotal + hst, 2)
        due_date = (datetime.now() + timedelta(days=7)).strftime('%B %d, %Y')
        inv_num  = f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # Store invoice record (linked to first order)
        supabase.table("sales_order_invoices") \
            .insert({
                "order_id":         orders[0]['order_id'],
                "customer_email":   customer_email,
                "due_date":         (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'),
                "total_before_tax": subtotal,
                "hst":              hst,
                "total_due":        total,
            }) \
            .execute()

        # Generate PDF
        pdf_bytes = build_invoice_pdf(
            invoice_number = inv_num,
            customer_name  = customer_name,
            customer_email = customer_email,
            line_items     = orders,
            subtotal       = subtotal,
            hst            = hst,
            total_due      = total,
            due_date       = due_date,
        )

        # Save PDF in shared store so process_emails can attach it
        _invoice_store['latest'] = {
            'pdf_bytes':      pdf_bytes,
            'invoice_number': inv_num,
        }

        # Build inline invoice text
        lines = "\n".join(
            f"  {o['product_name']:<25} x{o['quantity']:>3}  "
            f"@ ${o['unit_price']:.2f}  =  ${o['quantity'] * o['unit_price']:.2f}"
            for o in orders
        )
        inline = (
            f"{'─' * 55}\n"
            f"INVOICE #: {inv_num}\n"
            f"Date:      {datetime.now().strftime('%B %d, %Y')}\n"
            f"Due Date:  {due_date}\n"
            f"{'─' * 55}\n"
            f"ITEMS ORDERED:\n{lines}\n"
            f"{'─' * 55}\n"
            f"  Subtotal:   ${subtotal:.2f}\n"
            f"  HST (13%):  ${hst:.2f}\n"
            f"  TOTAL DUE:  ${total:.2f}\n"
            f"{'─' * 55}\n"
            f"Payment due by {due_date}.\n"
            f"Please reference invoice #{inv_num} when making payment.\n"
        )

        return json.dumps({
            "success":        True,
            "invoice_number": inv_num,
            "inline_text":    inline,
            "total_due":      total,
            "due_date":       due_date,
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


# ============================================================
# 5. AGENT
# ============================================================
# agent = Agent(
#     model,
#     system_prompt=(
#         "You are the Sales Order Operator for HomeBasics Co., a wholesale supplier in Ontario, Canada.\n"
#         "Process every customer order email by following these steps in order:\n\n"

#         "STEP 1 — INTERPRET\n"
#         "Extract every product and quantity the customer is requesting.\n"
#         "Customers may use brand names, informal names, or vague descriptions.\n"
#         "Map them to our four products: Toothpaste, Toilet Paper, Hand Sanitizer, Laundry Detergent.\n"
#         "Also extract the customer's name from the email signature or greeting. "
#         "If you cannot find a name, use 'Valued Customer'.\n\n"

#         "STEP 2 — CHECK INVENTORY\n"
#         "For every product identified, call tool_check_inventory(product_description, quantity).\n"
#         "Use the customer's own words as the product_description so the fuzzy matcher can work.\n\n"

#         "STEP 3 — CREATE ORDERS\n"
#         "For each product that is available, call tool_create_order("
#         "product_id, product_name, unit_price, quantity, customer_email, customer_name).\n"
#         "Use the product_id and unit_price returned by tool_check_inventory.\n\n"

#         "STEP 4 — GENERATE INVOICE\n"
#         "Once all available items have orders created, call tool_generate_invoice with:\n"
#         "  orders_json: a JSON array string like "
#         '[{"order_id":"...","product_name":"...","quantity":N,"unit_price":N.NN}, ...]\n'
#         "  customer_email: the sender's email address\n"
#         "  customer_name: the name extracted in Step 1\n\n"

#         "STEP 5 — COMPOSE REPLY\n"
#         "Write a professional email reply that includes:\n"
#         "- A greeting using the customer's name\n"
#         "- Confirmation of all items ordered and their quantities\n"
#         "- If any item was out of stock, clearly apologise and state what was available\n"
#         "- The full invoice inline, exactly as returned by tool_generate_invoice\n"
#         "- A note that a PDF invoice is attached\n"
#         "- A payment deadline reminder\n"
#         "- Sign off as 'HomeBasics Co. Sales Team'\n\n"

#         "RULES:\n"
#         "- Never skip the inventory check\n"
#         "- Never fabricate prices or order IDs\n"
#         "- Always process ALL products before generating the invoice\n"
#         "- If you cannot identify a product, ask the customer to clarify in your reply"
#     ),
# )

agent = Agent(
    model,
    system_prompt=(
        "You are the Sales Order Operator for HomeBasics Co., a wholesale supplier in Ontario, Canada.\n"
        "Process every customer order email by following these steps in order:\n\n"

        "STEP 1 — INTERPRET\n"
        "Extract every product and quantity the customer is requesting.\n"
        "Customers may use brand names, informal names, or vague descriptions.\n"
        "Map them to our four products: Toothpaste, Toilet Paper, Hand Sanitizer, Laundry Detergent.\n"
        "IMPORTANT: If multiple descriptions from the customer map to the SAME product, "
        "treat them as one request and add the quantities together. "
        "For example, 'mint paste' and 'sensitive teeth one' are both Toothpaste — combine them.\n"
        "If no quantity is specified for an item, assume 1.\n"
        "Extract the customer's name from the email signature or greeting. "
        "If you cannot find a name, use 'Valued Customer'.\n\n"

        "STEP 2 — CHECK INVENTORY\n"
        "For every UNIQUE product identified, call tool_check_inventory once with "
        "the combined quantity. Never call tool_check_inventory twice for the same product.\n\n"

        "STEP 3 — CREATE ORDERS\n"
        "For each available product, call tool_create_order once with the combined quantity.\n\n"

        "STEP 4 — GENERATE INVOICE\n"
        "Once all available items have orders created, call tool_generate_invoice with:\n"
        "  orders_json: a JSON array string like "
        '[{"order_id":"...","product_name":"...","quantity":N,"unit_price":N.NN}, ...]\n'
        "  customer_email: the sender's email address\n"
        "  customer_name: the name extracted in Step 1\n\n"

        "STEP 5 — COMPOSE REPLY\n"
        "Write a professional email reply that includes:\n"
        "- A greeting using the customer's name\n"
        "- Confirmation of all items ordered and their quantities\n"
        "- If any item was out of stock, clearly apologise and state what was available\n"
        "- The full invoice inline, exactly as returned by tool_generate_invoice\n"
        "- A note that a PDF invoice is attached\n"
        "- A payment deadline reminder\n"
        "- Sign off as 'HomeBasics Co. Sales Team'\n\n"

        "RULES:\n"
        "- Never output raw function calls or tool syntax in your reply\n"
        "- Never skip the inventory check\n"
        "- Never fabricate prices or order IDs\n"
        "- Always process ALL unique products before generating the invoice\n"
        "- If you cannot identify a product, ask the customer to clarify in your reply"
    ),
)

@agent.tool
def tool_check_inventory(ctx, product_description: str, quantity: int):
    """Check if a product is in stock for the requested quantity."""
    return check_inventory_fn(product_description, quantity)


@agent.tool
def tool_create_order(ctx, product_id: str, product_name: str, unit_price: float,
                      quantity: int, customer_email: str, customer_name: str):
    """Hold stock and create a pending order record in the database."""
    return create_order_fn(product_id, product_name, unit_price,
                           quantity, customer_email, customer_name)


@agent.tool
def tool_generate_invoice(ctx, orders_json: str, customer_email: str, customer_name: str):
    """Generate a PDF invoice and inline invoice text for all orders."""
    return generate_invoice_fn(orders_json, customer_email, customer_name)


# ============================================================
# 6. BACKGROUND ENGINE
# ============================================================
def process_emails():
    time.sleep(15)
    print("Sales Order Operator Started: Listening for order emails...")

    while True:
        try:
            service = get_gmail_service()
            results = service.users().messages().list(userId='me', q='is:unread').execute()
            messages = results.get('messages', [])
            print(f"Debug: Found {len(messages)} unread emails")

            for m in messages:
                msg = service.users().messages().get(
                    userId='me', id=m['id'], format='full'
                ).execute()

                headers  = msg.get('payload', {}).get('headers', [])
                subject  = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                sender   = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                body     = get_email_body(msg)

                print(f"Debug: Processing email from {sender} — {subject}")

                # Clear previous invoice data
                _invoice_store.clear()

                # Run the agent
                result     = agent.run_sync(f"From: {sender}\nSubject: {subject}\nMessage:\n{body}")
                reply_text = result.output

                # Retrieve PDF generated during the agent run
                invoice_data   = _invoice_store.get('latest', {})
                pdf_bytes      = invoice_data.get('pdf_bytes')
                invoice_number = invoice_data.get('invoice_number', 'N/A')

                # Build MIME email (inline text + optional PDF attachment)
                quoted_body = "\n".join(f"> {line}" for line in body.splitlines())
                full_reply = (
                    f"{reply_text}\n\n"
                    f"{'─' * 55}\n"
                    f"On {datetime.now().strftime('%B %d, %Y')}, {sender} wrote:\n\n"
                    f"{quoted_body}"
                )

                mime_msg = MIMEMultipart()
                mime_msg['To']      = sender
                mime_msg['Subject'] = f"Re: {subject}"
                mime_msg.attach(MIMEText(full_reply, 'plain'))

                if pdf_bytes:
                    attachment = MIMEBase('application', 'pdf')
                    attachment.set_payload(pdf_bytes)
                    encoders.encode_base64(attachment)
                    attachment.add_header(
                        'Content-Disposition',
                        f'attachment; filename="Invoice_{invoice_number}.pdf"'
                    )
                    mime_msg.attach(attachment)

                # Save as draft
                raw_draft = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
                service.users().drafts().create(
                    userId='me',
                    body={'message': {'raw': raw_draft}}
                ).execute()

                # Send alert email to owner
                if ALERT_EMAIL:
                    alert = MIMEMultipart()
                    alert['To']      = ALERT_EMAIL
                    alert['Subject'] = f"[HomeBasics] New Order Draft Ready — {subject}"
                    alert.attach(MIMEText(
                        f"A new customer order has been processed.\n\n"
                        f"Customer : {sender}\n"
                        f"Subject  : {subject}\n"
                        f"Invoice  : {invoice_number}\n\n"
                        f"Please review the draft in Gmail and send it out.",
                        'plain'
                    ))
                    raw_alert = base64.urlsafe_b64encode(alert.as_bytes()).decode()
                    service.users().messages().send(
                        userId='me',
                        body={'raw': raw_alert}
                    ).execute()

                # Mark original email as read
                service.users().messages().batchModify(
                    userId='me',
                    body={'ids': [m['id']], 'removeLabelIds': ['UNREAD']}
                ).execute()

                print(f"Success: Draft created and alert sent for {sender} — Invoice {invoice_number}")

        except Exception as e:
            print(f"Background Loop Error: {e}")

        print("Debug: Sleeping for 120 seconds...")
        time.sleep(120)


# ============================================================
# 7. ENDPOINTS
# ============================================================
@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return {
        "status":   "HomeBasics Sales Order Operator Active",
        "engine":   "Groq LLaMA 3.3 70B",
        "version":  "2.0",
    }


# Start background engine
threading.Thread(target=process_emails, daemon=True).start()
