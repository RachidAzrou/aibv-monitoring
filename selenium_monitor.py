# selenium_monitor.py
import os
import time
import logging
import sys
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Tuple, Optional

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

# Lokaal: automatische driver download; op Heroku gebruiken we env paden.
from webdriver_manager.chrome import ChromeDriverManager

from config import (
    Config,
    is_within_n_business_days,
    get_next_monday_if_weekend,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("AIBV_MON")


class AIBVMonitorBot:
    """
    Monitor (géén boeking):
    - Doorloopt login + flow tot aan station/week.
    - Selecteert de week van morgen (maandag-normalisatie).
    - Maakt periodieke echte page refreshes.
    - Rapporteert enkel nieuwe slots binnen 3 werkdagen (weekend overslaan).
    """
    def __init__(self):
        self.driver = None
        self.filters_initialized = False
        self.chassis = None
        self.merk_model = None
        self.indienst = None

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

        # password/autofill uit
        opts.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        })

        chrome_bin = os.environ.get("GOOGLE_CHROME_BIN") or os.environ.get("CHROME_BIN")
        driver_path = os.environ.get("CHROMEDRIVER_PATH")

        if chrome_bin:
            opts.binary_location = chrome_bin

        # Heroku: gebruik vaste paden; lokaal: webdriver-manager
        if driver_path and os.path.exists(driver_path):
            service = ChromeService(executable_path=driver_path)
        else:
            service = ChromeService(ChromeDriverManager().install())

        try:
            self.driver = webdriver.Chrome(service=service, options=opts)
            self.driver.set_page_load_timeout(45)
        except Exception as e:
            raise RuntimeError(
                f"Chrome startte niet: {e}\n"
                "Controleer CHROME_BIN/CHROMEDRIVER_PATH en buildpacks."
            )
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
                overlay = self._find_overlay()
                state = self.driver.execute_script("return document.readyState")
                if overlay is None and state == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def _find_overlay(self):
        try:
            return self.driver.find_element(By.XPATH, "//*[contains(., 'Even geduld')]")
        except NoSuchElementException:
            return None

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

    def type_by_id(self, element_id: str, value: str, timeout: int = 15):
        el = WebDriverWait(self.driver, timeout).until(
            EC.visibility_of_element_located((By.ID, element_id))
        )
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass

        for _ in range(2):
            try:
                el.clear()
                el.click()
                el.send_keys(value)
                break
            except StaleElementReferenceException:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.visibility_of_element_located((By.ID, element_id))
                )

        try:
            self.driver.execute_script(
                "var e=document.getElementById(arguments[0]);"
                "if(e){e.dispatchEvent(new Event('input',{bubbles:true}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));}", element_id
            )
        except Exception:
            pass
        return el

    def click_by_id(self, element_id: str, timeout: int = 15):
        el = WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((By.ID, element_id))
        )
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass

        try:
            el.click()
        except StaleElementReferenceException:
            el = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.ID, element_id))
            )
            el.click()
        self.wait_dom_idle()
        return el

    def try_accept_cookies_and_set_lang(self):
        # Cookies
        xpaths = [
            "//*[@id='onetrust-accept-btn-handler']",
            "//*[contains(@class,'accept') and contains(.,'Akkoord')]",
            "//*[contains(@class,'btn') and (contains(.,'Aanvaard') or contains(.,'Accepteer'))]",  # <- let op 'or'
        ]
        for xp in xpaths:
            try:
                btn = self.driver.find_element(By.XPATH, xp)
                if btn.is_displayed():
                    try:
                        btn.click()
                    except Exception:
                        self.driver.execute_script("arguments[0].click();", btn)
                    self.wait_dom_idle()
                    break
            except NoSuchElementException:
                pass

        # Forceer NL
        try:
            if "lang=nl" not in self.driver.current_url:
                self.driver.execute_script(
                    "if(window.location.search.indexOf('lang=nl')===-1){"
                    "  var u=new URL(window.location.href);"
                    "  u.searchParams.set('lang','nl');"
                    "  window.location.href=u.toString();}"
                )
                self.wait_dom_idle()
        except Exception:
            pass

    # ---------------- Flow (geen boeking) ----------------
    def login(self):
        d = self.driver
        d.get(Config.LOGIN_URL)
        self.wait_dom_idle()
        self.try_accept_cookies_and_set_lang()

        # velden invullen
        self._fill_login_fields(Config.AIBV_USERNAME, Config.AIBV_PASSWORD)

        # Aanmelden
        self.click_by_id("Button1")

        # Als postback hapert: één JS-click retry
        try:
            WebDriverWait(d, 6).until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(@id,'MainContent_btnVoertuigToevoegen') or contains(.,'Reservatie')]"))
            )
        except TimeoutException:
            try:
                btn = d.find_element(By.ID, "Button1")
                d.execute_script("arguments[0].click();", btn)
            except Exception:
                pass

        self.switch_to_latest_window(timeout=8)
        self.wait_dom_idle()

        # “Reservatie aanmaken”
        try:
            self.click_by_id("MainContent_cmdReservatieAutokeuringAanmaken")
            self.wait_dom_idle()
        except Exception:
            d.get("https://planning.aibv.be/Reservaties/ReservatieOverzicht.aspx?lang=nl")
            self.wait_dom_idle()
            try:
                btn = WebDriverWait(d, 10).until(
                    EC.element_to_be_clickable((By.ID, "MainContent_cmdReservatieAutokeuringAanmaken"))
                )
                d.execute_script("arguments[0].click();", btn)
                self.wait_dom_idle()
            except TimeoutException:
                btn = WebDriverWait(d, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='submit' and contains(@value,'Reservatie')]"))
                )
                d.execute_script("arguments[0].click();", btn)
                self.wait_dom_idle()

        # wachten tot “Voertuig toevoegen” verschijnt
        WebDriverWait(d, 20).until(
            EC.presence_of_element_located((By.ID, "MainContent_btnVoertuigToevoegen"))
        )

    def add_vehicle(self, chassis: str, merk_model: str, inschrijfdatum_ddmmyyyy: str):
        self.chassis = chassis
        self.merk_model = merk_model
        self.indienst = inschrijfdatum_ddmmyyyy

        self.click_by_id("MainContent_btnVoertuigToevoegen")
        self.type_by_id("MainContent_txtChassis", chassis)
        self.type_by_id("MainContent_txtMerkModel", merk_model)
        self.type_by_id("MainContent_txtIndienststelling", inschrijfdatum_ddmmyyyy)

        self.click_by_id("MainContent_cmdOpslaan")
        self.click_by_id("MainContent_cmdVolgendeStap1")

        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "MainContent_btnBevestig"))
        )

    def select_eu_vehicle(self):
        self.click_by_id("MainContent_3cc091f5-7a52-43e5-ab6a-5b211b5ceb91")
        self.click_by_id("MainContent_btnBevestig")

        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, f"MainContent_rblStation_{Config.STATION_ID}"))
        )

    def select_station(self):
        self.click_by_id(f"MainContent_rblStation_{Config.STATION_ID}")
        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek"))
        )
        self.wait_dom_idle()
        self.filters_initialized = True

    # ---------------- Week & Slots ----------------
    def _select_week_value(self, wanted_value: str) -> bool:
        dd = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek"))
        )
        sel = Select(dd)
        for opt in sel.options:
            if opt.get_attribute("value") == wanted_value:
                try:
                    self.driver.execute_script("arguments[0].selected = true;", opt)
                except Exception:
                    pass
                opt.click()
                self.wait_dom_idle()
                return True
        return False

    def _get_selected_week_value(self) -> Optional[str]:
        try:
            dd = self.driver.find_element(By.ID, "MainContent_lbSelectWeek")
            sel = Select(dd)
            return sel.first_selected_option.get_attribute("value")
        except Exception:
            return None

    def select_week_of_tomorrow(self) -> bool:
        tomorrow = datetime.now() + timedelta(days=1)
        monday = get_next_monday_if_weekend(tomorrow)
        monday = monday - timedelta(days=monday.weekday())
        return self._select_week_value(monday.strftime("%d/%m/%Y"))

    def _collect_slots(self) -> List[Tuple[datetime, str]]:
        """Return list[(start_dt, human_label)] binnen 3 werkdagen, weekdays only."""
        out = []
        now = datetime.now()

        for i in range(1, 7 + 1):
            try:
                label_el = self.driver.find_element(By.ID, f"MainContent_LabelDatum{i}")
                label_txt = label_el.text.strip()
            except NoSuchElementException:
                continue
            if not label_txt:
                continue

            day_prefix = label_txt.split()[0].lower()
            if day_prefix not in ("ma", "di", "wo", "do", "vr"):
                continue

            try:
                time_span = self.driver.find_element(By.ID, f"MainContent_rblTijdstip{i}")
            except NoSuchElementException:
                continue

            full_date = time_span.get_attribute("title")  # dd/mm/YYYY
            if not full_date:
                continue

            radios = time_span.find_elements(By.CSS_SELECTOR, "input[type='radio'][id^='MainContent_rblTijdstip']")
            for r in radios:
                try:
                    lb = r.find_element(By.XPATH, "./following-sibling::label")
                    hhmm = lb.text.strip()
                    dt = datetime.strptime(full_date + " " + hhmm, "%d/%m/%Y %H:%M")
                    if dt <= now:
                        continue
                    if is_within_n_business_days(dt, 3):
                        out.append((dt, f"{full_date} {hhmm}"))
                except Exception:
                    continue

        out.sort(key=lambda x: x[0])
        return out

    # ---------------- Monitoring (zonder boeken) ----------------
    def monitor_slots(
        self,
        stop_requested: Callable[[], bool],
        duration_sec: int = 24 * 3600,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """
        Refresh de pagina tot duration_sec of stop.
        Retourneert dict met 'new_slots': List[(ts_seen, label)] en meta.
        """
        start = time.time()
        next_status = start + 300  # elke 5 min
        seen: set[str] = set()
        new_events: List[Tuple[str, str]] = []  # (timestamp_seen, slot_label)

        if not self.filters_initialized:
            # station & week van morgen
            self.select_station()
            ok = self.select_week_of_tomorrow()
            if not ok:
                return {"success": False, "error": "Week van morgen niet gevonden in dropdown."}

        while True:
            if stop_requested():
                return {
                    "success": True,
                    "stopped": True,
                    "new_slots": new_events,
                    "elapsed_sec": int(time.time() - start),
                }

            elapsed = time.time() - start
            if elapsed >= duration_sec:
                return {
                    "success": True,
                    "timeout": True,
                    "new_slots": new_events,
                    "elapsed_sec": int(elapsed),
                }

            # Zorg dat week niet verspringt
            try:
                # dropdown aanwezig?
                WebDriverWait(self.driver, 8).until(
                    EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek"))
                )
            except TimeoutException:
                # terug opbouwen (eenmalig)
                self.select_station()
                self.select_week_of_tomorrow()

            # juiste week blijft behouden; alleen **echte refresh**
            slots = self._collect_slots()
            # detecteer nieuw
            for _, label in slots:
                if label not in seen:
                    seen.add(label)
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    new_events.append((ts, label))

            # status (optioneel)
            if status_callback and time.time() >= next_status:
                next_status += 300
                try:
                    status_callback("⏳ Monitor actief, nog bezig met verversen…")
                except Exception:
                    pass

            # refresh + korte pauze
            self.driver.refresh()
            self.wait_dom_idle()
            time.sleep(Config.REFRESH_DELAY)

    # ---------------- intern ----------------
    def _fill_login_fields(self, username: str, password: str):
        def set_and_verify():
            user = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located((By.ID, "txtUser"))
            )
            pwd = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located((By.ID, "txtPassWord"))
            )
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", user)
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pwd)
            except Exception:
                pass
            for _ in range(2):
                try:
                    user.clear(); user.click(); user.send_keys(username)
                    pwd.clear();  pwd.click();  pwd.send_keys(password)
                    break
                except StaleElementReferenceException:
                    user = self.driver.find_element(By.ID, "txtUser")
                    pwd = self.driver.find_element(By.ID, "txtPassWord")

            self.driver.execute_script(
                "document.getElementById('txtUser').dispatchEvent(new Event('input',{bubbles:true}));"
                "document.getElementById('txtUser').dispatchEvent(new Event('change',{bubbles:true}));"
                "document.getElementById('txtPassWord').dispatchEvent(new Event('input',{bubbles:true}));"
                "document.getElementById('txtPassWord').dispatchEvent(new Event('change',{bubbles:true}));"
            )
            time.sleep(0.15)
            length = self.driver.execute_script(
                "var e=document.getElementById('txtPassWord'); return e && e.value ? e.value.length : 0;"
            )
            if not length:
                # JS fallback
                self.driver.execute_script("""
                    const u=document.getElementById('txtUser');
                    const p=document.getElementById('txtPassWord');
                    if(u){ u.value=arguments[0];
                           u.dispatchEvent(new Event('input',{bubbles:true}));
                           u.dispatchEvent(new Event('change',{bubbles:true})); u.blur(); }
                    if(p){ p.value=arguments[1];
                           p.dispatchEvent(new Event('input',{bubbles:true}));
                           p.dispatchEvent(new Event('change',{bubbles:true})); p.blur(); }
                """, username, password)
                time.sleep(0.1)
            length = self.driver.execute_script(
                "var e=document.getElementById('txtPassWord'); return e && e.value ? e.value.length : 0;"
            )
            return length > 0

        if not set_and_verify():
            raise RuntimeError("Wachtwoordveld bleef leeg (native + JS).")

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
