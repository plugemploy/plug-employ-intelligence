# Connect Gmail: one-time Google setup

About 15 minutes, in your browser. Use the same Google Cloud project as your Places key ("Plug Employ").
Never paste the values from step 5 into chat; they go only in `.env`.

1. **Open** https://console.cloud.google.com and pick the **Plug Employ** project at the top.
2. **Turn on Gmail:** APIs & Services → Library → search **Gmail API** → **Enable**.
3. **Consent screen** (the page people see when they connect): menu → **Google Auth Platform**
   (or "OAuth consent screen") → **Get started**.
   - App name: `Plug Employ Intelligence`. Support email: your email.
   - **Audience:** choose **Internal** if it's offered (only possible when the project belongs to n2co.com);
     otherwise **External**.
   - Contact email: your email → **Create**.
   - If you chose External: go to **Audience** → **Test users** → **Add users** → add your `@n2co.com` address
     (and any teammates who'll test it, up to 100).
4. **Permission:** **Data access** → **Add or remove scopes** → find `.../auth/gmail.readonly`
   ("Read all resources and their metadata") → tick it → **Update** → **Save**.
5. **Login key for the app:** **Clients** → **Create client**.
   - Application type: **Web application**. Name: `Plug Employ local`.
   - **Authorized redirect URIs** → **Add URI** → `http://127.0.0.1:8000/gmail/callback`
   - **Create**, then copy the **Client ID** and **Client secret**.
6. **Paste into `.env`:** after `GOOGLE_OAUTH_CLIENT_ID=` and `GOOGLE_OAUTH_CLIENT_SECRET=`. Save.
7. **Restart the app** (Control + C in its Terminal window, then run `start.sh` again).
8. **Connect:** open **http://127.0.0.1:8000/business** (this exact address) → **Connect Gmail** → sign in with
   your `@n2co.com` account.
   - "Google hasn't verified this app" → **Continue** (it's your own app).
   - "Access blocked: your administrator…" → N2's Google admin must allow the app. Nothing else to do here.
9. Click **Check Gmail now**.
