# HomeBasics Sales Order Operator

An AI-powered sales order automation tool that reads customer order emails, interprets natural language requests, checks live inventory, holds stock, generates invoices, and saves draft replies — all automatically.

---

## How It Works

1. A customer sends an order email to the garage Gmail account
2. The app reads the unread email every 2 minutes
3. Groq LLaMA 3.3 70B interprets the email and maps informal product descriptions to inventory
4. Python checks stock availability and holds the requested quantity
5. An invoice is generated with subtotal, HST (13%), and a 7-day payment deadline
6. A draft reply with the invoice inline and a PDF attachment is saved in Gmail
7. An alert email is sent to the operator to review and send the draft

---

## Features

- **Natural language interpretation** — understands informal names, brand names, and vague descriptions (e.g. "TP", "Purell", "the sensitive teeth one")
- **Multi-item orders** — processes multiple products in a single email
- **Stock holding** — reduces available quantity immediately to prevent double-selling
- **Unavailable item handling** — notifies customers of out-of-stock items and products not carried
- **PDF invoice** — attached to every draft reply
- **Inline invoice** — included in the email body for quick review
- **Operator alert** — sends a notification email when a draft is ready
- **Original message quoting** — customer's original email is quoted below the reply

---

## Tech Stack

- **FastAPI** — web server
- **Groq LLaMA 3.3 70B** — natural language interpretation
- **Supabase** — inventory, orders, and invoice database
- **Gmail API** — reading emails and saving drafts
- **ReportLab** — PDF invoice generation
- **Render** — cloud deployment
- **cron-job.org** — keep-alive pinging for Render free tier

---

## Database Setup

Run the following SQL in your **Supabase SQL Editor**:

```sql
-- Products
CREATE TABLE sales_order_products (
    id         UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name       TEXT NOT NULL,
    price      NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Inventory
CREATE TABLE sales_order_inventory (
    id                 UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    product_id         UUID REFERENCES sales_order_products(id) ON DELETE CASCADE,
    stock_quantity     INTEGER NOT NULL DEFAULT 0,
    available_quantity INTEGER NOT NULL DEFAULT 0,
    shelf_location     TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Orders
CREATE TABLE sales_order_orders (
    id             UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    customer_email TEXT NOT NULL,
    customer_name  TEXT,
    product_id     UUID REFERENCES sales_order_products(id),
    quantity       INTEGER NOT NULL,
    unit_price     NUMERIC(10, 2) NOT NULL,
    status         TEXT DEFAULT 'pending',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Invoices
CREATE TABLE sales_order_invoices (
    id               UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    order_id         UUID REFERENCES sales_order_orders(id),
    customer_email   TEXT NOT NULL,
    due_date         DATE NOT NULL,
    total_before_tax NUMERIC(10, 2) NOT NULL,
    hst              NUMERIC(10, 2) NOT NULL,
    total_due        NUMERIC(10, 2) NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Sample products
INSERT INTO sales_order_products (name, price) VALUES
    ('Toothpaste 60 mL',     3.99),
    ('Toilet Paper - 8 Pack', 8.50),
    ('Hand Sanitizer',        5.49),
    ('Laundry Detergent',    12.99);

-- Sample inventory
INSERT INTO sales_order_inventory (product_id, stock_quantity, available_quantity, shelf_location)
SELECT id, 100, 100, 'Aisle A-1' FROM sales_order_products WHERE name = 'Toothpaste 60 mL'
UNION ALL
SELECT id, 150, 150, 'Aisle A-2' FROM sales_order_products WHERE name = 'Toilet Paper - 8 Pack'
UNION ALL
SELECT id, 80,  80,  'Aisle B-1' FROM sales_order_products WHERE name = 'Hand Sanitizer'
UNION ALL
SELECT id, 60,  60,  'Aisle B-2' FROM sales_order_products WHERE name = 'Laundry Detergent';
```

---

## Gmail API Setup

### Step 1 — Google Cloud Project
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create or select a project
3. Navigate to **APIs & Services → Library**
4. Search for **Gmail API** and click **Enable**

### Step 2 — OAuth Consent Screen
1. Go to **APIs & Services → OAuth Consent Screen**
2. Choose **External** and fill in your app name and email
3. Under **Test Users**, add the Gmail address the app will use
4. Click **Publish App** to prevent token expiry every 7 days

### Step 3 — OAuth Credentials
1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth Client ID**
3. Application type: **Web application**
4. Add `http://localhost:8080` under **Authorized Redirect URIs**
5. Click **Create** and download the JSON file
6. Rename it to `credentials.json`

### Step 4 — Generate token.json (run once locally)
Create a folder on your PC with `credentials.json` and a file called `generate_token.py`:

```python
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    SCOPES,
    redirect_uri='http://localhost:8080'
)
creds = flow.run_local_server(port=8080)

with open('token.json', 'w') as f:
    f.write(creds.to_json())

print("token.json generated successfully!")
```

Run it:
```bash
pip install google-auth-oauthlib
python generate_token.py
```

Sign in with the garage Gmail account in the browser that opens. `token.json` will be created in the same folder.

---

## Render Deployment

### Step 1 — Connect GitHub Repo
1. Go to [render.com](https://render.com) and create a new **Web Service**
2. Connect your GitHub repository
3. Set the start command to:
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

### Step 2 — Environment Variables
Add the following under **Environment → Environment Variables**:

| Key | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase service role key |
| `GROQ_API_KEY` | Your Groq API key from console.groq.com |
| `ALERT_EMAIL` | Email address to receive order notifications |

### Step 3 — Secret Files
Add the following under **Environment → Secret Files**:

| Filename | Contents |
|---|---|
| `token.json` | Paste the full contents of your generated `token.json` |

### Step 4 — Deploy
Click **Deploy**. Once live, your service URL will look like:
```
https://your-service-name.onrender.com
```

---

## Keep-Alive Setup (cron-job.org)

Render's free tier shuts down after inactivity. Set up a cron job to ping your service every minute:

1. Go to [cron-job.org](https://cron-job.org) and create a free account
2. Click **Create Cronjob**
3. Set the URL to your Render service URL
4. Set execution schedule to **Every 1 minute**
5. Save

---

## requirements.txt

```
fastapi
uvicorn
pydantic-ai[groq]
supabase
google-api-python-client
google-auth-httplib2
google-auth-oauthlib
python-dotenv
reportlab
groq
```

---

## Environment Variables Summary

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase service role key |
| `GROQ_API_KEY` | Groq API key |
| `ALERT_EMAIL` | Operator alert email address |

---

## Token Refresh

If you see `invalid_grant: Token has been expired or revoked` in the Render logs:

1. Run `generate_token.py` locally again
2. Go to Render → **Environment → Secret Files**
3. Edit `token.json` and paste the new contents
4. Save — Render will redeploy automatically

To prevent expiry, make sure your OAuth app is **published** (not in testing mode) in Google Cloud.

---

## Demo Products and Sample Emails

The app recognises informal names and brand references for all products.

| Product | Recognised As |
|---|---|
| Toothpaste 60 mL | toothpaste, Colgate, Sensodyne, mint paste, brushing paste, fluoride paste, sensitive teeth paste, whitening paste |
| Toilet Paper - 8 Pack | toilet paper, TP, tissue roll, bathroom tissue, 2-ply, Charmin, Scott, bathroom rolls |
| Hand Sanitizer | hand gel, sanitizer gel, Purell, alcohol gel, COVID gel, hygiene gel, disinfectant gel |
| Laundry Detergent | detergent, washing liquid, Tide, Gain, Ariel, pods, capsules, laundry soap, washing powder |

### Sample Emails for Demo

**Single item — informal name**
> Hi, need 2 packs of the mint paste. Thanks, Ray

**Brand preference**
> Hey, can I get 3 packs of TP? Charmin preferred. — Sarah

**Multi-item order**
> Hi team, I need 2 toothpaste, 1 toilet paper, and 3 sanitizer gel. Thanks.

**Item not carried**
> Hi, can I get 2 tubes of toothpaste and 4 toothbrushes? — Mike

**Fully vague multi-item**
> Hi, we're restocking our hotel floor. Need something for handwashing, something for laundry, and bathroom tissue. Around 5 units of each. — Manager
