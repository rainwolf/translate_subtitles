import os
from random import randint, random
import time

from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def checkDriverFirst():
    path = os.path.realpath("/usr/local/bin/chromedriver")
    with open(path, "rb") as file:
        content = file.read()
        if b"cdc_" in content:
            file.close()
            with open(path, "wb") as file:
                file.write(content.replace(b"cdc_", b"fis_"))
            print("driver cdc_ patched")
        else:
            print("driver is already cdc_ patched")


def change_browser_window_size(browser=None, max_width=None, max_height=None):
    width = max_width - randint(0, 100)
    height = max_height - randint(0, 100)
    if browser is not None:
        browser.set_window_size(width, height)


def setup_browser(save_path=None):
    checkDriverFirst()
    opts = Options()
    opts.add_argument("--start-maximized")
    # opts.add_argument("--headless")
    opts.binary_location = '/Applications/Internet/Google Chrome.app/Contents/MacOS/Google Chrome'
    opts.add_argument("user-data-dir=chromeUserData")
    opts.add_argument("--disable-dev-shm-usage")  # overcome limited resource problems
    opts.add_argument("--no-sandbox")  # Bypass OS security model
    opts.add_argument('--disable-web-security')
    opts.add_argument('--allow-running-insecure-content')
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    opts.add_experimental_option("useAutomationExtension", False) 


    prefs = {"download.default_directory" : save_path}
    opts.add_experimental_option("prefs", prefs)
    browser = Chrome(options=opts)
    browser.implicitly_wait(100)
    browser.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    # if random() > 0.5:
    #     browser.switch_to.new_window('tab')
    #     browser.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    width, height = browser.get_window_size().values()

    return browser, width, height


def translate(path=None, from_lang='en', to_lang='th', browser=None):
    save_path = path.replace(path.split('/')[-1], '')[:-1]
    width, height = None, None
    if browser is None:
        browser, width, height = setup_browser(save_path=save_path)

    browser.get(f'https://translate.google.com/?sl={from_lang}&tl={to_lang}&op=docs')
    drag_drop = browser.find_element('xpath', "/html/body/c-wiz/div/div[2]/c-wiz/div[3]/c-wiz/div[2]/c-wiz/div/div[1]/div/div/div[1]/div[2]/div[2]/div/input")
    drag_drop.send_keys(path)
    translate_button = browser.find_element('xpath', "/html/body/c-wiz/div/div[2]/c-wiz/div[3]/c-wiz/div[2]/c-wiz/div/div[1]/div/div[2]/div/div/button")
    translate_button.click()
    os.remove(path)

    failed = True

    while failed:
        try:
            element = WebDriverWait(browser, 15).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/c-wiz/div/div[2]/c-wiz/div[3]/c-wiz/div[2]/c-wiz/div/div[1]/div/div[2]/div/button/span[2]")))
            sleep_time = randint(1, 4)
            element.click()
            while not os.path.exists(path):
                time.sleep(1)
            failed = False
        except Exception as e:
            sleep_time = randint(2, 7)
            print("translation need to retry")
            time.sleep(sleep_time)

    return browser, width, height