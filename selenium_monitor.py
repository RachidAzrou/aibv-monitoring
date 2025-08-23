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

# Lokaal: automatische driver (niet gebruikt op Heroku als CHROMEDRIVER_PATH aanwezig is)
from webdriver_manager.chrome import ChromeDriverManager

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("AIBV-MON")


class AIBVMonitorBot:
    def __init__(self):
        self.driver = None
        self.filters_initialized = False

    # ---------------- Driver ----------------
    def setup_driver(self):
        """
        Chrome-driver starten.
        - Heroku: gebruikt CHROMEDRIVER_PATH + (GOOGLE_)CHROME_BIN (van buildpack Chrome for Testing).
        - Lokaal: webdriver_manager.
        """
        opts = ChromeOptions()
        if Config.TEST_MODE:
            opts.add_argument("--auto-open-devtools-for-tabs")
            opts.add_argument("--window-size=1366,900")
        else:
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1366,900")

        # Stabiel op server
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-features=VizDisplayCompositor")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-background-timer-throttling")

        # Geen password manager/autofill
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

        try:
            self.driver = webdriver.Chrome(service=service, options=opts)
            self.driver.set_page_load_timeout(45)
        except Exception as e:
            raise RuntimeError(f"Chrome startte niet: {e}")
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

    # ------- ID-first helpers -------
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

    # ------- login (met cookie/taal) -------
    def try_accept_cookies_and_set_lang(self):
        # cookie
        for xp in [
            "//*[@id='onetrust-accept-btn-handler']",
            "//*[contains(@class,'accept') and contains(.,'Akkoord')]",
            "//*[contains(@class,'btn') and (contains(.,'Aanvaard') or contains(.,'Accepteer'))]",
        ]:
            try:
                btn = self.driver.find_element(By.XPATH, xp)
                if btn.is_displayed():
                    btn.click()
                    self.wait_dom_idle()
                    break
            except NoSuchElementException:
                pass
        # taal
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

    def fill_login_fields(self, username, password):
        user = WebDriverWait(self.driver, 15).until(
            EC.visibility_of_element_located((By.ID, "txtUser"))
        )
        pwd = WebDriverWait(self.driver, 15).until(
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
        if not length:
            raise RuntimeError("Wachtwoordveld bleef leeg na invullen (native + JS).")

    # ---------------- Flow tot slots ----------------
    def login(self):
        d = self.driver
        d.get(Config.LOGIN_URL)
        self.wait_dom_idle()
        self.try_accept_cookies_and_set_lang()

        self.fill_login_fields(Config.AIBV_USERNAME, Config.AIBV_PASSWORD)
        self.click_by_id("Button1")

        try:
            WebDriverWait(d, 6).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(.,'Reservatie') or contains(@id,'MainContent_btnVoertuigToevoegen')]")
                )
            )
        except TimeoutException:
            try:
                btn = d.find_element(By.ID, "Button1")
                d.execute_script("arguments[0].click();", btn)
            except Exception:
                pass

        self.switch_to_latest_window(timeout=8)
        self.wait_dom_idle()

        # Klik “Reservatie aanmaken”
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

        WebDriverWait(d, 15).until(
            EC.presence_of_element_located((By.ID, "MainContent_btnVoertuigToevoegen"))
        )
        return True

    def add_vehicle(self, chassis: str, merk_model: str, inschrijfdatum_ddmmyyyy: str):
        self.click_by_id("MainContent_btnVoertuigToevoegen")
        self.type_by_id("MainContent_txtChassis", chassis)
        self.type_by_id("MainContent_txtMerkModel", merk_model)
        self.type_by_id("MainContent_txtIndienststelling", inschrijfdatum_ddmmyyyy)
        self.click_by_id("MainContent_cmdOpslaan")
        self.click_by_id("MainContent_cmdVolgendeStap1")
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "MainContent_btnBevestig"))
        )
        return True

    def select_eu_vehicle(self):
        self.click_by_id("MainContent_3cc091f5-7a52-43e5-ab6a-5b211b5ceb91")
        self.click_by_id("MainContent_btnBevestig")
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, f"MainContent_rblStation_{Config.STATION_ID}"))
        )
        return True

    def select_station(self):
        self.click_by_id(f"MainContent_rblStation_{Config.STATION_ID}")
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek"))
        )
        self.wait_dom_idle()
        self.filters_initialized = True
        return True

    # ---------------- Week & slots uitlezen ----------------
    def _get_selected_week_value(self) -> str | None:
        try:
            dd = self.driver.find_element(By.ID, "MainContent_lbSelectWeek")
            sel = Select(dd)
            return sel.first_selected_option.get_attribute("value")
        except Exception:
            return None

    def _ensure_week_selected(self) -> bool:
        wanted = Config.get_tomorrow_week_monday_str()
        try:
            dd = self.driver.find_element(By.ID, "MainContent_lbSelectWeek")
            sel = Select(dd)
            if sel.first_selected_option.get_attribute("value") == wanted:
                return True
            for opt in sel.options:
                if opt.get_attribute("value") == wanted:
                    try:
                        self.driver.execute_script("arguments[0].selected = true;", opt)
                    except Exception:
                        pass
                    opt.click()
                    self.wait_dom_idle()
                    return True
        except Exception:
            pass
        return False

    def _collect_slots(self):
        """Geeft list terug van (datetime, human_label)."""
        slots = []
        now = datetime.now()

        for i in range(1, 8):
            try:
                label_el = self.driver.find_element(By.ID, f"MainContent_LabelDatum{i}")
                label = label_el.text.strip()  # bv. "wo 10/09"
            except NoSuchElementException:
                continue
            if not label:
                continue

            day_prefix = label.split()[0].lower()
            if day_prefix not in ("ma", "di", "wo", "do", "vr"):
                continue

            try:
                time_span = self.driver.find_element(By.ID, f"MainContent_rblTijdstip{i}")
            except NoSuchElementException:
                continue

            full_date = time_span.get_attribute("title")  # "dd/mm/yyyy"
            if not full_date:
                continue

            radios = time_span.find_elements(By.CSS_SELECTOR, "input[type='radio'][id^='MainContent_rblTijdstip']")
            for r in radios:
                try:
                    label_el = r.find_element(By.XPATH, "./following-sibling::label")
                    text_time = label_el.text.strip()  # "08:30"
                    dt = datetime.strptime(full_date + " " + text_time, "%d/%m/%Y %H:%M")
                    if dt <= now:
                        continue
                    slots.append((dt, f"{full_date} {text_time}"))
                except Exception:
                    continue

        slots.sort(key=lambda x: x[0])
        return slots

    # ---------------- Monitoren (24u max) ----------------
    def monitor_slots(
        self,
        duration_seconds: int = 24 * 3600,
        stop_check=lambda: False,
        on_new_event=None,
    ):
        """
        Keert terug met een lijst events: [{"slot":"dd/mm/yyyy HH:MM","detected_at":"YYYY-mm-dd HH:MM:SS"}, ...]
        - stop_check(): callable die True teruggeeft wanneer we moeten stoppen (voor /stop).
        - on_new_event(event_dict): optionele callback bij nieuw slot.
        """
        start = time.time()
        events = []
        seen = set()  # unieke key per slot: "YYYY-mm-dd HH:MM"

        # 1) Zorg eenmalig dat juiste week staat
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek"))
            )
        except TimeoutException:
            pass
        self._ensure_week_selected()

        while True:
            if stop_check():
                break
            elapsed = time.time() - start
            if elapsed >= duration_seconds:
                break

            try:
                # 2) zeker zijn dat we nog op de juiste pagina staan
                try:
                    WebDriverWait(self.driver, 8).until(
                        EC.presence_of_element_located((By.ID, "MainContent_lbSelectWeek"))
                    )
                except TimeoutException:
                    # lichte recovery: hard refresh
                    self.driver.refresh()
                    self.wait_dom_idle()

                # 3) Week niet telkens wisselen; enkel checken en zo nodig herstellen
                self._ensure_week_selected()

                # 4) Lees slots
                found_now = self._collect_slots()

                # 5) detecteer nieuwe
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for dt, human in found_now:
                    key = dt.strftime("%Y-%m-%d %H:%M")
                    if key not in seen:
                        seen.add(key)
                        ev = {"slot": human, "detected_at": now_str}
                        events.append(ev)
                        if on_new_event:
                            try:
                                on_new_event(ev)
                            except Exception:
                                pass

                # 6) echte refresh en korte pauze
                self.driver.refresh()
                self.wait_dom_idle()
                time.sleep(Config.REFRESH_DELAY)

            except Exception as e:
                log.warning(f"⚠️ Monitor-fout: {e}")
                self.driver.refresh()
                self.wait_dom_idle()
                time.sleep(min(5, Config.REFRESH_DELAY * 2))

        return events

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
