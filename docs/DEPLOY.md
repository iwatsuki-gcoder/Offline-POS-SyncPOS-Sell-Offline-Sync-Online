# Deploying SwiftBill to a public website

This gets you a real public URL like `https://swiftbill.onrender.com`
(free), and optionally your own domain name like `mypos.com`.

## Option A — free public URL (5 minutes)

1. Go to **render.com** → **Sign up** with your GitHub account.
2. Dashboard → **New +** → **Blueprint** → select
   `Offline-POS-SyncPOS-Sell-Offline-Sync-Online` → **Apply**.
   (If PR #3 isn't merged yet, pick the `feat/tax-and-llm-chatbot` branch
   when it asks; otherwise `main`.)
3. Wait ~5 minutes for the build. Render gives you a public URL —
   open it and sign in (`manager` / `admin123`).

That's it. The same app you ran on `localhost:8000`, now on the internet.

## Option B — your own domain name

1. Buy a domain (~$10/year) from Namecheap, Porkbun, etc.
2. In Render: your service → **Settings** → **Custom Domain** → add it.
3. At your domain registrar, add the DNS record Render shows you
   (a `CNAME` pointing at your `*.onrender.com` URL).
4. Wait a few minutes for DNS + Render's free SSL certificate.

## Good to know

- **Free tier sleeps.** After ~15 min with no visitors, Render spins the
  app down; the first visit after that takes ~30–60 s to wake up.
- **Disk is temporary.** Sales, users, and receipts reset when Render
  restarts/redeploys the service (fine for a demo; a real shop would add
  a paid disk or external MySQL — see ADR-005).
- **Change the passwords.** Everyone on the internet can open your URL,
  and the demo logins are public: edit `seed_defaults()` in
  `offlinepos/auth.py` (then it re-seeds on next deploy).
- **LLM key:** add `OFFLINEPOS_LLM_API_KEY` (and optionally
  `OFFLINEPOS_LLM_BASE_URL` / `OFFLINEPOS_LLM_MODEL`) under Render's
  **Environment** tab — never commit keys to the repo.
- The **Auto** connectivity pill probes real internet in the cloud, so
  leave `OFFLINEPOS_NET_MODE=auto` (the blueprint default).
