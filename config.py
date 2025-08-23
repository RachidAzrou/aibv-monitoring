import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# Korte NL-naam van weekdagen (ma..zo)
WEEKDAY_NAMES_NL = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def business_days_from_today(n: int) -> datetime:
    d = datetime.now()
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def get_next_monday_if_weekend(dt: datetime) -> datetime:
    if dt.weekday() >= 5:
        return dt + timedelta(days=(7 - dt.weekday()))
    return dt


class Config:
    # Telegram
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    # AIBV
    AIBV_USERNAME = os.environ.get("AIBV_USERNAME", "")
    AIBV_PASSWORD = os.environ.get("AIBV_PASSWORD", "")
    LOGIN_URL = "https://planning.aibv.be/Login.aspx?ReturnUrl=%2fIndex.aspx%3flang%3dnl"

    # Station
    STATION_ID = os.environ.get("STATION_ID", "8")  # 8 = Montignies-sur-Sambre

    # Gedrag
    TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
    IS_HEROKU = os.environ.get("IS_HEROKU", "false").lower() == "true"

    # Timings
    REFRESH_DELAY = int(os.environ.get("REFRESH_DELAY", "5"))
    POSTBACK_TIMEOUT = int(os.environ.get("POSTBACK_TIMEOUT", "15"))

    @staticmethod
    def get_tomorrow_week_monday_str():
        """
        Maandag (dd/mm/YYYY) van de week waarin morgen valt.
        Als morgen in weekend valt, neem volgende maandag.
        """
        tomorrow = datetime.now() + timedelta(days=1)
        monday = get_next_monday_if_weekend(tomorrow)
        monday = monday - timedelta(days=monday.weekday())
        return monday.strftime("%d/%m/%Y")


if __name__ == "__main__":
    print("✅ Config loaded")
    print("TEST_MODE:", Config.TEST_MODE)
    print("Tomorrow-week Monday:", Config.get_tomorrow_week_monday_str())
