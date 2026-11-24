# Deployment Guide for NadiTwin

## ⚠️ Important: Vercel Limitations

**Vercel is NOT recommended for this application** because:
- Vercel is designed for serverless/static sites, not long-running Python servers
- This app needs persistent connections and background tasks
- Vercel has 10-second timeout limits for serverless functions
- The app downloads historical data (12+ minutes) which exceeds Vercel's limits

## ✅ Recommended Deployment Options

### Option 1: Docker on Cloud Platforms (RECOMMENDED)

#### Deploy to Railway (Easiest)
1. Install Railway CLI or use the web interface
2. Navigate to the project directory
```bash
cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital
```

3. Deploy:
```bash
railway login
railway init
railway up
```

Railway will automatically detect the Dockerfile and deploy.

#### Deploy to Render
1. Connect your GitHub repository to Render
2. Create a new "Web Service"
3. Render will auto-detect the Dockerfile
4. Set environment variables if needed
5. Deploy

#### Deploy to Google Cloud Run
```bash
# Build and push to Google Container Registry
gcloud builds submit --tag gcr.io/PROJECT_ID/naditwin

# Deploy to Cloud Run
gcloud run deploy naditwin \
  --image gcr.io/PROJECT_ID/naditwin \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --timeout 3600
```

#### Deploy to AWS ECS/Fargate
1. Build and push to Amazon ECR:
```bash
aws ecr create-repository --repository-name naditwin
docker build -t naditwin .
docker tag naditwin:latest AWS_ACCOUNT.dkr.ecr.REGION.amazonaws.com/naditwin:latest
aws ecr get-login-password | docker login --username AWS --password-stdin AWS_ACCOUNT.dkr.ecr.REGION.amazonaws.com
docker push AWS_ACCOUNT.dkr.ecr.REGION.amazonaws.com/naditwin:latest
```

2. Create ECS task definition and service via AWS Console or CLI

### Option 2: Local Docker

#### Build and Run
```bash
# Build the image
docker build -t naditwin .

# Run the container
docker run -p 8080:8080 naditwin

# Or use docker-compose
docker-compose up
```

#### With persistent data (recommended)
```bash
docker run -p 8080:8080 \
  -v $(pwd)/naditwin/rivers:/app/naditwin/rivers \
  -v $(pwd)/Digital\ _twin_kml:/app/Digital\ _twin_kml \
  naditwin
```

### Option 3: Traditional VPS (DigitalOcean, Linode, AWS EC2)

#### Install dependencies on Ubuntu/Debian:
```bash
sudo apt update
sudo apt install python3 python3-pip

# Clone/upload your code
cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital

# Install Python packages
pip3 install numpy Pillow

# Run with systemd (create service file)
sudo nano /etc/systemd/system/naditwin.service
```

#### Create systemd service file:
```ini
[Unit]
Description=NadiTwin River Digital Twin
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/Digital
ExecStart=/usr/bin/python3 run.py --port 8080 --no-browser
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable naditwin
sudo systemctl start naditwin
```

#### Setup Nginx reverse proxy:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
    }
}
```

## Environment Variables

Optional environment variables:
- `PORT`: Server port (default: 8080)
- `NADITWIN_HISTORY_START`: Start date for historical data (default: 2021-01-01)

## Vercel Workaround (Not Recommended)

If you absolutely must use Vercel, you would need to:
1. Rewrite the entire application as serverless functions
2. Use external storage for caching (Redis, database)
3. Split long-running tasks into smaller chunks
4. This requires significant refactoring and is NOT recommended

## Testing Your Deployment

After deployment, test the endpoints:
```bash
# Check if server is running
curl http://your-domain/api/rivers

# Check specific river data
curl http://your-domain/api/meta?river=Mithi

# Open in browser
http://your-domain/
```

## Monitoring

Add monitoring for:
- Server uptime
- API response times
- Memory usage (historical downloads can be memory-intensive)
- Disk space (cache files grow over time)

## Backup

Important data to backup:
- `naditwin/rivers/*/archive_*.json` - Historical data (takes 12+ min to re-download)
- `naditwin/rivers/*/glofas_history_*.json` - GloFAS discharge history
- `naditwin/rivers/*/params.json` - Custom river parameters
- `naditwin/rivers/*/sources.json` - River source file hashes

## Scaling Considerations

- The app is single-threaded by design
- For multiple instances, use a load balancer with sticky sessions
- Consider read-only replicas if you need high availability
- Cache invalidation strategy needed for distributed deployments
