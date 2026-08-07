#!/usr/bin/env python3
"""
Hardened, unattended chunking pipeline for the full Nahj ul-Balagha and
Ghurar al-Hikam PDFs.

    python run_full_chunking.py --nahj /path/to/full_nahj.pdf --ghurar /path/to/full_ghurar.pdf

Designed to run start-to-finish with no prompts, overnight, unsupervised:
  - a failure in one book's parse does not stop the other book from being
    saved
  - every page is processed individually so warnings can be attributed to
    a page number
  - all output is written to disk (chunks.json / parsing_log.txt /
    validation_report.txt) - nothing important is only printed to a
    terminal that will not be watched
"""

import argparse
import json
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

import pdfplumber

# --------------------------------------------------------------------------
# Text-cleanup primitives
# --------------------------------------------------------------------------

# Standard Arabic block, presentation forms, and the Private Use Area the
# custom font in these PDFs sometimes maps glyphs into, plus bidi controls.
NON_ENGLISH_RANGES = re.compile(
    r"[؀-ۿﭐ-﷿ﹰ-﻿-"
    r"‎‏‪-‮]"
)

BIDI_CONTROLS = re.compile(r"[‎‏‪-‮]")

# A footnote-reference digit glued directly onto a word with no space
# ("Vehemence1  1. It is...") immediately before a real "N. "/"N) " saying
# marker. Safe to strip because it only matches when unambiguous: a lone
# digit sitting hard against a letter, followed by 2+ spaces and then
# another digit-marker.
FOOTNOTE_GLUE = re.compile(r"(?<=[A-Za-z\)\]])(\d{1,2})(?=\s{2,}\d+[\.\)]\s)")


def is_non_english_line(line: str) -> bool:
    """
    True if a line is the Arabic side of the bilingual layout (or otherwise
    not real English body text) and should be dropped before chunk parsing.

    pdfplumber's extract_text() *does* recover the Arabic glyphs (a mix of
    real Arabic-block codepoints and PUA glyphs the custom font maps some
    characters into), unlike the previous pdftotext-based extraction, which
    silently dropped nearly all of them. extract_pages() strips runs of
    Arabic/PUA/bidi characters out of each page's text up front (replacing
    them with a space) before this function ever sees a line, specifically
    because pdfplumber often puts a topic's Arabic pair on the *same* line
    as its English name (e.g. "Parents أباء") - leaving
    it in place would make that whole line look Arabic-majority and drop
    the English topic name along with it. What's left here is a safety net:
    a line with no Latin letters at all (e.g. what remains of a line that
    was pure Arabic) is treated as non-English, since real English prose
    always contains letters.
    """
    stripped = line.replace(" ", "")
    if not stripped:
        return False
    non_english_chars = len(NON_ENGLISH_RANGES.findall(stripped))
    if non_english_chars / len(stripped) > 0.3:
        return True
    if not re.search(r"[A-Za-z]", stripped):
        return True
    return False


def extract_pages(pdf_path: Path, timeout_s: int = 600) -> list[str]:
    """Open the PDF with pdfplumber and extract each page's text. Pure
    Python - no external `pdftotext` binary dependency - and naturally
    yields per-page text, which is exactly what the rest of the script
    (header/footer detection, per-page warning logs) already expects."""
    log = logging.getLogger("chunking")
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if text is None:
                log.warning(
                    "Page %d of %s: pdfplumber returned no extractable text "
                    "(likely a blank page) - treated as empty",
                    page_number,
                    pdf_path,
                )
                text = ""
            normalized = unicodedata.normalize("NFKC", text)
            cleaned = BIDI_CONTROLS.sub("", normalized)
            # Strip the Arabic/PUA side of the bilingual text outright rather
            # than leaving whole-line detection to do it: pdfplumber often
            # places a topic's Arabic pair on the same line as its English
            # name, and a naive per-line Arabic-majority check would drop
            # the English topic name along with it.
            cleaned = NON_ENGLISH_RANGES.sub(" ", cleaned)
            pages.append(cleaned)
    return pages


def strip_repeated_headers_footers(pages: list[str], threshold: float = 0.8) -> list[str]:
    """
    Detect lines that repeat identically across a large fraction of pages
    (running headers/page numbers) by frequency, not by hardcoded text, and
    strip them. Only looks at the first/last couple of non-empty lines of
    each page, since that is where running headers/footers live.
    """
    from collections import Counter

    n_pages = len([p for p in pages if p.strip()])
    if n_pages == 0:
        return pages

    first_line_counts = Counter()
    last_line_counts = Counter()
    for page in pages:
        lines = [l.strip() for l in page.split("\n") if l.strip()]
        if not lines:
            continue
        for l in lines[:2]:
            first_line_counts[l] += 1
        for l in lines[-2:]:
            last_line_counts[l] += 1

    min_count = threshold * n_pages
    header_footer_lines = {
        l for l, c in first_line_counts.items() if c >= min_count and len(l) > 0
    }
    header_footer_lines |= {
        l for l, c in last_line_counts.items() if c >= min_count and len(l) > 0
    }

    if not header_footer_lines:
        return pages

    cleaned_pages = []
    for page in pages:
        lines = page.split("\n")
        cleaned_pages.append(
            "\n".join(l for l in lines if l.strip() not in header_footer_lines)
        )
    return cleaned_pages


def iter_page_lines(pages: list[str]):
    """Yield (page_number, line) across the whole book as one continuous
    stream. Because this is a single stream (not reset per page), a saying
    or hadith cut off at a page boundary is naturally concatenated with its
    continuation on the next page - while still letting callers attribute
    each line to the page it came from for warning logs."""
    for page_number, page in enumerate(pages, start=1):
        for raw_line in page.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            yield page_number, line


# --------------------------------------------------------------------------
# Nahj ul-Balagha parser: sequential "Hadith n. <N>" markers
# --------------------------------------------------------------------------

HADITH_MARKER = re.compile(r"^Hadith n\.\s*(\d+)$")


def clean_hadith_text(t: str) -> str:
    t = re.sub(r"^\d+\.\s*", "", t)
    t = re.sub(r"\s+\d+\.?\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_nahj_ul_balagha(pages: list[str], log: logging.Logger) -> list[dict]:
    chunks = []
    current_number = None
    current_lines = []
    last_number = None

    def flush():
        if current_number is not None and current_lines:
            full_text = clean_hadith_text(" ".join(current_lines))
            if full_text:
                chunks.append(
                    {
                        "source_book": "Nahj ul-Balagha",
                        "saying_number": current_number,
                        "topic": None,
                        "text": full_text,
                    }
                )

    for page_number, line in iter_page_lines(pages):
        try:
            marker = HADITH_MARKER.match(line)
            if marker:
                flush()
                new_number = int(marker.group(1))
                if last_number is not None and new_number != last_number + 1:
                    log.warning(
                        "Nahj p.%d: hadith numbering gap/anomaly: %s -> %s",
                        page_number,
                        last_number,
                        new_number,
                    )
                current_number = new_number
                last_number = new_number
                current_lines = []
                continue
            if is_non_english_line(line):
                continue
            if current_number is not None:
                current_lines.append(line)
            else:
                log.warning(
                    "Nahj p.%d: line seen before first 'Hadith n.' marker, dropped: %r",
                    page_number,
                    line[:120],
                )
        except Exception:
            log.exception("Nahj p.%d: failed to classify line %r", page_number, line[:120])

    flush()
    return chunks


# --------------------------------------------------------------------------
# Ghurar al-Hikam parser: topic headers + numbered sayings ("N." or "N)")
# --------------------------------------------------------------------------

# A line may be:
#   "N. text" / "N) text"                       -> saying start, no new topic
#   "TopicName N. text" / "TopicName N) text"    -> new topic + its 1st saying
#     (pdftotext frequently glues a short topic header onto the same line as
#     the saying that follows it)
# The topic group is optional and non-greedy so it only consumes characters
# up to the first point where a valid "<digits>[.)] " marker follows.
SAYING_OR_TOPIC_SAYING = re.compile(
    r"^(?P<topic>[A-Za-z][A-Za-z\s\-'\"\(\)\[\]&,\.!?:]*?)?\s*(?P<num>\d+)[\.\)]\s+(?P<rest>.+)$"
)


def _looks_like_topic_name(candidate: str) -> bool:
    """A topic header in this book is always short, Title-Case-ish, and
    doesn't read like a finished sentence - unlike a wrapped continuation
    line (or the last line) of an aphorism's prose, which is what it's
    being distinguished from below. A trailing footnote-reference digit
    ("...is deprived.1") is ignored for the sentence-ending check so it
    can't disguise a finished sentence as a topic name."""
    candidate = candidate.strip()
    if not candidate:
        return False
    sentence_check = re.sub(r"\d{1,2}$", "", candidate)
    return (
        len(candidate) <= 80
        and candidate[0].isupper()
        and not sentence_check.endswith((".", "!", "?"))
    )


def parse_ghurar_al_hikam(pages: list[str], log: logging.Logger) -> list[dict]:
    chunks = []
    current_topic = None
    current_number = None
    current_lines = []
    last_number = None
    # Lines seen before the very first saying has been opened at all (i.e.
    # before current_number is ever set) have nowhere else to accumulate -
    # current_lines only makes sense once a saying is open. This only
    # matters once, for the book's opening topic header.
    pre_first_lines = []

    def flush():
        if current_number is not None and current_lines:
            full_text = re.sub(r"\s+", " ", " ".join(current_lines)).strip()
            if full_text:
                chunks.append(
                    {
                        "source_book": "Ghurar al-Hikam",
                        "saying_number": current_number,
                        "topic": current_topic,
                        "text": full_text,
                    }
                )

    def pop_trailing_topic_candidate():
        """If the saying currently being accumulated ends with a line that
        reads like a topic header rather than more of its own prose (this
        book often puts the next topic's standalone header line right
        before its first saying, and a saying's own English text can wrap
        across several raw lines with pdfplumber), pull just that trailing
        line back out and hand it back as a topic-name candidate."""
        nonlocal current_lines
        if not current_lines:
            return None
        if _looks_like_topic_name(current_lines[-1]):
            candidate = current_lines[-1]
            current_lines = current_lines[:-1]
            return candidate.strip(" .:;,")
        return None

    for page_number, line in iter_page_lines(pages):
        try:
            line = FOOTNOTE_GLUE.sub("", line)
            if is_non_english_line(line):
                continue

            m = SAYING_OR_TOPIC_SAYING.match(line)
            if not m:
                # Not a recognizable saying start. With pdfplumber's real
                # line wrapping this is usually the continuation of the
                # saying currently being accumulated (its English text
                # wrapped onto another physical line) - attach it right
                # away rather than deferring the decision, so a saying that
                # wraps across several lines stays intact even if several
                # more numbered lines and topic transitions follow before
                # the next saying-start is seen. A standalone topic header
                # line ends up here too; it gets peeled back off by
                # pop_trailing_topic_candidate() when the next saying-start
                # line reveals it was a topic boundary, not a continuation.
                if current_number is not None:
                    current_lines.append(line)
                else:
                    pre_first_lines.append(line)
                continue

            num = int(m.group("num"))
            topic_prefix = (m.group("topic") or "").strip(" .:;,")
            rest = m.group("rest").strip()
            first_saying_of_book = last_number is None

            if topic_prefix:
                # Explicit topic name on this line is the strongest signal
                # there is - honor it regardless of what the numbering was
                # doing. If the line immediately before it also looked like
                # a topic-header fragment (a topic name wrapped across two
                # lines), fold it into the name; otherwise it was genuine
                # continuation prose of the previous saying and stays put.
                wrapped_prefix = pop_trailing_topic_candidate()
                flush()
                current_topic = (
                    f"{wrapped_prefix} {topic_prefix}".strip() if wrapped_prefix else topic_prefix
                )
                current_number = num
                current_lines = [rest]
                last_number = num
            elif first_saying_of_book:
                current_topic = " ".join(pre_first_lines).strip(" .:;,") or None
                current_number = num
                current_lines = [rest]
                last_number = num
            elif num == last_number + 1:
                flush()
                current_number = num
                current_lines = [rest]
                last_number = num
            elif num > last_number + 1:
                log.warning(
                    "Ghurar p.%d: missing/skipped saying number(s) in topic '%s': %s -> %s",
                    page_number,
                    current_topic,
                    last_number,
                    num,
                )
                flush()
                current_number = num
                current_lines = [rest]
                last_number = num
            else:
                # num <= last_number with no explicit topic name: numbering
                # went backward or repeated. Per spec this is treated as a
                # probable footnote UNLESS the line right before it reads
                # like a genuine (unglued) topic header - e.g. a topic name
                # that appeared alone on its own line before its first
                # saying - in which case it really is a new topic.
                topic_candidate = pop_trailing_topic_candidate()
                if topic_candidate:
                    flush()
                    current_topic = topic_candidate
                    current_number = num
                    current_lines = [rest]
                    last_number = num
                else:
                    log.warning(
                        "Ghurar p.%d: non-sequential saying number %s after %s in "
                        "topic '%s' - treated as probable footnote and discarded: %r",
                        page_number,
                        num,
                        last_number,
                        current_topic,
                        rest[:120],
                    )
        except Exception:
            log.exception("Ghurar p.%d: failed to classify line %r", page_number, line[:120])

    flush()
    return chunks


# --------------------------------------------------------------------------
# Validation report
# --------------------------------------------------------------------------

def build_validation_report(all_chunks: dict[str, list[dict]]) -> str:
    lines = []
    lines.append("VALIDATION REPORT - morning review")
    lines.append("=" * 60)
    lines.append("")

    for book, chunks in all_chunks.items():
        lines.append(f"## {book}")
        lines.append(f"Total chunks: {len(chunks)}")

        short = [c for c in chunks if len(c["text"]) < 15]
        long = [c for c in chunks if len(c["text"]) > 500]
        lines.append(f"Chunks under 15 chars (likely parsing errors): {len(short)}")
        for c in short[:50]:
            lines.append(f"  - saying {c['saying_number']} (topic={c['topic']!r}): {c['text']!r}")
        lines.append(f"Chunks over 500 chars (verify not merged/mis-split): {len(long)}")
        for c in long[:50]:
            lines.append(
                f"  - saying {c['saying_number']} (topic={c['topic']!r}, len={len(c['text'])}): "
                f"{c['text'][:120]!r}..."
            )

        if book == "Ghurar al-Hikam":
            topics = {}
            for c in chunks:
                topics.setdefault(c["topic"], []).append(c["saying_number"])
            single = [t for t, v in topics.items() if len(v) == 1]
            lines.append(f"Distinct topics: {len(topics)}")
            lines.append(f"Topics with only 1 saying (possible mis-detected split): {len(single)}")
            for t in single:
                lines.append(f"  - {t!r}")

        lines.append("")
        lines.append(f"First 3 chunks of {book}:")
        for c in chunks[:3]:
            lines.append(f"  [{c['saying_number']}] (topic={c['topic']!r}) {c['text']}")
        lines.append(f"Last 3 chunks of {book}:")
        for c in chunks[-3:]:
            lines.append(f"  [{c['saying_number']}] (topic={c['topic']!r}) {c['text']}")
        lines.append("")
        lines.append("-" * 60)
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_one_book(pdf_path: Path, book_name: str, parser_fn, log: logging.Logger):
    """Runs the full extract+strip+parse pipeline for one book. Returns
    (chunks, page_count) on success, or (None, 0) on failure - callers must
    keep going so the other book's output still gets saved."""
    log.info("=== %s: starting (%s) ===", book_name, pdf_path)
    t0 = time.time()
    try:
        pages = extract_pages(pdf_path)
    except Exception:
        log.exception("%s: pdfplumber extraction failed for %s", book_name, pdf_path)
        return None, 0

    page_count = len(pages)
    log.info("%s: extracted %d pages in %.1fs", book_name, page_count, time.time() - t0)

    pages = strip_repeated_headers_footers(pages)

    try:
        chunks = parser_fn(pages, log)
    except Exception:
        log.exception("%s: chunk parser crashed", book_name)
        return None, page_count

    log.info(
        "%s: finished - %d pages processed, %d chunks extracted (%.1fs total)",
        book_name,
        page_count,
        len(chunks),
        time.time() - t0,
    )
    return chunks, page_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nahj", required=True, type=Path, help="Path to full Nahj ul-Balagha PDF")
    parser.add_argument("--ghurar", required=True, type=Path, help="Path to full Ghurar al-Hikam PDF")
    parser.add_argument("--outdir", type=Path, default=Path("."), help="Directory to write outputs to")
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    log_path = args.outdir / "parsing_log.txt"
    chunks_path = args.outdir / "chunks.json"
    report_path = args.outdir / "validation_report.txt"

    # Console output on Windows otherwise defaults to the cp1252 codepage,
    # which cannot encode Arabic/PUA characters that can end up quoted in a
    # warning message - that raised UnicodeEncodeError inside the logging
    # module (non-fatal, but noisy) before this was added.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    log = logging.getLogger("chunking")
    log.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    log.addHandler(stream_handler)

    run_start = time.time()
    log.info("Run started")

    all_chunks: dict[str, list[dict]] = {}

    for pdf_path, book_name, parser_fn in (
        (args.nahj, "Nahj ul-Balagha", parse_nahj_ul_balagha),
        (args.ghurar, "Ghurar al-Hikam", parse_ghurar_al_hikam),
    ):
        try:
            chunks, _ = parse_one_book(pdf_path, book_name, parser_fn, log)
        except Exception:
            # Belt-and-suspenders: parse_one_book already catches its own
            # errors, but nothing about this run may ever crash and lose
            # the other book's output.
            log.exception("%s: unhandled top-level failure", book_name)
            chunks = None
        if chunks is not None:
            all_chunks[book_name] = chunks
        else:
            log.error("%s: no chunks produced due to failure - see errors above", book_name)
            all_chunks[book_name] = []

    combined = []
    for chunks in all_chunks.values():
        combined.extend(chunks)

    try:
        chunks_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Wrote %d total chunks to %s", len(combined), chunks_path)
    except Exception:
        log.exception("Failed to write %s", chunks_path)

    try:
        report = build_validation_report(all_chunks)
        report_path.write_text(report, encoding="utf-8")
        log.info("Wrote validation report to %s", report_path)
    except Exception:
        log.exception("Failed to write %s", report_path)

    log.info("Run finished in %.1fs", time.time() - run_start)


if __name__ == "__main__":
    main()
