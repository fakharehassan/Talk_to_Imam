"""
Step 1 — Nahj ul-Balagha Extraction (delimiter parsing + count verification)

WHAT THIS FIXES vs the original parser:
- Original regex required "Hadith n. <N>" to be the ENTIRE line.
- Real book has a variant: "Hadith n. <N>: Some Topic Title" (confirmed via
  outside source, not guessed) — the original would have silently failed to
  recognize these as delimiters, merging that hadith's content into the
  PREVIOUS one instead of starting a new chunk.

WHAT THIS DOES NOT YET FIX (deliberately — one problem at a time):
- The suspected "explanation section" after hadith 260 that's swallowing
  extra content (needs your input on what that section's heading actually
  looks like before we can design the right rule).

HOW TO JUDGE THE OUTPUT:
1. Check "Expected 480, found X" — if X != 480, something is still wrong.
2. Check the "missing numbers" list — every number in 1-480 not captured.
3. Check the "duplicate numbers" list — if a number was captured more than
   once, something merged incorrectly.
"""

import re
import json
import unicodedata
import pdfplumber

NON_ENGLISH_RANGES = re.compile(
    r'[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF\uE000-\uF8FF\u200E\u200F\u202A-\u202E]'
)


def is_arabic_majority(line: str) -> bool:
    stripped = line.replace(" ", "")
    if not stripped:
        return False
    non_english_chars = len(NON_ENGLISH_RANGES.findall(stripped))
    return non_english_chars / len(stripped) > 0.3


def extract_pages(pdf_path: str) -> list[str]:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            text = unicodedata.normalize("NFKC", text)
            text = re.sub(r'[\u200E\u200F\u202A-\u202E]', '', text)
            pages.append(text)
    return pages


def parse_nahj(pages: list[str]) -> list[dict]:
    # UPDATED regex: number is followed by end-of-line OR ": <anything>"
    # This is the fix for the "Hadith n. 31: Faith, Unbelief..." variant.
    marker_pattern = re.compile(r'^Hadith n\.\s*(\d+)\s*(?::\s*(.*))?$')

    chunks = []
    current_number = None
    current_topic_title = None
    current_lines = []

    def clean_text(t: str) -> str:
        t = re.sub(r'^\d+\.\s*', '', t)
        t = re.sub(r'\s+\d+\.?\s*$', '', t)
        return re.sub(r'\s+', ' ', t).strip()

    def flush():
        if current_number is not None and current_lines:
            full_text = clean_text(" ".join(current_lines))
            if full_text:
                chunks.append({
                    "source_book": "Nahj ul-Balagha",
                    "saying_number": current_number,
                    "topic_title": current_topic_title,  # None unless a "Hadith n. X: Title" variant was seen
                    "text": full_text,
                })

    for page_num, page_text in enumerate(pages, start=1):
        for line in page_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            marker = marker_pattern.match(line)
            if marker:
                flush()
                current_number = int(marker.group(1))
                current_topic_title = marker.group(2) or None
                current_lines = []
                continue
            if is_arabic_majority(line):
                continue
            if current_number is not None:
                current_lines.append(line)

    flush()
    return chunks


def verify_count(chunks: list[dict], expected_total: int = 480):
    found_numbers = sorted(c["saying_number"] for c in chunks)
    found_set = set(found_numbers)
    expected_set = set(range(1, expected_total + 1))

    missing = sorted(expected_set - found_set)
    unexpected = sorted(found_set - expected_set)  # numbers outside 1-480 range (parsing artifacts)

    # detect duplicates
    seen = set()
    duplicates = set()
    for n in found_numbers:
        if n in seen:
            duplicates.add(n)
        seen.add(n)

    print(f"Expected {expected_total} sayings, found {len(chunks)} chunks covering {len(found_set)} distinct numbers.")
    print(f"Missing numbers ({len(missing)}): {missing}")
    print(f"Unexpected numbers outside 1-{expected_total} ({len(unexpected)}): {unexpected}")
    print(f"Duplicate numbers ({len(duplicates)}): {sorted(duplicates)}")

    if missing:
        print("\n--> Any missing number needs manual investigation: check that page in the PDF directly.")
    if duplicates:
        print("\n--> Duplicates mean the SAME number was captured twice, likely once correctly and once")
        print("    as a merge/leak. Inspect both entries.")


if __name__ == "__main__":
    pdf_path = "nahj.pdf"  # adjust to your actual filename/path
    print(f"Extracting pages from {pdf_path}...")
    pages = extract_pages(pdf_path)
    print(f"Extracted {len(pages)} pages.\n")

    chunks = parse_nahj(pages)
    verify_count(chunks, expected_total=480)

    with open("nahj_chunks_step1.json", "w") as f:
        json.dump(chunks, f, indent=2)
    print(f"\nSaved {len(chunks)} chunks to nahj_chunks_step1.json")

    # Show any chunk with a topic_title, so you can confirm the colon-variant fix actually caught something
    titled = [c for c in chunks if c["topic_title"]]
    print(f"\nChunks with a topic subtitle (colon variant): {len(titled)}")
    for c in titled[:5]:
        print(f"  #{c['saying_number']}: {c['topic_title']}")