# config.py
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

WEEKDAY_NAMES_NL = ["ma", "di", "wo", "do", "vr", "za", "zo"]

def business_days_from_today(n: int) -> datetime:
    """Return datetime voor 'n' werkdagen vanaf vandaag (excl. weekend)."""
    d = datetime.now()
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:  # ma=0 .. vr=4
            added += 1
    return d

def is_within_n_business_days(date_obj: datetime, n: int) -> bool:
    """Check of date_obj binnen n werkdagen vanaf vandaag ligt (weekend telt niet mee)."""
    target = business_days_from_today(n)
    return date_obj.date() <= target.date()

def get_next_monday_if_weekend(dt: datetime) -> datetime:
    """Als dt in weekend valt, geef volgende maandag; anders dt ongewijzigd."""
    if dt.weekday() >= 5:  # 5=za,6=zo
        return dt + timedelta(days=(7 - dt.weekday()))
    return dt

class Config:
    # Telegram
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

    # AIBV
    AIBV_USERNAME = os.environ.get("AIBV_USERNAME", "")
    AIBV_PASSWORD = os.environ.get("AIBV_PASSWORD", "")
    LOGIN_URL = "https://planning.aibv.be/Login.aspx?ReturnUrl=%2fIndex.aspx%3flang%3dnl"

    # Station
    STATION_ID = os.environ.get("STATION_ID", "8")  # '8' = Montignies-sur-Sambre
    STATION_NAME = "Montignies-sur-Sambre"

    # Monitoring / timeouts
    REFRESH_DELAY = int(os.environ.get("REFRESH_DELAY", "5"))
    POSTBACK_TIMEOUT = int(os.environ.get("POSTBACK_TIMEOUT", "15"))

    # Omgeving
    IS_HEROKU = os.environ.get("IS_HEROKU", "false").lower() == "true"
    TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
    BOOKING_ENABLED = os.environ.get("BOOKING_ENABLED", "false").lower() == "true"

    @staticmethod
    def get_tomorrow_week_monday_str():
        """
        Maandag (dd/mm/YYYY) van de week waarin morgen valt.
        Als morgen in weekend valt, neem volgende maandag.
        """
        tomorrow = datetime.now() + timedelta(days=1)
        monday = get_next_monday_if_weekend(tomorrow)
        monday = monday - timedelta(days=monday.weekday())  # normaliseer naar maandag
        return monday.strftime("%d/%m/%Y")

if __name__ == "__main__":
    print("✅ Config loaded")
    print("TEST_MODE:", Config.TEST_MODE)
    print("BOOKING_ENABLED:", Config.BOOKING_ENABLED)
    print("Tomorrow-week Monday:", Config.get_tomorrow_week_monday_str())
