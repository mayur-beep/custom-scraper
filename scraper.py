"""
Custom RSS Feed Generator for JavaScript-rendered websites
Uses Playwright to render JS and generate RSS feeds
"""

from flask import Flask, Response, request
from playwright.sync_api import sync_playwright
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone
from urllib.parse import urljoin, quote
import hashlib
import json
import os

app = Flask(__name__)

# Cache to avoid re-scraping too frequently
cache = {}
CACHE_DURATION = 600  # 10 minutes


def scrape_js_website(url: str, config: dict) -> list:
    """
    Scrape a JavaScript-rendered website using Playwright

    config = {
        "item_selector": "article",           # CSS selector for each item
        "title_selector": "h2",               # CSS selector for title within item
        "link_selector": "a",                 # CSS selector for link within item
        "description_selector": ".summary",   # CSS selector for description (optional)
        "image_selector": "img",              # CSS selector for image (optional)
    }
    """
    items = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                    if item.get("title") and item.get("link"):
                        items.append(item)

                except Exception as e:
                    print(f"Error parsing item: {e}")
                    continue

        except Exception as e:
            print(f"Error scraping {url}: {e}")
        finally:
            browser.close()

    return items


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

        fe.published(datetime.now(timezone.utc))

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

    Example:
    /feed?url=https://example.com&item=.post&title=h2&link=a
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
    }

    # Check cache
    cache_key = hashlib.md5(f"{url}{json.dumps(config)}".encode()).hexdigest()
    cached = cache.get(cache_key)

    if cached and (datetime.now().timestamp() - cached["time"]) < CACHE_DURATION:
        return Response(cached["data"], mimetype="application/rss+xml")

    # Scrape and generate
    items = scrape_js_website(url, config)

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
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

            browser.close()

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
        except Exception as e:
            browser.close()
            return f"Error: {e}", 500


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
- url   (required): Website URL to scrape
- item  (optional): CSS selector for each item (default: article)
- title (optional): CSS selector for title (default: h2, h3, h4)
- link  (optional): CSS selector for link (default: a)
- desc  (optional): CSS selector for description
- img   (optional): CSS selector for image
        </pre>

        <h2>Example</h2>
        <pre style="background: #f4f4f4; padding: 15px; border-radius: 5px;">
/feed?url=https://news.bitcoin.com/press-releases/&item=.story-card&title=h6&link=a&img=img
        </pre>

        <h2>Try It</h2>
        <form action="/feed" method="get" style="background: #f9f9f9; padding: 20px; border-radius: 5px;">
            <p><label>URL: <input type="text" name="url" style="width: 400px;" placeholder="https://example.com"></label></p>
            <p><label>Item selector: <input type="text" name="item" value="article" style="width: 200px;"></label></p>
            <p><label>Title selector: <input type="text" name="title" value="h2, h3, h4" style="width: 200px;"></label></p>
            <p><label>Link selector: <input type="text" name="link" value="a" style="width: 200px;"></label></p>
            <p><label>Description selector: <input type="text" name="desc" style="width: 200px;"></label></p>
            <p><label>Image selector: <input type="text" name="img" style="width: 200px;"></label></p>
            <p><button type="submit">Generate Feed</button></p>
        </form>
    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
