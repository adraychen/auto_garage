# Garage AI Agent

An automated assistant for auto garages that reads Gmail inquiries, looks up prices in Supabase, and creates draft replies.

## Setup
1. **Supabase:** Create a table `garage_services` (service_name, price).
2. **Gmail API:** Generate `credentials.json` and `token.json` locally.
3. **Render Deployment:**
   - Connect this GitHub Repo.
   - Add `token.json` and `credentials.json` as **Secret Files**.
   - Add Environment Variables:
     - `SUPABASE_URL`
     - `SUPABASE_KEY`
     - `INFERENCE_API_KEY`
4. **Cron-job.org:** Ping your Render URL every 12 minutes to keep the Free Tier awake.
