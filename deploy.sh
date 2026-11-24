#!/bin/bash
# NadiTwin Deployment Script
# Usage: ./deploy.sh [docker|railway|render|local]

set -e

COMMAND=${1:-docker}
PORT=${PORT:-8080}

echo "🌊 NadiTwin Deployment Script"
echo "=============================="
echo ""

case $COMMAND in
  docker)
    echo "🐳 Building Docker image..."
    docker build -t naditwin:latest .
    
    echo ""
    echo "✅ Docker image built successfully!"
    echo ""
    echo "To run locally:"
    echo "  docker run -p 8080:8080 naditwin:latest"
    echo ""
    echo "To run with persistent data:"
    echo "  docker run -p 8080:8080 -v \$(pwd)/naditwin/rivers:/app/naditwin/rivers naditwin:latest"
    echo ""
    echo "To use docker-compose:"
    echo "  docker-compose up -d"
    ;;
    
  railway)
    echo "🚂 Deploying to Railway..."
    
    if ! command -v railway &> /dev/null; then
      echo "❌ Railway CLI not found. Install it first:"
      echo "   npm i -g @railway/cli"
      echo "   or visit: https://docs.railway.app/develop/cli"
      exit 1
    fi
    
    railway login
    railway init
    railway up
    
    echo ""
    echo "✅ Deployed to Railway!"
    echo "   Run 'railway open' to view your app"
    ;;
    
  render)
    echo "🎨 Setting up Render deployment..."
    
    if [ ! -d .git ]; then
      echo "Initializing git repository..."
      git init
      git add .
      git commit -m "Initial commit for Render deployment"
    fi
    
    echo ""
    echo "✅ Ready for Render deployment!"
    echo ""
    echo "Next steps:"
    echo "1. Push this repository to GitHub"
    echo "2. Go to https://dashboard.render.com/"
    echo "3. Click 'New +' → 'Web Service'"
    echo "4. Connect your GitHub repository"
    echo "5. Render will auto-detect the Dockerfile and deploy"
    echo ""
    echo "render.yaml is configured for automatic deployment"
    ;;
    
  local)
    echo "💻 Running locally..."
    
    # Check if Python is available
    if ! command -v python3 &> /dev/null; then
      echo "❌ Python 3 not found. Please install Python 3.8+"
      exit 1
    fi
    
    # Check if dependencies are installed
    echo "Checking dependencies..."
    python3 -c "import numpy, PIL" 2>/dev/null || {
      echo "Installing dependencies..."
      pip3 install numpy Pillow
    }
    
    echo ""
    echo "🚀 Starting NadiTwin server..."
    python3 run.py --port $PORT
    ;;
    
  test)
    echo "🧪 Testing Docker build..."
    docker build -t naditwin:test .
    
    echo ""
    echo "Starting test container..."
    docker run -d --name naditwin-test -p 8080:8080 naditwin:test
    
    echo "Waiting for server to start..."
    sleep 5
    
    echo "Testing API endpoints..."
    if curl -f http://localhost:8080/api/rivers > /dev/null 2>&1; then
      echo "✅ Server is responding!"
    else
      echo "❌ Server is not responding"
      docker logs naditwin-test
      exit 1
    fi
    
    echo ""
    echo "Cleaning up test container..."
    docker stop naditwin-test
    docker rm naditwin-test
    
    echo "✅ All tests passed!"
    ;;
    
  *)
    echo "Usage: $0 [docker|railway|render|local|test]"
    echo ""
    echo "Commands:"
    echo "  docker   - Build Docker image"
    echo "  railway  - Deploy to Railway"
    echo "  render   - Setup for Render deployment"
    echo "  local    - Run locally with Python"
    echo "  test     - Build and test Docker image"
    exit 1
    ;;
esac

echo ""
echo "🎉 Done!"
