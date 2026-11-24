# NadiTwin Quick Start Guide

## 🚀 Fastest Way to Deploy (2 minutes)

### Option 1: Railway (Recommended)

```bash
# Install Railway CLI
npm i -g @railway/cli

# Navigate to project
cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital

# Deploy
railway login
railway init
railway up
```

Your app will be live at: `https://naditwin-production.up.railway.app`

**Cost**: ~$5-10/month

---

### Option 2: Render (Free Tier)

1. Push your code to GitHub
2. Go to https://render.com
3. Click "New +" → "Web Service"
4. Connect your GitHub repository
5. Render auto-detects the Dockerfile and deploys

**Cost**: Free (spins down after 15min inactivity) or $7/month

---

### Option 3: Docker Locally

```bash
# Build
docker build -t naditwin .

# Run
docker run -p 8080:8080 naditwin

# Or use docker-compose
docker-compose up
```

Open: http://localhost:8080

---

### Option 4: Python Directly

```bash
# Install dependencies
pip3 install numpy Pillow

# Run
python3 run.py
```

Open: http://localhost:8080

---

## 📋 What You Get

### Built-in Rivers
- **Mula-Mutha** (Pune) - 8 locations
- **Mithi** (Mumbai) - 7 locations  
- **Mandovi** (Goa) - 13 locations

### Live Data
- Real-time discharge every 10m
- Water levels and stages
- Flood risk margins (SAFE/WATCH/WARNING/DANGER)
- 72-hour forecasts
- Historical data since Jan 2021

### API Endpoints
- `/api/rivers` - List all rivers
- `/api/meta?river=Mithi` - River metadata
- `/api/live?river=Mithi` - Live data
- `/api/discharge_profile?river=Mithi` - Full discharge profile
- `/api/hydrograph?river=Mithi&cell=5` - Time series
- And many more...

---

## ⚠️ Why Not Vercel?

**Vercel doesn't support this application** because:
- It's a long-running server, not serverless functions
- Downloads 12+ minutes of historical data
- Needs persistent storage and background tasks
- Requires state management

See `VERCEL_WARNING.md` for details.

---

## 📂 Files Created

```
Digital/
├── Dockerfile              ← Docker container definition
├── docker-compose.yml      ← Docker Compose config
├── .dockerignore          ← Files to exclude from Docker
├── deploy.sh              ← Deployment helper script
├── railway.toml           ← Railway platform config
├── render.yaml            ← Render platform config
├── vercel.json            ← Vercel config (won't work, see warning)
├── .gitignore             ← Git ignore rules
├── DEPLOYMENT.md          ← Full deployment guide
├── VERCEL_WARNING.md      ← Why Vercel won't work
├── QUICK_START.md         ← This file
└── public/
    └── index.html         ← Static info page
```

---

## 🔧 Configuration

### Environment Variables

```bash
PORT=8080                           # Server port
NADITWIN_HISTORY_START=2021-01-01  # Historical data start date
```

### Custom Parameters

Edit `naditwin/rivers/<river>/params.json`:

```json
{
  "manning_n": 0.035,
  "dwf_m3s_per_km": 0.12,
  "area_km2_per_km": 5.0,
  "bank_height_m": [4.5, 4.5, 3.8, ...],
  "flow_dir": 1
}
```

---

## 📊 Testing Your Deployment

After deployment, test these endpoints:

```bash
# Check server health
curl https://your-domain.com/api/rivers

# Get river metadata  
curl https://your-domain.com/api/meta?river=Mithi

# Get live data
curl https://your-domain.com/api/live?river=Mithi

# Open dashboard in browser
open https://your-domain.com/
```

---

## 🆘 Troubleshooting

### Problem: "Port already in use"
```bash
# Find and kill the process
lsof -ti:8080 | xargs kill -9
```

### Problem: Docker build fails
```bash
# Clean Docker cache
docker system prune -a

# Rebuild
docker build --no-cache -t naditwin .
```

### Problem: Historical data not loading
- Check internet connection
- Wait 12+ minutes for first download
- Check logs for Open-Meteo API errors

### Problem: "STALE" data warning
- Internet connection lost
- Using cached data from previous fetch
- Will auto-refresh when connection restored

---

## 📚 Next Steps

1. ✅ Deploy using Railway/Render (2 minutes)
2. 📖 Read `DEPLOYMENT.md` for advanced options
3. 🎨 Customize rivers by uploading KML files
4. 📊 Integrate API into your applications
5. 🔧 Calibrate parameters for your rivers

---

## 🤝 Need Help?

- Full deployment guide: `DEPLOYMENT.md`
- Vercel explanation: `VERCEL_WARNING.md`
- Run test: `./deploy.sh test`
- Check logs: `docker logs <container_id>`

---

## ⚡ One-Liner Deployment

```bash
# Railway
npm i -g @railway/cli && railway login && railway init && railway up

# Docker
docker-compose up -d

# Local Python
pip3 install numpy Pillow && python3 run.py
```

**That's it!** Your river digital twin is live. 🌊
