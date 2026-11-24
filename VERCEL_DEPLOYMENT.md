# Vercel Deployment Instructions

## ⚠️ Important Understanding

**The full NadiTwin Python application CANNOT run on Vercel.** This is a technical limitation, not a configuration issue.

However, you can use Vercel to:
1. ✅ Host a static landing page
2. ✅ Redirect users to your actual app (deployed on Railway/Render)
3. ✅ Serve documentation

## What This Vercel Deployment Does

This configuration deploys a **static landing page** to Vercel that:
- Explains what NadiTwin is
- Provides links to the actual application
- Shows API documentation links
- Displays river features

The actual NadiTwin application must be deployed to Railway, Render, or another container platform.

---

## Setup Instructions

### Step 1: Deploy the Python App Elsewhere First

Before deploying to Vercel, deploy the actual application:

#### Option A: Render (Free Tier)
1. Go to https://render.com
2. Sign in with GitHub
3. New + → Web Service
4. Select your repository
5. Render auto-detects Dockerfile
6. Click "Create Web Service"
7. Wait 10-15 minutes (historical data download)
8. Copy your URL: `https://your-app-name.onrender.com`

#### Option B: Railway
```bash
npm install -g @railway/cli
cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital
railway login
railway init
railway up
```
Copy your Railway URL: `https://your-app.up.railway.app`

---

### Step 2: Update the Landing Page

Edit `public/index.html` and replace `https://your-app.onrender.com` with your actual deployed URL:

```bash
cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital

# Replace with your actual URL
sed -i 's|https://your-app.onrender.com|https://naditwin.onrender.com|g' public/index.html

# Commit changes
git add public/index.html
git commit -m "Update landing page with actual deployment URL"
git push
```

---

### Step 3: Deploy to Vercel

Your Vercel deployment should now work automatically:

1. Vercel is already connected to your GitHub repo
2. Push your changes to main branch
3. Vercel will automatically deploy the static landing page
4. Visit: `https://your-vercel-app.vercel.app`

The landing page will redirect users to your actual application.

---

## Architecture

```
                                    ┌─────────────────────┐
                                    │   User Browser      │
                                    └──────────┬──────────┘
                                               │
                                               │
                        ┌──────────────────────┴──────────────────────┐
                        │                                             │
                        │                                             │
                 ┌──────▼─────────┐                           ┌──────▼─────────┐
                 │                │                           │                │
                 │  Vercel        │                           │  Railway/      │
                 │  (Static       │                           │  Render        │
                 │   Landing      │                           │  (Actual       │
                 │   Page)        │                           │   Python App)  │
                 │                │                           │                │
                 │  - Info        │                           │  - Live data   │
                 │  - Links       │    Redirects to     ──────▶  - API         │
                 │  - Docs        │                           │  - Dashboard   │
                 │                │                           │  - WebSocket   │
                 └────────────────┘                           └────────────────┘
```

---

## File Structure for Vercel

```
Digital/
├── vercel.json              # Vercel config (serves static files)
├── public/
│   └── index.html          # Landing page (UPDATE THIS)
└── [rest of files]         # Not deployed to Vercel
```

**Only `public/` is deployed to Vercel.** Everything else runs on Railway/Render.

---

## Vercel Configuration Explained

```json
{
  "version": 2,
  "name": "naditwin",
  "builds": [
    {
      "src": "public/**",
      "use": "@vercel/static"
    }
  ],
  "routes": [
    {
      "src": "/(.*)",
      "dest": "/public/index.html"
    }
  ]
}
```

This:
- ✅ Serves static files from `public/`
- ✅ Routes all requests to `index.html`
- ✅ Works with Vercel's serverless architecture
- ❌ Does NOT run Python code

---

## Custom Domain Setup

### On Vercel (Landing Page)
1. Go to Vercel dashboard → Your project → Settings → Domains
2. Add: `naditwin.com`
3. Configure DNS as instructed

### On Railway/Render (Actual App)
1. Go to Railway/Render dashboard → Your service → Settings
2. Add: `app.naditwin.com`
3. Update landing page links to use `app.naditwin.com`

Result:
- `naditwin.com` → Vercel landing page
- `app.naditwin.com` → Actual application

---

## Environment Variables

Since the Python app doesn't run on Vercel, set environment variables on Railway/Render:

**Railway/Render**:
- `PORT` = 8080
- `NADITWIN_HISTORY_START` = 2021-01-01

**Vercel**: No environment variables needed (static site)

---

## Why Can't the Full App Run on Vercel?

| Requirement | Vercel | Railway/Render |
|-------------|--------|----------------|
| Long-running server | ❌ No | ✅ Yes |
| 12+ min startup | ❌ 10-60s timeout | ✅ Unlimited |
| Background tasks | ❌ No | ✅ Yes |
| Persistent storage | ❌ Read-only | ✅ Yes |
| WebSocket support | ⚠️ Limited | ✅ Full |
| Python HTTP server | ❌ WSGI/ASGI only | ✅ Any server |

---

## Update Checklist

After deploying to Railway/Render, update these in `public/index.html`:

- [ ] Replace all `https://your-app.onrender.com` with actual URL
- [ ] Update GitHub repository link
- [ ] Update "Open Live Application" button
- [ ] Update API documentation link
- [ ] Add custom domain (if applicable)

```bash
# Quick replace script
ACTUAL_URL="https://naditwin.onrender.com"

cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital

sed -i "s|https://your-app.onrender.com|$ACTUAL_URL|g" public/index.html

git add public/index.html
git commit -m "Update landing page with actual app URL"
git push
```

---

## Testing

### Test Landing Page (Vercel)
1. Visit: `https://your-vercel-app.vercel.app`
2. Verify page loads
3. Check all links work
4. Verify redirect to actual app

### Test Actual App (Railway/Render)
1. Visit: `https://your-app.onrender.com`
2. Check: `https://your-app.onrender.com/api/rivers`
3. Test dashboard loads
4. Verify API endpoints work

---

## Troubleshooting

### Vercel Build Fails
- Ensure `public/index.html` exists
- Check `vercel.json` syntax
- Verify no Python code in build

### Links Don't Work
- Update all URLs in `public/index.html`
- Commit and push changes
- Vercel auto-deploys

### Actual App Not Responding
- Check Railway/Render logs
- Wait 10-15 minutes for first deploy
- Verify historical data downloaded

---

## Cost Breakdown

| Service | Purpose | Cost |
|---------|---------|------|
| Vercel | Landing page | Free |
| Render | Python app | Free or $7/month |
| Railway | Python app | $5-10/month |

**Total**: Free (Vercel + Render free tier) or $5-10/month

---

## Summary

✅ **Vercel**: Static landing page only
✅ **Railway/Render**: Actual Python application
✅ **GitHub**: Code repository
✅ **User Flow**: Vercel → redirects to → Railway/Render

This is the correct architecture for your application. The Python app CANNOT run on Vercel due to fundamental platform limitations.

Deploy the Python app to Railway/Render, then update the Vercel landing page to point to it. ✅
