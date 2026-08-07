"""
Diagnostic — does footnote marker 64 (or 65) exist ANYWHERE in the clean
480 chunks now, with no restriction, no exclusion, just a raw search?

If genuinely NOT FOUND anywhere, that tells us the join strategy's core
assumption (footnote number = a digit that literally reappears inline in
the target hadith text) is wrong for at least some markers, even though
it held for hadith #1's "1". That would mean SOME markers survive
extraction and some don't — inconsistent, not a clean yes/no we can
regex our way around without seeing why.
"""

import json
import re

with open("nahj_chunks_final.json") as f:
    chunks = json.load(f)

for target in [64, 65]:
    print(f"\n=== Raw search for standalone '{target}' across all 480 chunks ===")
    pattern = re.compile(r'(?<!\w)' + str(target) + r'(?!\w)')
    found_in = [c["saying_number"] for c in chunks if pattern.search(c["text"])]
    print(f"Found in hadith numbers: {found_in}")
    if not found_in:
        print(f"  -> Genuinely absent everywhere. The '{target}' marker did not survive extraction anywhere,")
        print(f"     OR it was never inline in the main text to begin with (different footnote system entirely).")

# Also: let's directly check whether hadith #1's "1" marker is STILL there
# post-cleanup (sanity check that Step 2A didn't accidentally strip it)
hadith_1 = next(c for c in chunks if c["saying_number"] == 1)
print(f"\n=== Sanity re-check: hadith #1 text ===")
print(hadith_1["text"])
print("(Confirming the '1' marker is still intact after all our processing so far.)")