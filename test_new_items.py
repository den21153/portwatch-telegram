import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import feedbot

items = feedbot.parse_feed(feedbot.fetch(feedbot.FEED_URL))
state = {"initialized": True, "seen": [i["guid"] for i in items[2:]]}
pathlib.Path("state.dryrun.json").write_text(
    json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"feed items: {len(items)}, pretending {len(items) - 2} are already seen\n")

out = subprocess.run(
    [sys.executable, "feedbot.py", "--dry-run"], capture_output=True, text=True, encoding="utf-8"
)
print(out.stdout)
print("stderr:", out.stderr.strip())
