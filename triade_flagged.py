"""
Triage the flagged chunks: separates likely FALSE POSITIVES (short but
grammatically complete sayings — normal for this book) from GENUINELY
SUSPICIOUS ones (missing terminal punctuation, ends mid-word, contains
obvious artifacts) that actually deserve your manual attention.

This does NOT change any text — it only sorts the flagged list into two
review files so you spend your limited time on the ones that matter.
"""

"""
Triage v2 — refined based on real evidence from the first pass:
1. A trailing lone digit right after final punctuation (e.g. "merit.1") is a
   leftover footnote marker, NOT truncation — this is safely auto-fixable,
   no manual review needed, no content lost by stripping it.
2. A trailing fragment matching a REAL topic name from the book's own topic
   list is a boundary-leak bug (the next topic's header bled into this
   saying) — confirmed against actual data, not guessed.
3. Only what's left after removing both of the above is genuine truncation
   needing your manual eyes.
"""

import json
import re

# Paste the real topic list you extracted earlier — used to CONFIRM (not guess)
# whether a trailing fragment is actually a leaked topic header.
KNOWN_TOPICS = [
    "Grandeur", "Certitude", "Self-Sacrifice", "Lust", "Parents", "The Camel",
    "The Son of Adam (The Human Being)",
    # NOTE: paste your FULL topic list here (from the message where you
    # extracted all unique Ghurar topics) — this is a partial placeholder;
    # more entries = more accurate leak detection.
]
# Normalize for comparison: lowercase, strip trailing "-" and whitespace
KNOWN_TOPICS_NORMALIZED = {t.strip().rstrip("-").strip().lower() for t in KNOWN_TOPICS}


def ends_with_leaked_topic(text: str) -> str | None:
    """Checks if the text's final 1-5 words match a known topic name. Returns
    the matched topic if found, else None."""
    words = text.rstrip(".!?\"')]").split()
    for n in range(1, 6):
        if len(words) < n:
            break
        candidate = " ".join(words[-n:]).strip().lower()
        if candidate in KNOWN_TOPICS_NORMALIZED:
            return candidate
    return None


def has_trailing_footnote_digit(text: str) -> bool:
    """Matches a SINGLE trailing digit immediately after sentence-ending
    punctuation, no space — e.g. 'merit.1' — the footnote-marker pattern."""
    return bool(re.search(r'[.!?"\')\]]\d$', text.strip()))


def strip_trailing_footnote_digit(text: str) -> str:
    return re.sub(r'(\d)$', '', text.strip()).strip()


with open("ghurar_enriched.json", encoding="utf-8") as f:
    chunks = json.load(f)

flagged = [c for c in chunks if c.get("flag_status") == "flagged"]
print(f"Total flagged: {len(flagged)}")

topic_leak_bugs = []
footnote_digit_fixable = []
likely_fine_ends_properly = []
genuine_truncation = []

SENTENCE_ENDINGS = (".", "!", "?", '"', "'", ")", "]")

for c in flagged:
    text = c["text"]

    leaked_topic = ends_with_leaked_topic(text)
    if leaked_topic:
        topic_leak_bugs.append({**c, "detected_leaked_topic": leaked_topic})
        continue

    if has_trailing_footnote_digit(text):
        footnote_digit_fixable.append(c)
        continue

    # Bracketed editorial insertions (e.g. "[soon]", "[by this]") are a NORMAL
    # translation convention, not truncation. If the text still ends with
    # proper terminal punctuation, it's very likely a complete, valid saying
    # that the enrichment pass over-flagged just because of the brackets.
    if text.strip().endswith(SENTENCE_ENDINGS):
        likely_fine_ends_properly.append(c)
        continue

    genuine_truncation.append(c)

print(f"\nTopic-leak bugs (confirmed against real topic list): {len(topic_leak_bugs)}")
print(f"Trailing footnote digit (safe auto-fix, not truncation): {len(footnote_digit_fixable)}")
print(f"Likely fine, ends properly (e.g. over-flagged for brackets): {len(likely_fine_ends_properly)}")
print(f"GENUINE truncation (needs your manual review): {len(genuine_truncation)}")

with open("topic_leak_bugs.json", "w", encoding="utf-8") as f:
    json.dump(topic_leak_bugs, f, indent=2, ensure_ascii=False)

with open("footnote_digit_fixable.json", "w", encoding="utf-8") as f:
    json.dump(footnote_digit_fixable, f, indent=2, ensure_ascii=False)

with open("likely_fine_ends_properly.json", "w", encoding="utf-8") as f:
    json.dump(likely_fine_ends_properly, f, indent=2, ensure_ascii=False)

with open("genuine_truncation_review.json", "w", encoding="utf-8") as f:
    json.dump(genuine_truncation, f, indent=2, ensure_ascii=False)

print("\n--- Sample topic-leak bugs ---")
for c in topic_leak_bugs[:5]:
    print(f"  #{c['saying_number']}: \"{c['text']}\" -> leaked topic: '{c['detected_leaked_topic']}'")

print("\n--- Sample footnote-digit (safe to auto-fix) ---")
for c in footnote_digit_fixable[:5]:
    print(f"  #{c['saying_number']}: \"{c['text']}\" -> would become: \"{strip_trailing_footnote_digit(c['text'])}\"")

print("\n--- Sample GENUINE truncation (real manual review) ---")
for c in genuine_truncation[:10]:
    print(f"  #{c['saying_number']}: \"{c['text']}\"")