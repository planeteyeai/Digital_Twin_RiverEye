# Git Setup Guide for NadiTwin

## Files Created

✅ `.gitignore` (292 lines) - Comprehensive ignore patterns
✅ `.gitattributes` (75 lines) - Git LFS and line ending configuration

---

## What's Ignored

### Python Files
- `__pycache__/` - Compiled Python bytecode
- `*.pyc`, `*.pyo`, `*.pyd` - Python cache files
- `venv/`, `env/`, `.venv/` - Virtual environments
- `*.egg-info/` - Package metadata
- `dist/`, `build/` - Build artifacts

### NadiTwin Cache Files (Auto-regenerated)
- `naditwin/rivers/*/live_cache*.json` - Live data cache
- `naditwin/rivers/*/dem_cache.json` - DEM elevation cache
- `naditwin/rivers/*/archive_*.json` - Historical weather data
- `naditwin/rivers/*/glofas_history_*.json` - GloFAS discharge history

**Note**: These cache files are ignored because they're automatically downloaded from Open-Meteo and GloFAS. First deployment takes 12+ minutes to download them.

### IDE Files
- `.vscode/`, `.idea/` - Editor settings
- `*.swp`, `*.swo` - Vim swap files
- `.DS_Store` - macOS metadata
- `Thumbs.db` - Windows thumbnails

### Deployment
- `.vercel/` - Vercel artifacts
- `.railway/` - Railway artifacts
- `.render/` - Render artifacts
- `*.log` - Log files

### Credentials
- `.env` - Environment variables
- `*.pem`, `*.key` - Private keys
- `credentials.json` - API credentials
- `secrets.json` - Secret data

---

## What's Tracked

✅ **Source Code**
- All `.py` files
- `run.py` - Main entry point
- `naditwin/` directory - Application code

✅ **Configuration**
- `requirements.txt` - Python dependencies
- `Dockerfile`, `docker-compose.yml` - Docker configs
- `railway.toml`, `render.yaml` - Platform configs
- `vercel.json` - Vercel config (educational)

✅ **Documentation**
- All `.md` files
- `README.md` - Project documentation
- `QUICK_START.md`, `DEPLOYMENT.md`, etc.

✅ **Source Data**
- `Digital _twin_kml/` - River KML files
- `naditwin/rivers/*/meta.json` - River metadata
- `naditwin/rivers/*/params.json` - River parameters
- `naditwin/rivers/*/sources.json` - Source file hashes
- `naditwin/rivers/*/localities.json` - Location data
- `naditwin/rivers/*/landmarks.json` - Landmark data
- `naditwin/rivers/*/depth_profile.json` - Depth profiles
- `naditwin/rivers/*/chainage_profile.json` - Chainage data
- `naditwin/rivers/*/reach_polygon.json` - River geometry

✅ **Static Assets**
- `naditwin/static/` - Dashboard HTML/CSS/JS
- `public/` - Static landing page

---

## Git Setup Commands

### Initialize Repository

```bash
cd /home/sid/Downloads/9e81b21e2f074c109a43f0dff382f5e1/Digital

# Initialize git
git init

# Add all files
git add .

# Check what will be committed
git status

# First commit
git commit -m "Initial commit: NadiTwin river digital twin application"
```

### Connect to GitHub

```bash
# Create repository on GitHub first, then:
git remote add origin https://github.com/YOUR_USERNAME/naditwin.git

# Or with SSH:
git remote add origin git@github.com:YOUR_USERNAME/naditwin.git

# Push to GitHub
git branch -M main
git push -u origin main
```

### Connect to GitLab

```bash
git remote add origin https://gitlab.com/YOUR_USERNAME/naditwin.git
git branch -M main
git push -u origin main
```

---

## Optional: Keep Historical Data in Git

If you want to commit the historical cache files (saves 12 minutes on first deployment), **comment out** these lines in `.gitignore`:

```bash
# Edit .gitignore and comment out:
# naditwin/rivers/*/live_cache*.json
# naditwin/rivers/*/dem_cache.json
# naditwin/rivers/*/archive_*.json
# naditwin/rivers/*/glofas_history_*.json
```

Then:

```bash
git add naditwin/rivers/*/archive_*.json
git add naditwin/rivers/*/glofas_history_*.json
git commit -m "Add historical data cache"
```

**Warning**: This adds ~50-100MB to your repository.

---

## Optional: Use Git LFS for Large Files

If your historical data files are very large, use Git Large File Storage:

### Install Git LFS

```bash
# Ubuntu/Debian
sudo apt install git-lfs

# macOS
brew install git-lfs

# Windows (Git Bash)
git lfs install
```

### Configure LFS

Edit `.gitattributes` and uncomment:

```
naditwin/rivers/*/archive_*.json filter=lfs diff=lfs merge=lfs -text
naditwin/rivers/*/glofas_history_*.json filter=lfs diff=lfs merge=lfs -text
```

Then:

```bash
git lfs install
git add .gitattributes
git add naditwin/rivers/*/archive_*.json
git commit -m "Track large data files with LFS"
git push
```

---

## Branching Strategy

### Feature Development

```bash
# Create feature branch
git checkout -b feature/new-river-upload

# Make changes...
git add .
git commit -m "Add new river upload feature"

# Push to remote
git push -u origin feature/new-river-upload

# Create pull request on GitHub/GitLab
```

### Hotfix

```bash
git checkout -b hotfix/fix-discharge-calculation
# Fix the bug...
git commit -m "Fix discharge calculation for narrow rivers"
git push -u origin hotfix/fix-discharge-calculation
```

### Release

```bash
git checkout main
git tag -a v1.0.0 -m "Release version 1.0.0"
git push origin v1.0.0
```

---

## Deployment from Git

### Railway

```bash
# Railway auto-deploys from Git
railway init
railway up

# Or connect GitHub repo in Railway dashboard
```

### Render

```bash
# Connect GitHub repo in Render dashboard
# Render auto-deploys on push to main branch
```

### Manual Deployment

```bash
# Clone on server
git clone https://github.com/YOUR_USERNAME/naditwin.git
cd naditwin

# Deploy with Docker
docker-compose up -d

# Or with Python
pip3 install -r requirements.txt
python3 run.py
```

---

## Git Workflow Example

```bash
# 1. Update from remote
git pull origin main

# 2. Create feature branch
git checkout -b feature/add-mandovi-landmarks

# 3. Make changes
nano naditwin/rivers/mandovi/landmarks.json

# 4. Check changes
git status
git diff

# 5. Stage changes
git add naditwin/rivers/mandovi/landmarks.json

# 6. Commit
git commit -m "Add Mandovi river landmarks"

# 7. Push to remote
git push -u origin feature/add-mandovi-landmarks

# 8. Create pull request on GitHub

# 9. After merge, update main
git checkout main
git pull origin main

# 10. Delete feature branch
git branch -d feature/add-mandovi-landmarks
```

---

## Check What's Ignored

```bash
# List all ignored files
git status --ignored

# Check if specific file is ignored
git check-ignore -v naditwin/rivers/mithi/live_cache_mithi.json

# See what would be committed
git status
```

---

## Useful Git Commands

```bash
# View commit history
git log --oneline --graph --all

# Undo last commit (keep changes)
git reset --soft HEAD~1

# Undo last commit (discard changes)
git reset --hard HEAD~1

# Discard local changes
git checkout -- <file>

# View file at specific commit
git show <commit>:<file>

# Search commits
git log --all --grep="discharge"

# Show files in commit
git show --name-only <commit>
```

---

## Repository Size

Without cache files: ~50-100 MB
- Source code: ~2 MB
- KML files: ~20 MB
- Documentation: ~1 MB
- River data: ~20-50 MB

With cache files: ~150-250 MB
- Historical weather: ~50-100 MB
- GloFAS history: ~20-30 MB
- DEM cache: ~10-20 MB

---

## .gitignore Structure

The `.gitignore` file is organized by category:

1. **Python** - Standard Python ignores
2. **NadiTwin Specific** - Cache files
3. **IDE** - Editor settings
4. **OS** - macOS, Windows, Linux
5. **Docker** - Container artifacts
6. **Deployment** - Platform artifacts
7. **Logs** - Log files
8. **Credentials** - Secrets
9. **Backups** - Backup files
10. **Archives** - Compressed files

---

## Troubleshooting

### Accidentally Committed Cache Files

```bash
# Remove from git but keep locally
git rm --cached naditwin/rivers/*/live_cache*.json
git commit -m "Remove cache files from tracking"
git push
```

### File Still Tracked After Adding to .gitignore

```bash
# Remove from cache and re-add
git rm -r --cached .
git add .
git commit -m "Apply .gitignore"
```

### Large Repository Size

```bash
# Check repository size
du -sh .git

# Find large files
git rev-list --objects --all | \
  git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | \
  awk '/^blob/ {print $3, $4}' | \
  sort -n | tail -10
```

---

## Summary

✅ Comprehensive `.gitignore` with 292 rules
✅ `.gitattributes` for line endings and LFS
✅ Cache files excluded (auto-regenerated)
✅ IDE and OS files ignored
✅ Credentials and secrets protected
✅ Ready for GitHub, GitLab, Bitbucket
✅ Works with Railway, Render auto-deploy

Your repository is ready to push to any Git hosting platform! 🚀
