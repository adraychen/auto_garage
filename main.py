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
from groq import Groq
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ALERT_EMAIL  = os.getenv("ALERT_EMAIL")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# Aliases help the AI map informal names to exact product names
PRODUCT_ALIASES = {
    "Toothpaste 60 mL": (
        "toothpaste, brushing paste, mint paste, fluoride paste, Colgate, Sensodyne, "
        "sensitive teeth paste, whitening paste, cavity protection, teeth gel, dental paste, "
        "the red tube, the white tube, gum protection paste"
    ),
    "Toilet Paper - 8 Pack": (
        "toilet paper, TP, tissue roll, bathroom tissue, 2-ply, two-ply, Charmin, Scott, "
        "bathroom rolls, white rolls, bathroom paper, the rolls, soft tissue"
    ),
    "Hand Sanitizer 300 mL": (
        "hand gel, sanitizer gel, sanitiser gel, alcohol gel, Purell, COVID gel, hand rub, "
        "hygiene gel, disinfectant gel, clear gel, antibacterial gel, sanitizer"
    ),
    "Laundry Detergent 1.8 L": (
        "laundry soap, washing liquid, Tide, Gain, Ariel, pods, capsules, clothes cleaner, "
        "washing powder, laundry stuff, detergent, stain remover wash, laundry liquid"
    ),
}


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

    if payload.get('body', {}).get('data'):
        return decode(payload['body']['data'])

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
    doc    = SimpleDocTemplate(buffer, pagesize=letter,
                               topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    el     = []

    # Header
    el.append(Paragraph("<b>HomeBasics Co.</b>", styles['Title']))
    el.append(Spacer(1, 0.25 * inch))

    # Invoice meta
    el.append(Paragraph("<b>INVOICE</b>", styles['Heading1']))
    el.append(Paragraph(f"Invoice #:  {invoice_number}", styles['Normal']))
    el.append(Paragraph(f"Date:       {datetime.now().strftime('%B %d, %Y')}", styles['Normal']))
    el.append(Paragraph(f"Due Date:   {due_date}", styles['Normal']))
    el.append(Spacer(1, 0.2 * inch))

    # Bill to
    el.append(Paragraph("<b>Bill To:</b>", styles['Normal']))
    el.append(Paragraph(customer_name,  styles['Normal']))
    el.append(Paragraph(customer_email, styles['Normal']))
    el.append(Spacer(1, 0.2 * inch))

    # Line items table
    rows = [['Product', 'Qty', 'Unit Price', 'Amount']]
    for item in line_items:
        rows.append([
            item['product_name'],
            str(item['quantity']),
            f"${item['unit_price']:.2f}",
            f"${item['quantity'] * item['unit_price']:.2f}",
        ])
    rows.append(['', '', 'Subtotal',  f"${subtotal:.2f}"])
    rows.append(['', '', 'HST (13%)', f"${hst:.2f}"])
    rows.append(['', '', 'Total Due', f"${total_due:.2f}"])

    tbl = Table(rows, colWidths=[3 * inch, 0.75 * inch, 1.5 * inch, 1.5 * inch])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',     (0, 0),  (-1, 0),  colors.HexColor('#2C3E50')),
        ('TEXTCOLOR',      (0, 0),  (-1, 0),  colors.white),
        ('FONTNAME',       (0, 0),  (-1, 0),  'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0, 1),  (-1, -4), [colors.white, colors.HexColor('#F4F4F4')]),
        ('FONTNAME',       (2, -3), (-1, -1), 'Helvetica-Bold'),
        ('LINEABOVE',      (2, -3), (-1, -3), 0.5, colors.grey),
        ('LINEABOVE',      (2, -1), (-1, -1), 1.5, colors.black),
        ('ALIGN',          (1, 0),  (-1, -1), 'RIGHT'),
        ('BOX',            (0, 0),  (-1, -4), 0.5, colors.black),
    ]))
    el.append(tbl)
    el.append(Spacer(1, 0.3 * inch))

    # Payment notice
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
# 4. INVENTORY
# ============================================================
def fetch_inventory():
    """Fetch all products and their current inventory from Supabase."""
    response = supabase.table("sales_order_products") \
        .select("id, name, price, sales_order_inventory(available_quantity, shelf_location)") \
        .execute()
    return response.data


def build_inventory_text(inventory):
    """Format inventory as a readable list for the AI prompt."""
    lines = []
    for product in inventory:
        inv       = (product.get('sales_order_inventory') or [{}])[0]
        available = inv.get('available_quantity', 0)
        aliases   = PRODUCT_ALIASES.get(product['name'], '')
        lines.append(
            f"- {product['name']} | also known as: {aliases} | "
            f"price: ${float(product['price']):.2f} | available: {available} units"
        )
    return "\n".join(lines)


# ============================================================
# 5. AI INTERPRETATION
# ============================================================
def interpret_email(sender, subject, body, inventory):
    """Ask Groq to interpret the customer email and return structured JSON."""
    inventory_text = build_inventory_text(inventory)

    prompt = f"""You are an order interpreter for HomeBasics Co., a wholesale supplier.

Our current inventory:
{inventory_text}

A customer sent this email:
From: {sender}
Subject: {subject}
Message:
{body}

Match the customer's requests to our inventory using the "also known as" aliases.
Extract the customer's name from the signature or greeting.

Return ONLY a valid JSON object with this exact structure:
{{
  "customer_name": "name from email or Valued Customer if not found",
  "items_requested": [
    {{"product_name": "exact product name from our inventory", "quantity": number, "original_request": "what the customer originally asked for in their own words"}}
  ],
  "items_not_found": ["items the customer asked for that do not match any of our products"]
}}

Rules:
- Only put products that exist in our inventory in items_requested
- Use the exact product name as listed in our inventory
- If two descriptions refer to the same product, combine them into one entry with added quantities
- Quantity must always be a whole number (integer). Never use decimals or fractions.
- If the customer uses a generic unit word like "units", "pieces", "items", or "bottles", treat the number as the exact quantity requested. For example, "6 units" = quantity 6.
- Only round up to packs when the customer describes individual components of a known pack (e.g. "6 rolls" for a product sold in 8-roll packs).
- Brand names like Charmin, Colgate, Tide are preferences, not separate products. Do not put them in items_not_found.
- If no quantity is mentioned, assume 1
- Put only items that are completely unrelated to our inventory in items_not_found
- Return ONLY the JSON object, no explanation or extra text"""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return json.loads(response.choices[0].message.content)


# ============================================================
# 6. ORDER PROCESSING
# ============================================================
def process_order(interpretation, inventory, sender):
    """Hold stock and create order records for confirmed items."""
    product_lookup = {}
    for product in inventory:
        inv = (product.get('sales_order_inventory') or [{}])[0]
        product_lookup[product['name']] = {
            'id':        product['id'],
            'price':     float(product['price']),
            'available': inv.get('available_quantity', 0),
        }

    customer_name   = interpretation.get('customer_name', 'Valued Customer')
    items_requested = interpretation.get('items_requested', [])
    items_not_found = list(interpretation.get('items_not_found', []))

    confirmed_items    = []
    out_of_stock_items = []

    for item in items_requested:
        product_name = item['product_name']
        quantity     = item['quantity']

        if product_name not in product_lookup:
            items_not_found.append(product_name)
            continue

        product = product_lookup[product_name]

        if product['available'] < quantity:
            out_of_stock_items.append({
                'product_name': product_name,
                'requested':    quantity,
                'available':    product['available'],
            })
            continue

        # Hold stock — reduce available quantity
        inv_record = supabase.table("sales_order_inventory") \
            .select("id, available_quantity") \
            .eq("product_id", product['id']) \
            .execute()

        new_available = inv_record.data[0]['available_quantity'] - quantity
        supabase.table("sales_order_inventory") \
            .update({"available_quantity": new_available}) \
            .eq("id", inv_record.data[0]['id']) \
            .execute()

        # Create order record
        order = supabase.table("sales_order_orders") \
            .insert({
                "customer_email": sender,
                "customer_name":  customer_name,
                "product_id":     product['id'],
                "quantity":       quantity,
                "unit_price":     product['price'],
                "status":         "pending",
            }) \
            .execute()

        confirmed_items.append({
            'order_id':        order.data[0]['id'],
            'product_name':    product_name,
            'quantity':        quantity,
            'unit_price':      product['price'],
            'original_request': item.get('original_request', ''),
        })

    return customer_name, confirmed_items, out_of_stock_items, items_not_found


# ============================================================
# 7. INVOICE
# ============================================================
def create_invoice(confirmed_items, sender, customer_name):
    """Store invoice in Supabase and return invoice details."""
    subtotal    = round(sum(i['quantity'] * i['unit_price'] for i in confirmed_items), 2)
    hst         = round(subtotal * 0.13, 2)
    total       = round(subtotal + hst, 2)
    due_date_dt = datetime.now() + timedelta(days=7)
    due_date    = due_date_dt.strftime('%B %d, %Y')
    inv_num     = f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    supabase.table("sales_order_invoices") \
        .insert({
            "order_id":         confirmed_items[0]['order_id'],
            "customer_email":   sender,
            "due_date":         due_date_dt.strftime('%Y-%m-%d'),
            "total_before_tax": subtotal,
            "hst":              hst,
            "total_due":        total,
        }) \
        .execute()

    return inv_num, subtotal, hst, total, due_date


# ============================================================
# 8. EMAIL REPLY COMPOSER
# ============================================================
def compose_reply(customer_name, confirmed_items, out_of_stock_items,
                  items_not_found, inv_num, subtotal, hst, total, due_date):
    lines = [f"Dear {customer_name},\n"]
    lines.append(
        "Thank you for your order with HomeBasics Co. "
        "Here is a summary of your request:\n"
    )

    if confirmed_items:
        lines.append("CONFIRMED ITEMS:")
        for item in confirmed_items:
            lines.append(f"  - {item['product_name']}  x{item['quantity']}  @ ${item['unit_price']:.2f} each")
            original = item.get('original_request', '')
            
            if original and original.lower() not in item['product_name'].lower():
                    is_pack_product = "pack" in item['product_name'].lower() or "roll" in item['product_name'].lower()
                    if is_pack_product:
                        lines.append(
                            f"    Note: You requested {original}. We offer this product in "
                            f"{item['product_name'].split('-')[-1].strip()} — "
                            f"we have processed {item['quantity']} "
                            f"pack{'s' if item['quantity'] > 1 else ''} for your order."
                        )
                    else:
                        lines.append(
                            f"    Note: You requested {original}. "
                            f"We have processed {item['quantity']} "
                            f"unit{'s' if item['quantity'] > 1 else ''} of {item['product_name']} for your order."
                        )   
        lines.append("")
                      
    if out_of_stock_items:
        lines.append("INSUFFICIENT STOCK:")
        for item in out_of_stock_items:
            lines.append(
                f"  - {item['product_name']}: you requested {item['requested']} units "
                f"but only {item['available']} are currently available. "
                "Please contact us to arrange."
            )
        lines.append("")

    if items_not_found:
        lines.append("ITEMS NOT CARRIED:")
        for item in items_not_found:
            lines.append(
                f"  - {item}: we do not currently carry this product. "
                "Please contact us for alternatives."
            )
        lines.append("")

    if confirmed_items:
        invoice_text = (
            f"{'─' * 58}\n"
            f"INVOICE #: {inv_num}\n"
            f"Date:      {datetime.now().strftime('%B %d, %Y')}\n"
            f"Due Date:  {due_date}\n"
            f"{'─' * 58}\n"
        )
        for item in confirmed_items:
            amount = item['quantity'] * item['unit_price']
            invoice_text += (
                f"  {item['product_name']:<32} "
                f"x{item['quantity']:>3}  "
                f"@ ${item['unit_price']:.2f}  =  ${amount:.2f}\n"
            )
        invoice_text += (
            f"{'─' * 58}\n"
            f"  Subtotal:   ${subtotal:.2f}\n"
            f"  HST (13%):  ${hst:.2f}\n"
            f"  TOTAL DUE:  ${total:.2f}\n"
            f"{'─' * 58}\n"
            f"Payment due by {due_date}.\n"
            f"Please reference invoice #{inv_num} when making payment.\n"
        )
        lines.append(invoice_text)
        lines.append("A PDF copy of this invoice is attached for your records.\n")

    lines.append("If you have any questions, please don't hesitate to reach out.")
    lines.append("\nHomeBasics Co. Sales Team")

    return "\n".join(lines)


# ============================================================
# 9. BACKGROUND ENGINE
# ============================================================
def process_emails():
    time.sleep(15)
    print("Sales Order Operator Started: Listening for order emails...")

    while True:
        try:
            service  = get_gmail_service()
            results  = service.users().messages().list(userId='me', q='is:unread').execute()
            messages = results.get('messages', [])
            print(f"Debug: Found {len(messages)} unread emails")

            for m in messages:
                try:
                    msg = service.users().messages().get(
                        userId='me', id=m['id'], format='full'
                    ).execute()

                    headers = msg.get('payload', {}).get('headers', [])
                    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                    sender  = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                    body    = get_email_body(msg)

                    print(f"Debug: Processing email from {sender} — {subject}")

                    # Step 1: Fetch live inventory
                    inventory = fetch_inventory()

                    # Step 2: AI interprets the email
                    interpretation = interpret_email(sender, subject, body, inventory)
                    print(f"Debug: Interpretation — {interpretation}")

                    # Step 3: Process order — hold stock, create records
                    customer_name, confirmed_items, out_of_stock_items, items_not_found = \
                        process_order(interpretation, inventory, sender)

                    # Step 4: Generate invoice if there are confirmed items
                    pdf_bytes = None
                    inv_num = subtotal = hst = total = due_date = None

                    if confirmed_items:
                        inv_num, subtotal, hst, total, due_date = \
                            create_invoice(confirmed_items, sender, customer_name)
                        pdf_bytes = build_invoice_pdf(
                            invoice_number = inv_num,
                            customer_name  = customer_name,
                            customer_email = sender,
                            line_items     = confirmed_items,
                            subtotal       = subtotal,
                            hst            = hst,
                            total_due      = total,
                            due_date       = due_date,
                        )

                    # Step 5: Compose reply
                    reply_text = compose_reply(
                        customer_name, confirmed_items, out_of_stock_items,
                        items_not_found, inv_num, subtotal, hst, total, due_date
                    )

                    # Step 6: Quote original email below reply
                    quoted     = "\n".join(f"> {line}" for line in body.splitlines())
                    full_reply = (
                        f"{reply_text}\n\n"
                        f"{'─' * 58}\n"
                        f"On {datetime.now().strftime('%B %d, %Y')}, {sender} wrote:\n\n"
                        f"{quoted}"
                    )

                    # Step 7: Build MIME email with optional PDF attachment
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
                            f'attachment; filename="Invoice_{inv_num}.pdf"'
                        )
                        mime_msg.attach(attachment)

                    # Step 8: Save as draft
                    raw_draft = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
                    service.users().drafts().create(
                        userId='me',
                        body={'message': {'raw': raw_draft}}
                    ).execute()

                    # Step 9: Send alert email to owner
                    if ALERT_EMAIL:
                        alert = MIMEMultipart()
                        alert['To']      = ALERT_EMAIL
                        alert['Subject'] = f"[HomeBasics] New Order Draft Ready — {subject}"
                        alert.attach(MIMEText(
                            f"A new customer order has been processed.\n\n"
                            f"Customer : {sender}\n"
                            f"Subject  : {subject}\n"
                            f"Invoice  : {inv_num or 'No invoice — no items confirmed'}\n\n"
                            f"Please review the draft in Gmail and send it out.",
                            'plain'
                        ))
                        raw_alert = base64.urlsafe_b64encode(alert.as_bytes()).decode()
                        service.users().messages().send(
                            userId='me',
                            body={'raw': raw_alert}
                        ).execute()

                    # Step 10: Mark original email as read
                    service.users().messages().batchModify(
                        userId='me',
                        body={'ids': [m['id']], 'removeLabelIds': ['UNREAD']}
                    ).execute()

                    print(f"Success: Draft created for {sender} — Invoice {inv_num}")

                except Exception as e:
                    print(f"Error processing individual email: {e}")

        except Exception as e:
            print(f"Background Loop Error: {e}")

        print("Debug: Sleeping for 120 seconds...")
        time.sleep(120)


# ============================================================
# 10. ENDPOINTS
# ============================================================
@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return {
        "status":  "HomeBasics Sales Order Operator Active",
        "engine":  "Groq LLaMA 3.3 70B",
        "version": "3.0",
    }


# Start background engine
threading.Thread(target=process_emails, daemon=True).start()
