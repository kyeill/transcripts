"""Resolve the latest sermon episode and its media links.

christchurchgr.org is a client-rendered Next.js app - the sermons list and
episode pages carry no real links in their static HTML, only after the page
hydrates. A headless browser is used to read the real DOM instead of
reverse-engineering the site's private API.
"""
from playwright.sync_api import sync_playwright

SERMONS_URL = "https://www.christchurchgr.org/sermons"


def latest_episode_url():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(SERMONS_URL, wait_until="networkidle")
        href = page.eval_on_selector('a[href*="/episode/"]', "el => el.getAttribute('href')")
        browser.close()
    if not href or href.endswith("/undefined"):
        raise RuntimeError(f"could not resolve latest episode from {SERMONS_URL}")
    return "https://www.christchurchgr.org" + href


def episode_assets(episode_url):
    """Return (title, audio_url, worship_guide_pdf_url) for an episode page."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(episode_url, wait_until="networkidle")
        title = page.title().split("|")[0].strip()
        audio_url = page.eval_on_selector(
            'a:has-text("Listen to the entire service")', "el => el.getAttribute('href')"
        )
        pdf_url = page.eval_on_selector(
            'a:has-text("Worship Guide")', "el => el.getAttribute('href')"
        )
        browser.close()
    if not audio_url:
        raise RuntimeError(f"no full-service audio link found on {episode_url}")
    return title, audio_url, pdf_url


if __name__ == "__main__":
    url = latest_episode_url()
    print("latest episode:", url)
    title, audio_url, pdf_url = episode_assets(url)
    print("title:", title)
    print("audio:", audio_url)
    print("worship guide:", pdf_url)
