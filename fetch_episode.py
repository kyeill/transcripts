"""Find the latest Sunday service and its media links.

christchurchgr.org is a client-rendered Next.js app - the sermons list and
episode pages carry no real links in their static HTML, only after the page
hydrates. A headless browser is used to read the real DOM instead of
reverse-engineering the site's private API.

Not every episode is a Sunday service. Classes and seminars are posted to the
same feed with only a handout - no service recording, no worship guide - so
the newest episode is not necessarily the one to transcribe.
"""
from playwright.sync_api import sync_playwright

BASE = "https://www.christchurchgr.org"
SERMONS_URL = f"{BASE}/sermons"

# the list renders a placeholder link before the real ones hydrate; waiting on
# a bare /episode/ match would catch that and find nothing behind it
REAL_EPISODE = 'a[href*="/episode/"]:not([href$="undefined"])'
SERVICE_AUDIO = 'a:has-text("Listen to the entire service")'
WORSHIP_GUIDE = 'a:has-text("Worship Guide")'

EPISODES_TO_CHECK = 6
PAGE_TIMEOUT_MS = 60_000
HYDRATE_MS = 2_500


def _open(page, url):
    # "networkidle" can sit on this site until it times out; wait for the
    # content that actually matters instead
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)


def _href(page, selector):
    element = page.query_selector(selector)
    return element.get_attribute("href") if element else None


def _assets_on_page(page):
    return (
        page.title().split("|")[0].strip(),
        _href(page, SERVICE_AUDIO),
        _href(page, WORSHIP_GUIDE),
    )


def latest_service():
    """The most recent episode that is a full Sunday service.

    Returns (url, title, audio_url, worship_guide_url), or None when none of
    the recent episodes has a service recording yet - which is the ordinary
    state of things between the service and the recording being posted, not a
    failure.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            _open(page, SERMONS_URL)
            page.wait_for_selector(REAL_EPISODE, timeout=45_000)
            hrefs = page.eval_on_selector_all(
                REAL_EPISODE, "els => els.map(e => e.getAttribute('href'))"
            )
            ordered = list(dict.fromkeys(h for h in hrefs if h))
            if not ordered:
                raise RuntimeError(f"no episodes found on {SERMONS_URL}")

            for href in ordered[:EPISODES_TO_CHECK]:
                url = BASE + href
                _open(page, url)
                page.wait_for_timeout(HYDRATE_MS)
                title, audio_url, pdf_url = _assets_on_page(page)
                if not audio_url:
                    print(f"skipping {href} - not a full service recording")
                    continue
                return url, title, audio_url, pdf_url
            return None
        finally:
            browser.close()


def episode_assets(episode_url):
    """Return (title, audio_url, worship_guide_url) for one specific episode."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            _open(page, episode_url)
            page.wait_for_timeout(HYDRATE_MS)
            title, audio_url, pdf_url = _assets_on_page(page)
        finally:
            browser.close()
    if not audio_url:
        raise RuntimeError(f"no full-service audio link found on {episode_url}")
    return title, audio_url, pdf_url


if __name__ == "__main__":
    found = latest_service()
    if found is None:
        print("no full service recording posted yet")
    else:
        url, title, audio_url, pdf_url = found
        print("episode:", url)
        print("title:", title)
        print("audio:", audio_url)
        print("worship guide:", pdf_url)
