"""
Step 2 — Isolate Appendix Sections + Join Footnote Explanations by Number

THE PROBLEM THIS SOLVES:
The book has 2 "explanation appendix" sections (confirmed by you: one after
hadith 260, ending before hadith 261; one after hadith 480, running to EOF).
Both are marked by a "*****" line. Without special handling, these sections'
text gets swallowed into hadith 260 and 480 respectively, because the parser
has no "Hadith n." marker to stop at.

THE FIX, IN TWO STAGES:
Stage A - DIVERSION: while "inside" a *****-delimited region, route lines to
a separate appendix buffer instead of the current hadith's text. Since there
are exactly 3 "*****" markers (open, close, open-with-no-close), this is a
simple ON/OFF TOGGLE - no special-casing needed for the "runs to EOF" case,
it falls out naturally from the toggle never being turned off again.

Stage B - JOINING: parse the appendix into Roman-numeral items (I., II., III...),
extract each item's footnote NUMBER (the shared key between the appendix
commentary and the bare number sitting in the main hadith text - e.g. "64" in
both "Ya'sub 64" (main text) and "'Ya'sub' 64 is the great chief..." (appendix)).
Search the 480 main chunks for that exact number as a standalone token. If
found in EXACTLY ONE chunk, insert the explanation in brackets right after
that number. If zero or multiple matches, DO NOT GUESS - log it for manual
review instead (per your rule: don't silently leave sayings/explanations
unaccounted for, but also don't silently attach them wrong).

NOTE: hadith #479-style cases (explanation already inline, e.g. "...observed.
101 As-Sayyid ar-Radi says: ...") are correctly left untouched - the number
search below only matches BARE numbers not already followed by an inline
explanation, so we don't double-process what's already complete.
"""

import re
import json
import unicodedata
import pdfplumber

NON_ENGLISH_RANGES = re.compile(
    r'[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF\uE000-\uF8FF\u200E\u200F\u202A-\u202E]'
)
ASTERISK_TOGGLE = re.compile(r'^\*{3,}$')


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


# ---------- Stage A: diversion ----------

def split_main_and_appendix(pages: list[str]) -> tuple[list[str], list[str]]:
    """Returns (main_lines, appendix_lines) - flat lists of English-only lines,
    routed based on the ***** toggle state."""
    main_lines = []
    appendix_lines = []
    in_appendix = False

    for page_text in pages:
        for line in page_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if ASTERISK_TOGGLE.match(line):
                in_appendix = not in_appendix
                continue  # the marker itself is not content
            if is_arabic_majority(line):
                continue
            (appendix_lines if in_appendix else main_lines).append(line)

    return main_lines, appendix_lines


# ---------- Main hadith parsing (same logic as Step 1, adapted to flat line list) ----------

def parse_nahj_main(main_lines: list[str]) -> list[dict]:
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
                    "topic_title": current_topic_title,
                    "text": full_text,
                })

    for line in main_lines:
        marker = marker_pattern.match(line)
        if marker:
            flush()
            current_number = int(marker.group(1))
            current_topic_title = marker.group(2) or None
            current_lines = []
            continue
        if current_number is not None:
            current_lines.append(line)

    flush()
    return chunks


# ---------- Stage B: parse appendix items and extract footnote numbers ----------

ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}


def roman_to_int(s: str) -> int | None:
    """Converts a Roman numeral string to an int, or None if invalid."""
    if not s or any(ch not in ROMAN_VALUES for ch in s):
        return None
    total = 0
    prev = 0
    for ch in reversed(s):
        val = ROMAN_VALUES[ch]
        total += val if val >= prev else -val
        prev = val
    return total


# Bibliographic/citation number patterns to EXCLUDE from footnote-number candidates
CITATION_CONTEXT = re.compile(
    r'(?:vol\.|p\.|pp\.|no\.|A\.H\.|A\.D\.)\s*\d+', re.IGNORECASE
)


def extract_footnote_number(item_text: str) -> int | None:
    """Finds the first standalone number in the item that is NOT part of a
    bibliographic citation (vol./p./pp./no./A.H./A.D.). This is the shared
    key linking the appendix item back to the main hadith text."""
    # Mask out citation-context numbers first so we don't accidentally grab them
    masked = CITATION_CONTEXT.sub('', item_text)
    match = re.search(r'\b(\d{1,3})\b', masked)
    return int(match.group(1)) if match else None


def parse_appendix_items(appendix_lines: list[str]) -> list[dict]:
    """Splits appendix lines into Roman-numeral-delimited items."""
    item_pattern = re.compile(r'^([IVXLCDM]+)\.\s+(.+)$')
    items = []
    current_roman = None
    current_lines = []

    def flush():
        if current_roman is not None and current_lines:
            text = re.sub(r'\s+', ' ', " ".join(current_lines)).strip()
            roman_int = roman_to_int(current_roman)
            items.append({
                "roman_numeral": current_roman,
                "roman_as_int": roman_int,
                "footnote_number": extract_footnote_number(text),
                "explanation_text": text,
            })

    for line in appendix_lines:
        match = item_pattern.match(line)
        if match:
            flush()
            current_roman = match.group(1)
            current_lines = [match.group(2)]
        else:
            if current_roman is not None:
                current_lines.append(line)

    flush()
    return items


def validate_roman_sequence(items: list[dict]):
    """Sanity check: roman numerals should form 1,2,3,4... with no break.
    A break means our regex probably mis-fired on a false positive."""
    ints = [item["roman_as_int"] for item in items]
    issues = []
    for i, val in enumerate(ints):
        expected = i + 1
        if val != expected:
            issues.append((expected, items[i]["roman_numeral"], val))
    return issues


# ---------- Stage B continued: join by footnote number ----------

BARE_NUMBER_PATTERN = re.compile(r'\b(\d{1,3})\b')


def find_number_occurrences(chunks: list[dict]) -> dict[int, list[int]]:
    """Maps footnote_number -> list of saying_numbers whose text contains
    that number as a standalone token (excluding citation-style contexts)."""
    number_to_sayings = {}
    for chunk in chunks:
        masked = CITATION_CONTEXT.sub('', chunk["text"])
        for match in BARE_NUMBER_PATTERN.finditer(masked):
            num = int(match.group(1))
            number_to_sayings.setdefault(num, []).append(chunk["saying_number"])
    return number_to_sayings


def join_explanations(chunks: list[dict], appendix_items: list[dict]):
    """Mutates chunks in place, inserting bracketed explanations where a
    confident (exactly-one-match) join is found. Returns a review list of
    items that couldn't be confidently matched."""
    number_to_sayings = find_number_occurrences(chunks)
    chunk_by_number = {c["saying_number"]: c for c in chunks}
    review_list = []

    for item in appendix_items:
        fn = item["footnote_number"]
        if fn is None:
            review_list.append({**item, "reason": "no footnote number found in item text"})
            continue

        candidate_sayings = number_to_sayings.get(fn, [])
        if len(candidate_sayings) != 1:
            review_list.append({
                **item,
                "reason": f"number {fn} found in {len(candidate_sayings)} main chunks (need exactly 1)",
                "candidate_sayings": candidate_sayings,
            })
            continue

        target_chunk = chunk_by_number[candidate_sayings[0]]
        # Insert the explanation in brackets right after the matched number
        target_chunk["text"] = re.sub(
            rf'\b{fn}\b',
            f'{fn} [{item["explanation_text"]}]',
            target_chunk["text"],
            count=1,
        )
        target_chunk.setdefault("joined_footnotes", []).append(fn)

    return review_list


if __name__ == "__main__":
    pdf_path = "nahj.pdf"
    pages = extract_pages(pdf_path)
    main_lines, appendix_lines = split_main_and_appendix(pages)

    chunks = parse_nahj_main(main_lines)
    print(f"Main hadith chunks: {len(chunks)} (expect 480)")

    appendix_items = parse_appendix_items(appendix_lines)
    print(f"Appendix items parsed: {len(appendix_items)}")

    sequence_issues = validate_roman_sequence(appendix_items)
    if sequence_issues:
        print(f"\nWARNING: {len(sequence_issues)} Roman numeral sequence breaks (possible false-positive item splits):")
        for expected, actual_roman, actual_int in sequence_issues[:10]:
            print(f"  expected {expected}, got '{actual_roman}' (parsed as {actual_int})")

    review_list = join_explanations(chunks, appendix_items)

    joined_count = sum(len(c.get("joined_footnotes", [])) for c in chunks)
    print(f"\nSuccessfully joined: {joined_count} explanations")
    print(f"Needs manual review: {len(review_list)} items")

    with open("nahj_chunks_step2.json", "w") as f:
        json.dump(chunks, f, indent=2)
    with open("appendix_review_list.json", "w") as f:
        json.dump(review_list, f, indent=2)

    print("\nSaved nahj_chunks_step2.json and appendix_review_list.json")
    print("\n--- Sample of review list (check these manually) ---")
    for r in review_list[:5]:
        print(f"  Roman {r['roman_numeral']}, footnote_number={r['footnote_number']}: {r['reason']}")