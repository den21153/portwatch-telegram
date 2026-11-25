"""PortWatch VI -> Telegram.

Two sources, one bot:

* the RSS feed at /news/feed.xml - the site's news articles;
* /data/tracker.json - the change feed, DMCA notices and the executable status.

Only new entries are sent. Translations to Russian go through Google's public
translate endpoint and fall back to the original text when it fails. Everything
already sent is remembered in state.json, which the workflow commits back.

Environment:
    TELEGRAM_TOKEN    bot token from @BotFather
    TELEGRAM_CHAT_ID  where to send (your chat id)

Flags:
    --dry-run         print messages instead of sending, and use a scratch state

Standard library only.
"""

import hashlib
import html
import json
import os
import re
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
MAX_MESSAGE_CHARS = 3500  # Telegram hard limit is 4096; leave room for the header

DRY_RUN = "--dry-run" in sys.argv
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if DRY_RUN:
    STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.dryrun.json")

_TRANSLATIONS: dict[str, str] = {}
_SCOUT_RE = re.compile(r"^Scout spotted (\S+) \(([^,]+), substance (\d+)\)")
_KIND_RU = {"possible effort": "", "tool": "инструмент", "project": "проект"}


def esc(value: str) -> str:
    return html.escape(value or "", quote=False)


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "portwatch-telegram/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_tracker() -> dict | None:
    try:
        return json.loads(fetch(TRACKER_URL))
    except Exception as error:  # noqa: BLE001 - any network/parse issue is non-fatal
        print(f"tracker unavailable: {error}", file=sys.stderr)
        return None


def translate_chunk(text: str) -> str:
    query = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "ru", "dt": "t", "q": text}
    )
    raw = fetch(f"https://translate.googleapis.com/translate_a/single?{query}")
    data = json.loads(raw)
    result = "".join(part[0] for part in data[0] if part and part[0])
    if not result.strip():
        raise RuntimeError("empty translation")
    return result


def translate_chunk_backup(text: str) -> str:
    """Second free provider, used when Google throttles a burst of requests."""
    query = urllib.parse.urlencode({"q": text, "langpair": "en|ru"})
    raw = fetch(f"https://api.mymemory.translated.net/get?{query}")
    data = json.loads(raw)
    result = html.unescape(((data.get("responseData") or {}).get("translatedText") or "")).strip()
    if not result or "MYMEMORY WARNING" in result.upper() or "QUERY LENGTH LIMIT" in result.upper():
        raise RuntimeError("backup translator refused")
    return result


def split_for_translation(text: str, limit: int = 900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current and len(current) + len(sentence) + 1 > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def translate(text: str) -> str:
    """English -> Russian. Google first, a second free provider next, the
    original text as the last resort."""
    text = (text or "").strip()
    if not text:
        return text
    if text in _TRANSLATIONS:
        return _TRANSLATIONS[text]

    for provider in (translate_chunk, translate_chunk_backup):
        for attempt in range(2):
            try:
                time.sleep(1.0)  # the public endpoints throttle bursts
                result = " ".join(
                    provider(chunk) for chunk in split_for_translation(text)
                ).strip()
                if result:
                    _TRANSLATIONS[text] = result
                    return result
            except Exception as error:  # noqa: BLE001 - translation is best effort
                print(
                    f"{provider.__name__} attempt {attempt + 1} failed: {error}",
                    file=sys.stderr,
                )
                time.sleep(1.5)

    _TRANSLATIONS[text] = text
    return text


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
    # A short preview in the workflow log makes it obvious what language a
    # message went out in, without exposing the whole text.
    print("[send] " + text.replace("\n", " ")[:100])
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


def pack_messages(header: str, lines: list[str], footer: str) -> list[str]:
    """Split lines into as few Telegram-sized messages as needed."""
    messages, current = [], ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) + len(header) + len(footer) > MAX_MESSAGE_CHARS and current:
            messages.append(f"{header}\n\n{current}\n\n{footer}")
            current = line
        else:
            current = candidate
    if current:
        messages.append(f"{header}\n\n{current}\n\n{footer}")
    return messages


def format_news(item: dict) -> str:
    parts = [f"<b>{esc(translate(item['title']))}</b>"]
    if item["category"]:
        parts.append(f"<i>{esc(item['category'])}</i>")
    if item["summary"]:
        parts.append(esc(translate(item["summary"])))
    parts.append(item["link"])
    return "\n\n".join(parts)


def tracker_messages(state: dict, tracker: dict) -> list[str]:
    """Build messages for the tracker and update the matching state fields."""
    messages = []

    seen_changes = set(state["changes"])
    candidates, notices = [], []
    for change in tracker.get("changes") or []:
        text = (change.get("text") or "").strip()
        if not text:
            continue
        key = fingerprint(change.get("date", "") + "|" + text)
        if key in seen_changes:
            continue
        state["changes"].append(key)
        match = _SCOUT_RE.match(text)
        if match:
            name, kind, score = match.group(1), match.group(2).strip(), match.group(3)
            label = _KIND_RU.get(kind, translate(kind))
            candidates.append(f"{name} ({score})" if not label else f"{name} — {label} ({score})")
        else:
            notices.append(text)
    state["changes"] = state["changes"][:400]

    if notices:
        lines = [f"• {esc(translate(notice))}" for notice in notices]
        messages.extend(
            pack_messages(
                "<b>PortWatch: что изменилось</b>",
                lines,
                f"{SITE_URL}/#changes",
            )
        )

    if candidates:
        messages.extend(
            pack_messages(
                f"<b>PortWatch: новые кандидаты ({len(candidates)})</b>",
                [f"• {esc(name)}" for name in candidates],
                f"{SITE_URL}/#changes",
            )
        )

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
        if state["exeStatus"]:
            note = (exe.get("note") or "").strip()
            messages.append(
                f"<b>Статус exe на PortWatch: {esc(status)}</b>"
                + (f"\n\n{esc(translate(note))}" if note else "")
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
    for item in reversed(fresh_news):
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
