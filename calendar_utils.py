import holidays
from datetime import datetime, timedelta
from typing import List


def get_trading_days(start_date: datetime, end_date: datetime) -> List[datetime]:
    """Get list of trading days (weekdays excluding US holidays) between start and end."""
    us_holidays = holidays.US()
    trading_days = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5 and current not in us_holidays:  # Monday to Friday, not holiday
            trading_days.append(current)
        current += timedelta(days=1)
    return trading_days


def get_5_year_trading_calendar() -> List[datetime]:
    """Get trading days for the next 5 years from today."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = today.replace(year=today.year + 5)
    return get_trading_days(today, end_date)