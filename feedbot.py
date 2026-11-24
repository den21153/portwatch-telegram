"""PortWatch VI news -> Telegram.

Reads the site's RSS feed, sends only items that were not seen before, and keeps
the list of seen items in state.json so the next run knows what is new.

Environment:
    TELEGRAM_TOKEN    bot token from @BotFather
    TELEGRAM_CHAT_ID  where to send (your chat id)

Flags:
    --dry-run         print messages instead of sending, and use a scratch state

Standard library only: no packages to install in the workflow.
"""

import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = "https://portwatch-vi.xyz/news/feed.xml"
STATE_FILE = "state.json"
MAX_SEEN = 300

DRY_RUN = "--dry-run" in sys.argv
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if DRY_RUN:
    STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.dryrun.json")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "portwatch-telegram/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_feed(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    items = []
    for node in root.iter("item"):
        guid = (node.findtext("guid") or node.findtext("link") or "").strip()
        if not guid:
            continue
        items.append(
            {
                "guid": guid,
                "title": (node.findtext("title") or "").strip(),
                "link": (node.findtext("link") or "").strip(),
                "summary": (node.findtext("description") or "").strip(),
                "date": (node.findtext("pubDate") or "").strip(),
                "category": (node.findtext("category") or "").strip(),
            }
        )
    return items


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"initialized": False, "seen": []}
    data.setdefault("seen", [])
    data.setdefault("initialized", False)
    return data


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def send(text: str) -> None:
    if DRY_RUN:
        print("--- would send ---")
        print(text)
        print()
        return
    payload = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=payload
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if not body.get("ok"):
        raise RuntimeError(f"telegram error: {body}")


def format_item(item: dict) -> str:
    # Telegram's HTML mode only needs <, > and & escaped; keep quotes readable.
    esc = lambda value: html.escape(value, quote=False)
    parts = [f"<b>{esc(item['title'])}</b>"]
    if item["category"]:
        parts.append(f"<i>{esc(item['category'])}</i>")
    if item["summary"]:
        parts.append(esc(item["summary"]))
    parts.append(item["link"])
    return "\n\n".join(parts)


def main() -> int:
    if not DRY_RUN and not (TOKEN and CHAT_ID):
        print(
            "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(repository Settings -> Secrets -> Actions), or run with --dry-run",
            file=sys.stderr,
        )
        return 2

    items = parse_feed(fetch(FEED_URL))
    if not items:
        print("feed is empty, nothing to do")
        return 0

    state = load_state()
    seen = set(state["seen"])

    if not state["initialized"]:
        # First run: remember the backlog and announce, instead of sending 19
        # old articles at once.
        state["seen"] = [item["guid"] for item in items][:MAX_SEEN]
        state["initialized"] = True
        save_state(state)
        send(
            "<b>PortWatch VI</b>: уведомления включены.\n\n"
            f"Новостей в ленте сейчас: {len(items)}. "
            "Дальше буду присылать только новое."
        )
        return 0

    fresh = [item for item in items if item["guid"] not in seen]
    if not fresh:
        print("no new items")
        return 0

    for item in reversed(fresh):  # oldest first, so the chat reads in order
        send(format_item(item))
        if not DRY_RUN:
            time.sleep(1.2)  # Telegram allows about one message per second per chat

    state["seen"] = ([item["guid"] for item in fresh] + state["seen"])[:MAX_SEEN]
    save_state(state)
    print(f"sent {len(fresh)} item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
