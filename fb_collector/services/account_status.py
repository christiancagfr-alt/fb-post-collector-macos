from urllib.parse import urlparse
import logging
import sqlite3

from .. import db


def record_status(account_id, status, driver=None):
    if not account_id:
        return
    values = {"facebook_login_status": status, "last_checked_at": db.utc_now()}
    if driver is not None and status == "logged_in":
        try:
            # Restrict to the account menu, not avatars of post authors or friends.
            avatar = driver.execute_script("""
                const selectors = ['[role="banner"] [aria-label="Your profile"]',
                    '[role="banner"] [aria-label="你的个人主页"]',
                    '[role="banner"] [aria-label="你的個人檔案"]',
                    '[role="banner"] [aria-label="Seu perfil"]',
                    '[role="banner"] [aria-label="Votre profil"]',
                    '[role="banner"] [aria-label="Account"]',
                    '[role="banner"] [aria-label="账户"]'];
                for (const selector of selectors) {
                    const node = document.querySelector(selector);
                    const image = node && node.querySelector('image, img');
                    if (image) return image.getAttribute('href') || image.getAttribute('xlink:href') || image.src || '';
                }
                return '';
            """)
            parsed = urlparse(avatar or "")
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and any(host == root or host.endswith('.' + root) for root in ("fbcdn.net", "facebook.com", "fbsbx.com")):
                values["avatar_url"] = avatar
        except Exception:
            pass
    try:
        db.update_browser_account(account_id, values)
    except sqlite3.OperationalError:
        # Display metadata must not abort a scrape or hide the original login error.
        logging.getLogger(__name__).exception("Unable to persist browser account status for %s", account_id)
