import os
import requests
from fastapi import HTTPException
from models import User
from database import SessionLocal


DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")


def get_discord_oauth_url():
    return f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"


def exchange_code_for_token(code: str):
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    response.raise_for_status()
    return response.json()


def get_discord_user_info(access_token: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get("https://discord.com/api/users/@me", headers=headers)
    response.raise_for_status()
    return response.json()


def get_or_create_user(discord_user):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.discord_id == discord_user["id"]).first()
        if not user:
            user = User(
                discord_id=discord_user["id"],
                username=discord_user["username"],
                avatar=discord_user.get("avatar")
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return user
    finally:
        db.close()