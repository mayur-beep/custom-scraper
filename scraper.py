"""
Custom RSS Feed Generator for JavaScript-rendered websites
Uses Playwright to render JS and generate RSS feeds
"""

from flask import Flask, Response, request
from playwright.sync_api import sync_playwright, Error as PlaywrightError
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone
from urllib.parse import urljoin, quote
import atexit
import hashlib
import json
import logging
import os
import re
import threading

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache to avoid re-scraping too frequently
cache = {}
CACHE_DURATION = 600  # 10 minutes

# --- Persistent Browser Singleton ---

_playwright = None
_browser = None
_browser_lock = threading.Lock()

CHROMIUM_ARGS = [
    "--single-process",
    "--no-zygote",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
    "--hide-scrollbars",
]


def _get_browser():
    """Get or create the persistent browser instance."""
    global _playwright, _browser

    if _browser and _browser.is_connected():
        return _browser

    # Browser is dead or never started -- (re)launch
    _force_restart_browser()
    return _browser


def _force_restart_browser():
    """Unconditionally kill and relaunch the browser."""
    global _playwright, _browser

    logger.info("(Re)launching Chromium browser...")

    # Clean up old instances
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=True,
        args=CHROMIUM_ARGS,
    )
    logger.info("Browser launched successfully.")


def _shutdown_browser():
    """Clean up browser on process exit."""
    global _playwright, _browser
    logger.info("Shutting down browser...")
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
    if _playwright:
        try:
            _playwright.stop()
        except Exception:
            pass


atexit.register(_shutdown_browser)


def _is_browser_crash(error: Exception) -> bool:
    """Check if an error indicates the browser process died."""
    msg = str(error).lower()
    return any(keyword in msg for keyword in [
        "target closed", "browser has been closed", "connection closed",
        "target page, context or browser has been closed",
        "browser.newpage", "page.goto",
    ])


def _scrape_page(url: str, config: dict) -> list:
    """
    Core scraping logic -- opens a page, scrapes items, closes page.
    Must be called while holding _browser_lock.
    """
    items = []
    browser = _get_browser()
    page = browser.new_page()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)  # Extra wait for dynamic content

        # Find all items
        elements = page.query_selector_all(config.get("item_selector", "article"))

        for element in elements[:20]:  # Limit to 20 items
            try:
                item = {}

                # Get title
                title_el = element.query_selector(config.get("title_selector", "h2, h3, h4"))
                if title_el:
                    item["title"] = title_el.inner_text().strip()

                # Get link
                link_el = element.query_selector(config.get("link_selector", "a"))
                if link_el:
                    href = link_el.get_attribute("href")
                    if href:
                        item["link"] = urljoin(url, href)

                # Get description (optional)
                desc_selector = config.get("description_selector")
                if desc_selector:
                    desc_el = element.query_selector(desc_selector)
                    if desc_el:
                        item["description"] = desc_el.inner_text().strip()

                # Get image (optional)
                img_selector = config.get("image_selector")
                if img_selector:
                    img_el = element.query_selector(img_selector)
                    if img_el:
                        item["image"] = img_el.get_attribute("src")

                # Get date (optional)
                date_selector = config.get("date_selector")
                if date_selector:
                    date_el = element.query_selector(date_selector)
                    if date_el:
                        date_text = date_el.inner_text().strip()
                        date_fmt = config.get("date_format", "%d-%m-%Y")
                        try:
                            item["date"] = datetime.strptime(date_text, date_fmt).replace(tzinfo=timezone.utc)
                        except ValueError:
                            # Try to extract a date pattern from the text
                            date_match = re.search(r'\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}', date_text)
                            if date_match:
                                try:
                                    item["date"] = datetime.strptime(date_match.group(), date_fmt).replace(tzinfo=timezone.utc)
                                except ValueError:
                                    pass

                if item.get("title") and item.get("link"):
                    items.append(item)

            except Exception as e:
                logger.warning(f"Error parsing item: {e}")
                continue

    finally:
        try:
            page.close()
        except Exception:
            pass

    return items


def scrape_js_website(url: str, config: dict) -> list:
    """
    Scrape with automatic retry on browser crash.
    If the browser dies mid-request, restart it and retry once.
    """
    max_attempts = 2

    with _browser_lock:
        for attempt in range(1, max_attempts + 1):
            try:
                return _scrape_page(url, config)
            except Exception as e:
                if attempt < max_attempts and _is_browser_crash(e):
                    logger.warning(f"Browser crashed on attempt {attempt}, restarting: {e}")
                    _force_restart_browser()
                    continue
                raise


def generate_rss(items: list, feed_title: str, feed_url: str) -> str:
    """Generate RSS XML from scraped items"""
    fg = FeedGenerator()
    fg.title(feed_title)
    fg.link(href=feed_url)
    fg.description(f"Custom RSS feed for {feed_url}")
    fg.language("en")

    for item in items:
        fe = fg.add_entry()
        fe.title(item.get("title", "No title"))
        fe.link(href=item.get("link", feed_url))
        fe.guid(item.get("link", ""), permalink=True)

        if item.get("description"):
            fe.description(item["description"])

        if item.get("image"):
            fe.enclosure(item["image"], 0, "image/jpeg")

        fe.published(item.get("date", datetime.now(timezone.utc)))

    return fg.rss_str(pretty=True).decode("utf-8")


@app.route("/feed")
def create_feed():
    """
    Create RSS feed from any website

    Query params:
    - url: Website URL to scrape
    - item: CSS selector for items (default: article)
    - title: CSS selector for title (default: h2, h3, h4)
    - link: CSS selector for link (default: a)
    - desc: CSS selector for description (optional)
    - img: CSS selector for image (optional)
    - date: CSS selector for date (optional)
    - datefmt: Date format string (default: %d-%m-%Y, i.e. DD-MM-YYYY)

    Example:
    /feed?url=https://example.com&item=.post&title=h2&link=a&date=.date&datefmt=%d-%m-%Y
    """
    url = request.args.get("url")
    if not url:
        return "Missing 'url' parameter", 400

    config = {
        "item_selector": request.args.get("item", "article"),
        "title_selector": request.args.get("title", "h2, h3, h4"),
        "link_selector": request.args.get("link", "a"),
        "description_selector": request.args.get("desc"),
        "image_selector": request.args.get("img"),
        "date_selector": request.args.get("date"),
        "date_format": request.args.get("datefmt", "%d-%m-%Y"),
    }

    # Check cache
    cache_key = hashlib.md5(f"{url}{json.dumps(config)}".encode()).hexdigest()
    cached = cache.get(cache_key)

    if cached and (datetime.now().timestamp() - cached["time"]) < CACHE_DURATION:
        return Response(cached["data"], mimetype="application/rss+xml")

    # Scrape and generate
    try:
        items = scrape_js_website(url, config)
    except TimeoutError:
        logger.error(f"Timeout scraping {url}")
        return "Scraping timed out. The target page took too long to load.", 504
    except PlaywrightError as e:
        error_msg = str(e)
        if "Target" in error_msg and "closed" in error_msg:
            logger.error(f"Browser target closed while scraping {url}: {e}")
            return "Browser error (target closed). Please retry.", 503
        logger.error(f"Playwright error for {url}: {e}")
        return "Scraping failed due to a browser error. Please retry.", 503
    except Exception as e:
        logger.error(f"Unexpected error scraping {url}: {e}")
        return "Internal scraping error. Please retry.", 500

    if not items:
        return "No items found. Check your CSS selectors.", 404

    rss = generate_rss(items, f"Feed: {url}", url)

    # Update cache
    cache[cache_key] = {"data": rss, "time": datetime.now().timestamp()}

    return Response(rss, mimetype="application/rss+xml")


@app.route("/debug")
def debug_page():
    """Debug endpoint to see page structure"""
    url = request.args.get("url")
    if not url:
        return "Missing 'url' parameter", 400

    with _browser_lock:
        last_error = None
        for attempt in range(1, 3):
            page = None
            try:
                browser = _get_browser()
                page = browser.new_page()

                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)

                # Get page HTML
                html = page.content()

                # Find all potential item containers
                selectors_to_try = [
                    "article", ".article", ".post", ".story", ".card",
                    ".story-card", ".news-item", ".entry", "[class*='story']",
                    "[class*='article']", "[class*='post']", "[class*='card']"
                ]

                results = []
                for selector in selectors_to_try:
                    elements = page.query_selector_all(selector)
                    if elements:
                        results.append(f"{selector}: {len(elements)} elements found")

                return f"""
                <html>
                <head><title>Debug: {url}</title></head>
                <body style="font-family: monospace;">
                    <h1>Debug: {url}</h1>
                    <h2>Potential selectors found:</h2>
                    <pre>{"<br>".join(results) if results else "No common selectors found"}</pre>
                    <h2>Page HTML (first 10000 chars):</h2>
                    <textarea style="width:100%; height:500px;">{html[:10000]}</textarea>
                </body>
                </html>
                """
            except TimeoutError:
                return "Debug timed out. The target page took too long to load.", 504
            except Exception as e:
                last_error = e
                if attempt < 2 and _is_browser_crash(e):
                    logger.warning(f"Browser crashed during debug (attempt {attempt}), restarting: {e}")
                    _force_restart_browser()
                    continue
                return f"Error: {e}", 500
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass

        return f"Error after retries: {last_error}", 500


@app.route("/")
def home():
    """Homepage with usage instructions"""
    return """
    <html>
    <head><title>Custom RSS Generator</title></head>
    <body style="font-family: Arial; max-width: 800px; margin: 50px auto; padding: 20px;">
        <h1>Custom RSS Feed Generator</h1>
        <p>Generate RSS feeds from JavaScript-rendered websites.</p>

        <h2>Usage</h2>
        <pre style="background: #f4f4f4; padding: 15px; border-radius: 5px;">
GET /feed?url=WEBSITE_URL&item=CSS_SELECTOR&title=CSS_SELECTOR&link=CSS_SELECTOR

Parameters:
- url     (required): Website URL to scrape
- item    (optional): CSS selector for each item (default: article)
- title   (optional): CSS selector for title (default: h2, h3, h4)
- link    (optional): CSS selector for link (default: a)
- desc    (optional): CSS selector for description
- img     (optional): CSS selector for image
- date    (optional): CSS selector for date
- datefmt (optional): Date format (default: %d-%m-%Y i.e. DD-MM-YYYY)
        </pre>

        <h2>Example</h2>
        <pre style="background: #f4f4f4; padding: 15px; border-radius: 5px;">
/feed?url=https://news.bitcoin.com/press-releases/&item=.story-card&title=h6&link=a&img=img&date=.date&datefmt=%d-%m-%Y
        </pre>

        <h2>Try It</h2>
        <form action="/feed" method="get" style="background: #f9f9f9; padding: 20px; border-radius: 5px;">
            <p><label>URL: <input type="text" name="url" style="width: 400px;" placeholder="https://example.com"></label></p>
            <p><label>Item selector: <input type="text" name="item" value="article" style="width: 200px;"></label></p>
            <p><label>Title selector: <input type="text" name="title" value="h2, h3, h4" style="width: 200px;"></label></p>
            <p><label>Link selector: <input type="text" name="link" value="a" style="width: 200px;"></label></p>
            <p><label>Description selector: <input type="text" name="desc" style="width: 200px;"></label></p>
            <p><label>Image selector: <input type="text" name="img" style="width: 200px;"></label></p>
            <p><label>Date selector: <input type="text" name="date" style="width: 200px;" placeholder=".date, time, .timestamp"></label></p>
            <p><label>Date format: <input type="text" name="datefmt" value="%d-%m-%Y" style="width: 200px;" placeholder="%d-%m-%Y"></label>
                <small style="color: #666;">(DD-MM-YYYY)</small></label></p>
            <p><button type="submit">Generate Feed</button></p>
        </form>
    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
