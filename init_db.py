#!/usr/bin/env python3
"""
Post-deployment script to initialize database
"""
import os
from database import init_db

if __name__ == "__main__":
    # Ensure we're in the right directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Initialize database
    init_db()
    print("Database initialized successfully")