"""Ask for the Foursquare access token (hidden while pasting) and save it in .env as FOURSQUARE_TOKEN."""

import getpass
import pathlib
import re

token = getpass.getpass("Paste the Foursquare token (nothing will show), then press Return: ").strip()
env = pathlib.Path(__file__).resolve().parent.parent / ".env"
text = env.read_text()
if re.search(r"(?m)^FOURSQUARE_TOKEN=", text):
    text = re.sub(r"(?m)^FOURSQUARE_TOKEN=.*$", lambda m: "FOURSQUARE_TOKEN=" + token, text)
else:
    text = text.rstrip("\n") + "\nFOURSQUARE_TOKEN=" + token + "\n"
env.write_text(text)
print(f"Saved ({len(token)} characters)." if token else "Nothing was pasted. Run it again.")
