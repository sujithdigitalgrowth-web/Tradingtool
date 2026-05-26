from playwright.sync_api import sync_playwright
import os

os.makedirs("logs", exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})

    # Screenshot 1: Live tab
    page.goto("http://localhost:5000")
    page.wait_for_timeout(2500)
    page.screenshot(path="logs/dash_live.png")
    print("Live tab screenshot saved")

    # Switch to Date Range Analysis tab
    page.click("button#tab-range")
    # Wait for the cache to load and chart to render
    page.wait_for_timeout(3000)
    page.screenshot(path="logs/dash_range.png")
    print("Range tab screenshot saved")

    browser.close()
