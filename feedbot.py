"""PortWatch VI -> Telegram.

Two sources, one bot:

* the RSS feed at /news/feed.xml - the site's news articles;
* /data/tracker.json - the change feed, DMCA notices and the executable status.

Only new entries are sent. Everything already sent is remembered in state.json,
which the GitHub workflow commits back, so a later run knows what is new.

Environment:
    TELEGRAM_TOKEN    bot token from @BotFather
    TELEGRAM_CHAT_ID  where to send (your chat id)

Flags:
    --dry-run         print messages instead of sending, and use a scratch state

Standard library only: nothing to install in the workflow.
"""

import hashlib
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = "https://portwatch-vi.xyz/news/feed.xml"
TRACKER_URL = "https://portwatch-vi.xyz/data/tracker.json"
SITE_URL = "https://portwatch-vi.xyz"
STATE_FILE = "state.json"
MAX_SEEN = 300
MAX_CHANGE_LINES = 12

DRY_RUN = "--dry-run" in sys.argv
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if DRY_RUN:
    STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.dryrun.json")


def esc(value: str) -> str:
    return html.escape(value or "", quote=False)


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "portwatch-telegram/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_tracker() -> dict | None:
    # A tracker hiccup must not take the news feed down with it, so failures
    # here are reported as "nothing new" and the state is left untouched.
    try:
        return json.loads(fetch(TRACKER_URL))
    except Exception as error:  # noqa: BLE001 - any network/parse issue is non-fatal
        print(f"tracker unavailable: {error}", file=sys.stderr)
        return None


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
                "category": (node.findtext("category") or "").strip(),
            }
        )
    return items


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("initialized", False)
    data.setdefault("seen", [])
    data.setdefault("changes", [])
    data.setdefault("dmca", [])
    data.setdefault("exeStatus", "")
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


def format_news(item: dict) -> str:
    parts = [f"<b>{esc(item['title'])}</b>"]
    if item["category"]:
        parts.append(f"<i>{esc(item['category'])}</i>")
    if item["summary"]:
        parts.append(esc(item["summary"]))
    parts.append(item["link"])
    return "\n\n".join(parts)


def tracker_messages(state: dict, tracker: dict) -> list[str]:
    """Build messages for the tracker and update the matching state fields."""
    messages = []

    seen_changes = set(state["changes"])
    fresh = []
    for change in tracker.get("changes") or []:
        text = (change.get("text") or "").strip()
        if not text:
            continue
        key = fingerprint(change.get("date", "") + "|" + text)
        if key not in seen_changes:
            fresh.append((key, text))

    if fresh:
        lines = [f"• {esc(text)}" for _, text in fresh[:MAX_CHANGE_LINES]]
        if len(fresh) > MAX_CHANGE_LINES:
            lines.append(f"… и ещё {len(fresh) - MAX_CHANGE_LINES}")
        messages.append(
            "<b>PortWatch: что изменилось</b>\n\n" + "\n".join(lines) + f"\n\n{SITE_URL}/#changes"
        )
        state["changes"] = ([key for key, _ in fresh] + state["changes"])[:400]

    seen_dmca = set(state["dmca"])
    for notice in tracker.get("dmca") or []:
        url = (notice.get("url") or "").strip()
        if not url or url in seen_dmca:
            continue
        tracked = ", ".join(notice.get("tracked") or []) or "—"
        messages.append(
            "<b>DMCA-уведомление</b>\n\n"
            f"Затронутые проекты: {esc(tracked)}\n"
            f"Дата: {esc(notice.get('date', ''))}\n\n{url}"
        )
        state["dmca"].append(url)

    exe = tracker.get("exe") or {}
    status = (exe.get("status") or "").strip()
    if status and status != state["exeStatus"]:
        if state["exeStatus"]:  # do not announce the state we start from
            note = (exe.get("note") or "").strip()
            messages.append(
                f"<b>Статус exe на PortWatch: {esc(status)}</b>"
                + (f"\n\n{esc(note)}" if note else "")
                + f"\n\n{SITE_URL}/#top"
            )
        state["exeStatus"] = status

    return messages


def main() -> int:
    if not DRY_RUN and not (TOKEN and CHAT_ID):
        print(
            "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(repository Settings -> Secrets -> Actions), or run with --dry-run",
            file=sys.stderr,
        )
        return 2

    items = parse_feed(fetch(FEED_URL))
    tracker = fetch_tracker()
    state = load_state()

    if not state["initialized"]:
        # First run: remember the backlog and announce once, instead of sending
        # dozens of old entries.
        state["seen"] = [item["guid"] for item in items][:MAX_SEEN]
        state["initialized"] = True
        if tracker:
            for change in tracker.get("changes") or []:
                text = (change.get("text") or "").strip()
                if text:
                    state["changes"].append(
                        fingerprint(change.get("date", "") + "|" + text)
                    )
            state["changes"] = state["changes"][:400]
            state["dmca"] = [n.get("url", "") for n in tracker.get("dmca") or [] if n.get("url")]
            state["exeStatus"] = ((tracker.get("exe") or {}).get("status") or "").strip()
        save_state(state)
        send(
            "<b>PortWatch VI</b>: уведомления включены.\n\n"
            f"Новостей в ленте сейчас: {len(items)}. "
            "Дальше буду присылать только новое: статьи, изменения по проектам "
            "и DMCA-уведомления."
        )
        return 0

    sent = 0

    if tracker:
        for message in tracker_messages(state, tracker):
            send(message)
            sent += 1
            if not DRY_RUN:
                time.sleep(1.2)

    seen = set(state["seen"])
    fresh_news = [item for item in items if item["guid"] not in seen]
    for item in reversed(fresh_news):  # oldest first, so the chat reads in order
        send(format_news(item))
        sent += 1
        if not DRY_RUN:
            time.sleep(1.2)
    if fresh_news:
        state["seen"] = ([item["guid"] for item in fresh_news] + state["seen"])[:MAX_SEEN]

    save_state(state)
    print(f"sent {sent} message(s)" if sent else "no new items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
