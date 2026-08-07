"""
Step 3 — Split appendix segments into individual items, find each item's
real footnote number (not a citation number), and join back to the correct
main hadith chunk.

TWO KEY DESIGN DECISIONS, both explained here so you can judge if they're right:

1. ROMAN NUMERAL SPLITTING uses a SEQUENCE VALIDATION trick: we don't just
   regex-match "I.", "II.", "III." anywhere in the text (too fragile — a
   citation or stray letter could false-match). Instead we require the
   matches to be a STRICTLY INCREASING sequence starting at I (1, 2, 3...).
   Any match that would break the sequence gets rejected as a false
   positive automatically — no manual exclusion list needed.

2. FOOTNOTE NUMBER EXTRACTION doesn't trust the first number it finds.
   It excludes numbers immediately preceded by "vol.", "p.", or "pp."
   (citation context), collects all REMAINING candidate numbers, then
   the JOIN step (searching main chunks) decides which candidate is real:
   only a number that appears in EXACTLY ONE main chunk is accepted.
"""

import re
import json

ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s: str) -> int:
    total = 0
    prev = 0
    for ch in reversed(s):
        val = ROMAN_VALUES.get(ch, 0)
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total


def split_into_roman_items(segment_text: str) -> list[dict]:
    """
    Finds all "ROMAN_NUMERAL." occurrences, keeps only the ones that form a
    strictly increasing sequence starting at I, and splits the text at those
    accepted positions.
    """
    candidate_pattern = re.compile(r'(?<!\w)([IVXLCDM]{1,6})\.\s+(?=[A-Z])')
    candidates = []
    for m in candidate_pattern.finditer(segment_text):
        roman_str = m.group(1)
        value = roman_to_int(roman_str)
        if value > 0:
            candidates.append((m.start(), m.end(), value, roman_str))

    # keep only a strictly increasing sequence starting at 1
    accepted = []
    expected_next = 1
    for start, end, value, roman_str in candidates:
        if value == expected_next:
            accepted.append((start, end, value, roman_str))
            expected_next += 1

    if not accepted:
        return []

    items = []
    for i, (start, end, value, roman_str) in enumerate(accepted):
        text_start = end
        text_end = accepted[i + 1][0] if i + 1 < len(accepted) else len(segment_text)
        item_text = segment_text[text_start:text_end].strip()
        items.append({"roman": roman_str, "value": value, "text": item_text})

    return items


def extract_candidate_footnote_numbers(item_text: str) -> list[int]:
    """
    Finds standalone number tokens, EXCLUDING any number that falls inside a
    parenthetical citation span — e.g. "(in Gharib al-hadith, vol.3, pp.456-458)".
    We detect the whole (...) span first, and if it contains a citation marker
    ('vol.', 'p.', 'pp.'), every number anywhere in that span is excluded —
    not just the one immediately following the abbreviation, since ranges
    like "456-458" would otherwise leak the second number through.
    """
    citation_spans = []
    for paren_match in re.finditer(r'\([^()]*\)', item_text):
        span_text = paren_match.group(0)
        if re.search(r'\b(vol\.|p\.|pp\.)', span_text, re.IGNORECASE):
            citation_spans.append((paren_match.start(), paren_match.end()))

    def in_citation_span(pos: int) -> bool:
        return any(start <= pos < end for start, end in citation_spans)

    candidates = []
    for m in re.finditer(r'(?<!\w)(\d{1,4})(?!\w)', item_text):
        if in_citation_span(m.start()):
            continue
        candidates.append(int(m.group(1)))
    return candidates


def join_item_to_main_chunk(candidate_numbers: list[int], main_chunks: list[dict]) -> dict:
    """
    Tries each candidate number, keeps it only if it appears as a standalone
    token in EXACTLY ONE main chunk's text. Returns a result dict describing
    what happened, for either automatic joining or manual review.
    """
    number_pattern_cache = {}

    def matches_for_number(n: int) -> list[int]:
        if n not in number_pattern_cache:
            pat = re.compile(r'(?<!\w)' + str(n) + r'(?!\w)')
            matches = [c["saying_number"] for c in main_chunks if pat.search(c["text"])]
            number_pattern_cache[n] = matches
        return number_pattern_cache[n]

    confident_matches = []
    for n in candidate_numbers:
        matches = matches_for_number(n)
        if len(matches) == 1:
            confident_matches.append((n, matches[0]))

    if len(confident_matches) == 1:
        n, hadith_num = confident_matches[0]
        return {"status": "joined", "footnote_number": n, "hadith_number": hadith_num}
    elif len(confident_matches) == 0:
        return {"status": "no_confident_match", "candidates_tried": candidate_numbers}
    else:
        return {"status": "ambiguous_multiple_confident_matches", "candidates": confident_matches}


if __name__ == "__main__":
    with open("nahj_chunks_step2a.json") as f:
        main_chunks = json.load(f)

    with open("nahj_appendix_segments.json") as f:
        appendix_segments = json.load(f)

    all_items = []
    for seg_idx, segment in enumerate(appendix_segments):
        items = split_into_roman_items(segment)
        print(f"Segment {seg_idx + 1}: parsed {len(items)} Roman-numeral items")
        all_items.extend(items)

    joined_count = 0
    review_list = []
    final_commentary = {}  # hadith_number -> list of commentary strings

    for item in all_items:
        candidates = extract_candidate_footnote_numbers(item["text"])
        result = join_item_to_main_chunk(candidates, main_chunks)

        if result["status"] == "joined":
            joined_count += 1
            hadith_num = result["hadith_number"]
            final_commentary.setdefault(hadith_num, []).append(item["text"])
        else:
            review_list.append({
                "roman": item["roman"],
                "candidates_found": candidates,
                "status": result["status"],
                "details": result,
                "item_text_preview": item["text"][:150],
            })

    print(f"\nTotal appendix items: {len(all_items)}")
    print(f"Successfully joined: {joined_count}")
    print(f"Needs manual review: {len(review_list)}")

    # Attach joined commentary to main chunks, in brackets, as originally requested
    for chunk in main_chunks:
        n = chunk["saying_number"]
        if n in final_commentary:
            commentary_text = " | ".join(final_commentary[n])
            chunk["text"] = chunk["text"] + f" [Commentary: {commentary_text}]"

    with open("nahj_chunks_final.json", "w") as f:
        json.dump(main_chunks, f, indent=2)

    with open("appendix_review_list.json", "w") as f:
        json.dump(review_list, f, indent=2)

    print("\nSaved nahj_chunks_final.json (with bracketed commentary attached where confidently joined)")
    print("Saved appendix_review_list.json (items needing your manual attention)")
    print("\n--- Review list preview ---")
    for r in review_list:
        print(f"  Roman {r['roman']}: candidates={r['candidates_found']}, status={r['status']}")