"""
Step 2A — Isolate the two "*****"-delimited appendix sections from the main
480 hadith chunks.

THE BUG THIS FIXES: the previous version read the appendix content out
separately for analysis, but never actually REMOVED it from the main hadith's
own accumulated text — so hadith #260 and #480 still contained the full
appendix text duplicated inside them.

THE FIX: a single toggle flag added to the same line-by-line loop that
already handles marker detection and Arabic-skipping. When we see a line
that is exactly "*****", we flip into "appendix mode" and start routing
lines into a SEPARATE buffer instead of the current hadith's buffer. When we
see the next "*****", we flip back. If the file ends while still in
appendix mode (your third occurrence, after hadith #480), we just flush
whatever's there — no error, this is expected per your description.

HOW TO VERIFY THIS WORKED:
Check the length of hadith #260 and #480 in the output — they should now
be back to a normal length (comparable to their neighbors #259/#261,
#479), not the abnormal ~8000+ characters from before.
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


def parse_nahj_with_appendix_isolation(pages: list[str]) -> tuple[list[dict], list[str]]:
    marker_pattern = re.compile(r'^Hadith n\.\s*(\d+)\s*(?::\s*(.*))?$')
    appendix_delimiter_pattern = re.compile(r'^\*+$')  # matches a line that is ONLY asterisks

    chunks = []
    current_number = None
    current_topic_title = None
    current_lines = []

    # Separate buffer for appendix content — collected across BOTH appendix
    # sections into one list of "segments" (one string per segment).
    appendix_segments = []
    appendix_lines = []
    in_appendix_section = False

    def clean_text(t: str) -> str:
        t = re.sub(r'^\d+\.\s*', '', t)
        t = re.sub(r'\s+\d+\.?\s*$', '', t)
        return re.sub(r'\s+', ' ', t).strip()

    def flush_hadith():
        if current_number is not None and current_lines:
            full_text = clean_text(" ".join(current_lines))
            if full_text:
                chunks.append({
                    "source_book": "Nahj ul-Balagha",
                    "saying_number": current_number,
                    "topic_title": current_topic_title,
                    "text": full_text,
                })

    def flush_appendix_segment():
        if appendix_lines:
            segment_text = re.sub(r'\s+', ' ', " ".join(appendix_lines)).strip()
            if segment_text:
                appendix_segments.append(segment_text)

    for page_num, page_text in enumerate(pages, start=1):
        for line in page_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Check the appendix toggle FIRST — it can occur mid-hadith, mid-anywhere.
            if appendix_delimiter_pattern.match(line):
                if not in_appendix_section:
                    # Turning ON: whatever was accumulated so far for the CURRENT hadith
                    # stays as-is (it's genuinely part of that hadith, e.g. hadith #260's
                    # real content before the section starts) — we just stop adding to it.
                    in_appendix_section = True
                    appendix_lines = []
                else:
                    # Turning OFF: save this appendix segment, resume normal hadith parsing.
                    flush_appendix_segment()
                    in_appendix_section = False
                    appendix_lines = []
                continue  # the "*****" line itself is not content, never add it anywhere

            marker = marker_pattern.match(line)

            if in_appendix_section:
                # While in appendix mode, "Hadith n. X" markers should NOT appear
                # (they're outside this section) — but if pdftotext/pdfplumber page
                # ordering ever surprises us, log it loudly rather than silently
                # misfiling a real hadith marker as appendix text.
                if marker:
                    print(f"WARNING p.{page_num}: 'Hadith n.' marker seen WHILE inside an "
                          f"appendix section — this shouldn't happen, investigate: {line}")
                if is_arabic_majority(line):
                    continue
                appendix_lines.append(line)
                continue

            # Normal (non-appendix) processing — unchanged from Step 1
            if marker:
                flush_hadith()
                current_number = int(marker.group(1))
                current_topic_title = marker.group(2) or None
                current_lines = []
                continue
            if is_arabic_majority(line):
                continue
            if current_number is not None:
                current_lines.append(line)

    flush_hadith()
    # If the file ended while still in appendix mode (your 3rd "*****" case), flush that too.
    if in_appendix_section:
        flush_appendix_segment()

    return chunks, appendix_segments


if __name__ == "__main__":
    pdf_path = "nahj.pdf"
    print(f"Extracting pages from {pdf_path}...")
    pages = extract_pages(pdf_path)
    print(f"Extracted {len(pages)} pages.\n")

    chunks, appendix_segments = parse_nahj_with_appendix_isolation(pages)

    print(f"Main hadith chunks: {len(chunks)} (expect 480)")
    print(f"Appendix segments captured: {len(appendix_segments)} (expect 2 — one per '*****' pair/EOF-open section)\n")

    # The verification check YOU asked for — length sanity check on the two
    # previously-contaminated hadiths
    for n in [259, 260, 261, 479, 480]:
        match = next((c for c in chunks if c["saying_number"] == n), None)
        if match:
            print(f"#{n}: {len(match['text'])} chars")
        else:
            print(f"#{n}: NOT FOUND (unexpected)")

    with open("nahj_chunks_step2a.json", "w") as f:
        json.dump(chunks, f, indent=2)

    with open("nahj_appendix_segments.json", "w") as f:
        json.dump(appendix_segments, f, indent=2)

    print(f"\nSaved {len(chunks)} clean main chunks to nahj_chunks_step2a.json")
    print(f"Saved {len(appendix_segments)} appendix segments to nahj_appendix_segments.json")
    print("\nNext: inspect nahj_appendix_segments.json — this is the raw appendix text,")
    print("still needing to be split into individual Roman-numeral items before the")
    print("footnote-number join (Step 3) can be attempted correctly.")