"""Artists and artist wildcards. An artist tag is not a scene detail: it is only used when the
user asked for one, so it needs one plain rule for both the tag check and the wildcard lists.

An artist counts as asked for when the request says "artist" (for example "use artist wildcards" or
"an artist I like"); a name typed with `@` or "by Name" is the user's own tag and is kept by the existing
typed-text rule. The number asked for ("3 artists") caps how many may reach one prompt; one by default.
"""
import re

ARTIST = re.compile(r"\b(artists?|art\s+styles?)\b", re.I)
COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
ASKED = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:\w+\s+){0,2}artists?\b", re.I)


def wants_artists(text: str) -> bool:
    """Does the text ask for artists at all? Without this no artist list is offered and no artist tag is written."""
    return bool(ARTIST.search(text or ""))


def asked_artists(text: str) -> int:
    """How many artists the text asks for: its number ("3 artists") or 1 when it only says "artist"."""
    if not wants_artists(text):
        return 0
    m = ASKED.search(text or "")
    if not m:
        return 1
    value = m.group(1).lower()
    return COUNT_WORDS.get(value, int(value) if value.isdigit() else 1)
