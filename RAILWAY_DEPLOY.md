# Railway Deployment Guide

## Prerequisites
1. GitHub repository with this code
2. Railway account (free tier available)

## Steps

### 1. Push to GitHub
```bash
git add .
git commit -m "Ready for Railway deployment"
git push origin main
```

### 2. Deploy on Railway
1. Go to [Railway.app](https://railway.app) and sign up/login
2. Click "New Project" → "Deploy from GitHub repo"
3. Connect your GitHub account and select this repository
4. Railway will auto-detect it's a Python app

### 3. Configure Environment Variables
In Railway dashboard → Variables tab, add:
```
POLYGON_API_KEY=your_polygon_key
DISCORD_CLIENT_ID=your_client_id
DISCORD_CLIENT_SECRET=your_client_secret
DISCORD_REDIRECT_URI=https://your-project-name.up.railway.app/auth/callback
SECRET_KEY=super-secret-random-key-change-this-in-production
```

**Note**: `DISCORD_BOT_TOKEN`, `CHANNEL_SMALL`, and `CHANNEL_MID` are **optional** - only needed if you want to run the stock scanner bot alongside the calendar.

### 4. Update Discord OAuth2
- Copy your Railway URL (ends with `.up.railway.app`)
- Go to Discord Developer Portal → OAuth2 → Redirect URIs
- Add: `https://your-project-name.up.railway.app/auth/callback`
- Update the `DISCORD_REDIRECT_URI` in Railway variables

### 5. Deploy
Railway will automatically build and deploy. You'll get a public URL to share!

## Troubleshooting
- Check Railway build logs for errors
- Ensure all environment variables are set
- Railway uses `$PORT` environment variable automatically