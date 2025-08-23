# selenium_monitor.py
import os
import time
import logging
import sys
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    NoSuchElementException,
    NoSuchWindowException,
)

from webdriver_manager.chrome import ChromeDriverManager

from config import Config, is_within_n_business_days  # <- 3-werkdagen check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("AIBV_MONITOR")


def get_next_monday_if_weekend(dt: datetime) -> datetime:
    if dt.weekday() >= 5:
        return dt + timedelta(days=(7 - dt.weekday()))
    return dt

def monday_of_week(dt: datetime) -> datetime:
    base = dt - timedelta(days=dt.weekday())
    return datetime(base.year, base.month, base.day)


class AIBVMonitorBot:
    def __init__(self):
        self.driver = None
        self.filters_initialized = False
        # doelweek = maandag van de week waar "morgen" in valt (week van morgen)
        tmw = datetime.now() + timedelta(days=1)
        tmw_week_mon = monday_of_week(get_next_monday_if_weekend(tmw))
        self.target_week_monday = tmw_week_mon

    # ---------------- Driver ----------------
    def setup_driver(self):
        opts = ChromeOptions()

        if Config.TEST_MODE:
            opts.add_argument("--auto-open-devtools-for-tabs")
            opts.add_argument("--window-size=1366,900")
        else:
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1366,900")

        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-features=VizDisplayCompositor")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-background-timer-throttling")

        prefs = {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        }
        opts.add_experimental_option("prefs", prefs)

        chrome_bin = os.environ.get("GOOGLE_CHROME_BIN") or os.environ.get("CHROME_BIN")
        driver_path = os.environ.get("CHROMEDRIVER_PATH")

        if chrome_bin:
            opts.binary_location = chrome_bin

        if driver_path and os.path.exists(driver_path):
            service = ChromeService(executable_path=driver_path)
        else:
            service = ChromeService(ChromeDriverManager().install())

        self.driver = webdriver.Chrome(service=service, options=opts)
        self.driver.set_page_load_timeout(45)
        return self.driver

    # ---------------- Helpers ----------------
    def wait(self, cond, timeout=None):
        try:
            return WebDriverWait(self.driver, timeout or Config.POSTBACK_TIMEOUT).until(cond)
        except NoSuchWindowException:
            if self.switch_to_latest_window():
                return WebDriverWait(self.driver, timeout or Config.POSTBACK_TIMEOUT).until(cond)
            raise

    def wait_dom_idle(self, timeout=Config.POSTBACK_TIMEOUT):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.driver.execute_script("return document.readyState") == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def switch_to_latest_window(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            try:
                handles = self.driver.window_handles
                if handles:
                    self.driver.switch_to.window(handles[-1])
                    return True
            except NoSuchWindowException:
                pass
            time.sleep(0.2)
        return False

    def js_click(self, el):
        self.driver.execute_script("arguments[0].click();", el)

    # ---------------- Flow ----------------
    def login_and_open_new_reservation(self):
        d = self.driver
        d.get(Config.LOGIN_URL)
        self.wait_dom_idle()

        user = WebDriverWait(d, 15).until(EC.visibility_of_element_located((By.ID, "txtUser")))
        pwd  = WebDriverWait(d, 15).until(EC.visibility_of_element_located((By.ID, "txtPassWord")))
        user.clear(); user.send_keys(Config.AIBV_USERNAME)
        pwd.clear();  pwd.send_keys(Config.AIBV_PASSWORD)

        self.js_click(WebDriverWait(d, 10).until(EC.element_to_be_clickable((By.ID, "Button1"))))
        self.switch_to_latest_window()
        self.wait_dom_idle()

        try:
            self.js_click(WebDriverWait(d, 10).until(
                EC.element_to_be_clickable((By.ID, "MainContent_cmdReservatieAutokeuringAanmaken"))
            ))
            self.wait_dom_idle()
        except TimeoutException:
            d.get("https://planning.aibv.be/Reservaties/ReservatieOverzicht.aspx?lang=nl")
            self.wait_dom_idle()
            self.js_click(WebDriverWait(d, 10).until(
                EC.element_to_be_clickable((By.ID, "MainContent_cmdReservatieAutokeuringAanmaken"))
            ))
            self.wait_dom_idle()

        WebDriverWait(d, 15).until(EC.presence_of_element_located((By.ID, "MainContent_btnVoertuigToevoegen")))

    def go_until_station_week(self):
        d = self.driver

        self.js_click(WebDriverWait(d, 10).until(EC.element_to_be_clickable((By.ID, "MainContent_btnVoertuigToevoegen"))))
        self.wait_dom_idle()

        d.find_element(By.ID, "MainContent_txtChassis").send_keys("MONITOR1234567890")
        d.find_element(By.ID, "MainContent_txtMerkModel").send_keys("Monitor Bot")
        d.find_element(By.ID, "MainContent_txtIndienststelling").send_keys("01/01/2000")

        self.js_click(WebDriverWait(d, 10).until(EC.element_to_be_clickable((By.ID, "MainContent_cmdOpslaan"))))
        self.wait_dom_idle()

        self.js_click(WebDriverWait(d, 10).until(EC.element_to_be_clickable((By.ID, "MainContent_cmdVolgendeStap1"))))
        self.wait_dom_idle()

        self.js_click(WebDriverWait(d, 10).until(
            EC.element_to_be_clickable((By.ID, "MainContent_3cc091f5-7a52-43e5-ab6a-5b211b5ceb91"))
        ))
        self.js_click(WebDriverWait(d, 10).until(
            EC.element_to_be_clickable((By.ID, "MainContent_btnBevestig"))
        ))
        self.wait_dom_idle()

        WebDriverWait(d, 10).until(EC.presence_of_element_located((By.ID, f"MainContent_rblStation_{Config.STATION_ID}")))
        self.js_click(d.find_element(By.ID, f"MainContent_rblStation_{Config.STATION_ID}"))
        self.wait_dom_idle()

        WebDriverWait(d, 10).until(EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek")))
        self.filters_initialized = True

        self.ensure_week_of_tomorrow(force=True)

    # ---------------- Week & filter ----------------
    def ensure_week_of_tomorrow(self, force: bool = False) -> bool:
        wanted_val = self.target_week_monday.strftime("%d/%m/%Y")
        try:
            sel = Select(self.driver.find_element(By.ID, "MainContent_lbSelectWeek"))
            current = sel.first_selected_option.get_attribute("value")
            if not force and current == wanted_val:
                return True
            for opt in sel.options:
                if opt.get_attribute("value") == wanted_val:
                    self.js_click(opt)
                    self.wait_dom_idle()
                    return True
            return False
        except Exception:
            return False

    # ---------------- Slots verzamelen ----------------
    def _collect_slots_current_table(self):
        """
        Lees slots in de huidige (dropdown) week.
        Regels:
          - Alleen *week van morgen*
          - *Weekend overslaan* (net als boekingsbot)
          - Alleen slots *binnen 3 werkdagen* vanaf nu
        """
        now = datetime.now()
        slots = []
        for i in range(1, 8):
            try:
                label = self.driver.find_element(By.ID, f"MainContent_LabelDatum{i}").text.strip()
            except NoSuchElementException:
                continue
            if not label:
                continue

            # weekend overslaan (zelfde principe als afspraken-bot)
            if not any(label.lower().startswith(x) for x in ("ma", "di", "wo", "do", "vr")):
                continue

            try:
                container = self.driver.find_element(By.ID, f"MainContent_rblTijdstip{i}")
            except NoSuchElementException:
                continue

            full_date = container.get_attribute("title")  # dd/mm/YYYY
            if not full_date:
                continue

            # binnen de target-week blijven
            try:
                d = datetime.strptime(full_date, "%d/%m/%Y")
            except ValueError:
                continue
            if monday_of_week(d) != self.target_week_monday:
                continue

            radios = container.find_elements(By.CSS_SELECTOR, "input[type='radio'][id^='MainContent_rblTijdstip']")
            for r in radios:
                try:
                    lbl = r.find_element(By.XPATH, "./following-sibling::label").text.strip()  # HH:MM
                    dt = datetime.strptime(full_date + " " + lbl, "%d/%m/%Y %H:%M")
                    if dt <= now:
                        continue
                    # Alleen slots binnen 3 *werkdagen* vanaf nu (zelfde filter als boekbot)
                    if not is_within_n_business_days(dt, 3):
                        continue
                    slots.append((dt, f"{full_date} {lbl}"))
                except Exception:
                    continue

        slots.sort(key=lambda x: x[0])
        return slots

    # ---------------- Monitoren ----------------
    def monitor_24h_collect(self, stop_checker, status_hook=None):
        """
        Monitor maximaal 24u, alleen week van morgen, weekend overslaan,
        en alleen slots binnen 3 *werkdagen*. Boekt niets.
        """
        start = time.time()
        self.login_and_open_new_reservation()
        self.go_until_station_week()

        seen = set()
        found = []

        last_status = time.time()
        STATUS_EVERY = 300  # 5 min

        while True:
            if stop_checker and stop_checker():
                return {"success": True, "found": found, "ended": "stopped"}

            elapsed = time.time() - start
            if elapsed >= 24 * 3600:
                return {"success": True, "found": found, "ended": "timeout"}

            # blijf in juiste week
            self.ensure_week_of_tomorrow(force=False)

            try:
                slots = self._collect_slots_current_table()
                now_iso = datetime.now().isoformat(timespec="seconds")
                new_count = 0
                for _, human in slots:
                    if human not in seen:
                        seen.add(human)
                        found.append((now_iso, human))
                        new_count += 1
                if new_count:
                    log.info(f"[MONITOR] nieuwe slots ({new_count}) in doelweek (werkdagen, ≤3bd): "
                             f"{', '.join(h for _, h in found[-new_count:])}")
            except Exception as e:
                log.warning(f"[MONITOR] leesfout slots: {e}")

            self.driver.refresh()
            self.wait_dom_idle()

            if status_hook and time.time() - last_status >= STATUS_EVERY:
                last_status = time.time()
                status_hook({
                    "elapsed_min": int(elapsed // 60),
                    "total_found": len(found),
                    "last_found": found[-3:] if found else [],
                })

            time.sleep(Config.REFRESH_DELAY)

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
