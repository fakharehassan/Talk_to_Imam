"""
Resumable enrichment pipeline for the Ghurar al-Hikam / Nahj ul-Balagha chunks.

WHAT IT DOES per batch:
1. Flags chunks that look truncated/garbled (NEVER attempts to reconstruct
   missing text — flagging only, for your manual review).
2. Assigns 1-3 life-situation tags from a FIXED taxonomy (validated against
   the list — any tag the model invents outside this list is dropped).
3. If needs_topic_fill=True (use this for Nahj ul-Balagha), also fills the
   'topic' field the same way Ghurar already has one.

RESUMABILITY:
- Progress is tracked by a (source_book, chunk_id) key, where chunk_id is
  each chunk's fixed position in the input file — not batch position, and
  not (source_book, saying_number), since saying_number restarts at 1 for
  every topic and is NOT globally unique (~280 collisions in Ghurar alone).
  A content-based key like (source_book, topic, saying_number) was
  considered but rejected: for Nahj ul-Balagha (--topic-fill), 'topic'
  doesn't exist on the input chunks until enrichment fills it in, so it
  can't be part of the identity key used to look up "is this chunk done
  yet?" before it's even been processed. Position-in-file has no such
  dependency and is stable across runs of the same input file.
- The enriched output file is written after EVERY batch (atomic write via
  temp file + rename), so a crash or killed process never loses more than
  one batch's worth of work.
- If the API starts returning rate-limit/quota errors repeatedly, the
  script stops cleanly with a clear message instead of crashing — just
  re-run the same command tomorrow and it picks up exactly where it left off.
- Multiple Gemini API keys are supported for quota fallback: set
  GEMINI_API_KEY, GEMINI_API_KEY1, GEMINI_API_KEY2, ... in .env (any number,
  any gaps). On a 429/quota/rate-limit error, the script rotates to the next
  key and keeps going — it only counts as a failed attempt (and backs off)
  once EVERY configured key has been tried and all are exhausted.

USAGE:
  python enrich_chunks.py --input ghurar_final.json --output ghurar_enriched.json
  python enrich_chunks.py --input nahj_chunks_step2a.json --output nahj_enriched.json --topic-fill
"""

import argparse
import json
import os
import re
import time
from google import genai
from google.genai import types
from google.genai import errors
from dotenv import load_dotenv

load_dotenv()


def _load_api_keys() -> list[str]:
    keys = []
    base = os.environ.get("GEMINI_API_KEY")
    if base:
        keys.append(base)
    i = 1
    while True:
        k = os.environ.get(f"GEMINI_API_KEY{i}")
        if k:
            keys.append(k)
        elif i > 20:  # stop scanning after a generous run of absent indices
            break
        i += 1
    return keys


API_KEYS = _load_api_keys()
if not API_KEYS:
    raise RuntimeError(
        "No Gemini API key found. Set GEMINI_API_KEY (and optionally "
        "GEMINI_API_KEY1, GEMINI_API_KEY2, ...) in .env."
    )
CLIENTS = [genai.Client(api_key=k) for k in API_KEYS]
_current_key_idx = 0

MODEL_NAME = "gemini-3.1-flash-lite"
BATCH_SIZE = 20
REQUEST_DELAY_SECONDS = 4.5   # keeps us under ~13 RPM, safely below the ~15 RPM free-tier ceiling
MAX_RETRIES_PER_BATCH = 3
QUOTA_BACKOFF_SECONDS = 60

TAXONOMY = [
    "Faith, Worship & Devotion to Allah", "Divine Decree & Trust in Allah",
    "The Afterlife (Paradise, Hellfire, Resurrection)", "Death & Mortality",
    "Repentance & Sin", "Sincerity & Hypocrisy", "Knowledge, Wisdom & Ignorance",
    "Speech, Silence & Eloquence", "Truthfulness, Lying & Deception",
    "Humility & Arrogance", "Patience & Endurance", "Anger & Forgiveness",
    "Envy & Jealousy", "Greed & Ambition", "Wealth & Poverty",
    "Charity & Generosity", "Contentment & Gratitude", "Hope & Despair",
    "Fear & Anxiety", "Grief & Loss", "Self-Discipline & the Soul",
    "Family, Parents & Marriage", "Friendship & Companionship",
    "Enmity, Conflict & Dispute", "Backbiting, Gossip & Secrecy",
    "Justice & Oppression", "Leadership & Governance", "War & Struggle (Jihad)",
    "Time & Opportunity", "Work, Trade & Earnings", "Health, the Body & Sickness",
]


def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def atomic_write(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic on POSIX and Windows — no partial-write corruption risk


def build_prompt(batch: list[dict], needs_topic_fill: bool) -> str:
    taxonomy_list = ", ".join(f'"{t}"' for t in TAXONOMY)

    topic_field = (
        '\n- "topic": choose the single best-fitting short topic label for this saying (your own words, 1-3 words, similar in style to labels like "Patience", "Wealth and riches", "Self-Sacrifice")'
        if needs_topic_fill else ""
    )

    items_text = "\n".join(f"{i+1}. {c['text']}" for i, c in enumerate(batch))

    return f"""You are proofreading and tagging a batch of quotations from classical Islamic wisdom texts (sayings attributed to Imam Ali).

For each numbered item below, output a JSON object with these fields:
- "index": the same number as given (1-based, matching the input)
- "status": "ok" if the text reads as a complete, coherent standalone quotation, or "flagged" if it appears truncated, garbled, or contains obvious extraction errors (stray characters, cut off mid-sentence, nonsensical fragments)
- "issue": a short description of the problem if flagged, else null. CRITICAL: if you flag something, NEVER attempt to guess, reconstruct, or invent what the correct/complete text should be. Only describe the problem — do not generate replacement content.
- "tags": an array of 1 to 3 tags describing the life-situation/theme this saying relates to, chosen ONLY from this fixed list (do not invent new tags, do not modify the wording): [{taxonomy_list}]{topic_field}

Respond with ONLY a valid JSON array of these objects, in the same order as the input, no markdown code fences, no extra commentary before or after.

Items:
{items_text}"""


def parse_response(raw_text: str, expected_count: int) -> list[dict]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    results = json.loads(text)
    if not isinstance(results, list) or len(results) != expected_count:
        raise ValueError(f"Expected {expected_count} results, got {len(results) if isinstance(results, list) else 'non-list'}")
    return results


def call_gemini_with_retry(prompt: str) -> str | None:
    global _current_key_idx
    for attempt in range(MAX_RETRIES_PER_BATCH):
        keys_tried = 0
        while keys_tried < len(CLIENTS):
            try:
                response = CLIENTS[_current_key_idx].models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=4000),
                )
                return response.text
            except errors.APIError as e:
                msg = str(e).lower()
                if "429" in str(e.code) or "quota" in msg or "resource_exhausted" in msg:
                    keys_tried += 1
                    print(f"  Key #{_current_key_idx+1}/{len(CLIENTS)} rate-limited/quota signal detected.")
                    if keys_tried < len(CLIENTS):
                        _current_key_idx = (_current_key_idx + 1) % len(CLIENTS)
                        print(f"  Rotating to key #{_current_key_idx+1}/{len(CLIENTS)}...")
                        continue
                    print(f"  All {len(CLIENTS)} key(s) rate-limited/quota-exhausted "
                          f"(attempt {attempt+1}/{MAX_RETRIES_PER_BATCH}). Backing off {QUOTA_BACKOFF_SECONDS}s...")
                    time.sleep(QUOTA_BACKOFF_SECONDS)
                else:
                    print(f"  API error (attempt {attempt+1}/{MAX_RETRIES_PER_BATCH}): {e.code} {e.message}")
                    time.sleep(5)
                break
            except Exception as e:
                print(f"  Unexpected error (attempt {attempt+1}/{MAX_RETRIES_PER_BATCH}): {e}")
                time.sleep(5)
                break
    return None  # exhausted retries


def process_book(input_path: str, output_path: str, needs_topic_fill: bool):
    progress_path = output_path + ".progress.json"

    chunks = load_json(input_path, [])
    if not chunks:
        print(f"No chunks found in {input_path} — check the path.")
        return

    # chunk_id = fixed position in the input file. This is the unique identity
    # key for resumability — see the RESUMABILITY note in the module docstring
    # for why saying_number and topic can't be used for this instead.
    for idx, c in enumerate(chunks):
        c["chunk_id"] = idx

    enriched_list = load_json(output_path, [])
    enriched_by_key = {(c["source_book"], c["chunk_id"]): c for c in enriched_list}

    progress = load_json(progress_path, {"done_keys": []})
    done_keys = {tuple(k) for k in progress["done_keys"]}

    remaining = [c for c in chunks if (c["source_book"], c["chunk_id"]) not in done_keys]
    print(f"{len(chunks)} total chunks. {len(done_keys)} already done. {len(remaining)} remaining.\n")

    if not remaining:
        print("Nothing left to process — this book is fully enriched already.")
        return

    consecutive_failures = 0

    for batch_start in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1

        prompt = build_prompt(batch, needs_topic_fill)
        raw = call_gemini_with_retry(prompt)

        if raw is None:
            consecutive_failures += 1
            print(f"Batch {batch_num} failed after all retries.")
            if consecutive_failures >= 3:
                print("\n3 consecutive batch failures — likely the daily quota is exhausted for today.")
                print(f"Progress is saved ({len(done_keys)} done). Just re-run this exact command "
                      f"tomorrow (or whenever quota resets) and it will continue from here.")
                return
            continue

        try:
            results = parse_response(raw, len(batch))
        except Exception as e:
            print(f"Batch {batch_num}: failed to parse response ({e}) — skipping, will retry next run.")
            consecutive_failures += 1
            continue

        consecutive_failures = 0  # reset on any success

        for chunk, result in zip(batch, results):
            key = (chunk["source_book"], chunk["chunk_id"])
            enriched_entry = dict(chunk)  # copy — original 'text' field is NEVER modified
            enriched_entry["flag_status"] = result.get("status", "unknown")
            enriched_entry["flag_note"] = result.get("issue")

            raw_tags = result.get("tags", []) or []
            valid_tags = [t for t in raw_tags if t in TAXONOMY]  # drop anything not in our fixed list
            enriched_entry["life_situation_tags"] = valid_tags

            if needs_topic_fill and not enriched_entry.get("topic"):
                enriched_entry["topic"] = result.get("topic")

            enriched_by_key[key] = enriched_entry
            done_keys.add(key)

        # Write progress after EVERY batch — this is what makes resumption safe
        enriched_list = list(enriched_by_key.values())
        atomic_write(output_path, enriched_list)
        progress["done_keys"] = [list(k) for k in done_keys]
        atomic_write(progress_path, progress)

        print(f"Batch {batch_num} done. Total progress: {len(done_keys)}/{len(chunks)}.")
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nFinished — {len(done_keys)}/{len(chunks)} chunks enriched.")
    flagged = [c for c in enriched_list if c.get("flag_status") == "flagged"]
    print(f"{len(flagged)} chunks flagged for manual review — check '{output_path}' for flag_status=='flagged'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--topic-fill", action="store_true", help="Use for Nahj ul-Balagha, which has no topic field yet")
    args = parser.parse_args()

    process_book(args.input, args.output, args.topic_fill)