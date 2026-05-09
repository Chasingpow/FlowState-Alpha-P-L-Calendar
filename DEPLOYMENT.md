# DigitalOcean App Platform Deployment

## Prerequisites
1. DigitalOcean account
2. GitHub repository with this code
3. Discord OAuth2 app configured

## Steps

### 1. Update Environment Variables
Edit your `.env` file with production values:
- Set `DISCORD_REDIRECT_URI` to your DigitalOcean app URL + `/auth/callback`
- Add your `DISCORD_BOT_TOKEN`
- Add your channel IDs for `CHANNEL_SMALL` and `CHANNEL_MID`

### 2. Push to GitHub
```bash
git add .
git commit -m "Ready for deployment"
git push origin main
```

### 3. Deploy on DigitalOcean
1. Go to [DigitalOcean App Platform](https://cloud.digitalocean.com/apps)
2. Click "Create App" → "GitHub"
3. Connect your GitHub account and select this repository
4. Configure:
   - **Resource Type**: Web Service
   - **Source**: GitHub (your repo)
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Run Command**: `uvicorn pnl_calendar:app --host 0.0.0.0 --port $PORT`
   - **Environment Variables**: Copy from your `.env` file

### 4. Update Discord OAuth2
In your Discord Developer Portal:
- Add your DigitalOcean app URL to "Redirect URIs"
- Update `DISCORD_REDIRECT_URI` in your `.env` file

### 5. Database
The app uses SQLite, which will be created automatically. For production, consider upgrading to PostgreSQL later.

## Troubleshooting
- Check logs in DigitalOcean App Platform
- Ensure all environment variables are set
- Verify Discord OAuth2 redirect URI matches