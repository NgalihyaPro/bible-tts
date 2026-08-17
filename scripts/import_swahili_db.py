"""Convert the Swahili Bible SQLite database into the API's JSON format.

The source is the Android app's bundled asset DB. Three things need handling:

* Verses are bilingual. Each row holds Swahili followed by an English gloss in
  HTML: "Hapo mwanzo ... <br/><i>In the beginning ...</i>". Only the Swahili may
  reach the synthesiser, or it would narrate the translation and the markup.
* Section headings live in the same table (head 1 and 2) and are not scripture,
  so they are excluded; head=0 rows come to exactly 31,102, the canonical count.
* Verse numbers are not stored. `rank` counts headings too, so numbering is
  derived by position among the head=0 rows of each chapter.

Book keys are the English slugs used by the KJV data, because the Android client
already maps its Swahili book names onto those.

    python scripts/import_swahili_db.py --db "path/to/bible_swahili.db"
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Canonical order. The source orders books by _id, so index alignment is enough.
SLUGS = """genesis exodus leviticus numbers deuteronomy joshua judges ruth
1-samuel 2-samuel 1-kings 2-kings 1-chronicles 2-chronicles ezra nehemiah esther
job psalms proverbs ecclesiastes song-of-solomon isaiah jeremiah lamentations
ezekiel daniel hosea joel amos obadiah jonah micah nahum habakkuk zephaniah
haggai zechariah malachi matthew mark luke john acts romans 1-corinthians
2-corinthians galatians ephesians philippians colossians 1-thessalonians
2-thessalonians 1-timothy 2-timothy titus philemon hebrews james 1-peter 2-peter
1-john 2-john 3-john jude revelation""".split()

TAG_RE = re.compile(r"<[^>]+>")
# The English gloss is introduced by a self-closing <br/> followed by <i>.
# A bare <br> is a line break inside the Swahili -- common in poetry -- so
# splitting on any <br> silently discards verses that open with one.
GLOSS_RE = re.compile(r"<\s*br\s*/\s*>\s*<\s*i\s*>|<\s*i\s*>", re.I)
LINEBREAK_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)
ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&lt;": "<", "&gt;": ">"}


def clean_verse(raw: str | None) -> str:
    """Strip the English gloss, markup and entities; keep only Swahili."""
    if not raw:
        return ""
    match = GLOSS_RE.search(raw)
    swahili = raw[: match.start()] if match else raw
    # Poetic line breaks become spaces so the narration flows as one sentence.
    swahili = LINEBREAK_RE.sub(" ", swahili)
    swahili = TAG_RE.sub("", swahili)
    for entity, char in ENTITIES.items():
        swahili = swahili.replace(entity, char)
    # espeak-ng's Swahili expects an ASCII apostrophe for the ng' velar nasal;
    # the source uses a typographic one, which phonemises differently.
    swahili = swahili.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", swahili).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=str(ROOT / "data/bible/sw/suv.json"))
    ap.add_argument("--translation", default="suv")
    ap.add_argument("--name", default="Swahili Union Version")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    src_books = list(cur.execute("SELECT _id, title, num FROM chapters ORDER BY _id"))
    if len(src_books) != len(SLUGS):
        print(f"expected {len(SLUGS)} books, found {len(src_books)}", file=sys.stderr)
        return 1

    books: dict[str, dict] = {}
    total_ch = total_v = skipped = 0

    for slug, src in zip(SLUGS, src_books):
        chapters: dict[str, list[str]] = {}
        for chapter_no in range(1, src["num"] + 1):
            rows = cur.execute(
                "SELECT text FROM texts WHERE chapter_id=? AND chapter_num=? AND head=0 ORDER BY position",
                (src["_id"], chapter_no),
            )
            verses = []
            for (raw,) in rows:
                text = clean_verse(raw)
                if not text:
                    skipped += 1
                    continue
                verses.append(text)
            if not verses:
                print(f"  warning: {slug} {chapter_no} has no verses", file=sys.stderr)
                continue
            chapters[str(chapter_no)] = verses
            total_v += len(verses)
        total_ch += len(chapters)
        books[slug] = {"name": src["title"], "chapters": chapters}

    out = {
        "translation": args.translation,
        "language": "sw",
        "name": args.name,
        "note": "Imported from the Android app's bundled SQLite database; English glosses and markup stripped.",
        "books": books,
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"books    {len(books)} (expect 66)")
    print(f"chapters {total_ch} (expect 1189)")
    print(f"verses   {total_v} (KJV has 31102)")
    print(f"skipped  {skipped} empty rows")
    print(f"size     {dest.stat().st_size / 1024 / 1024:.2f} MB -> {dest}")
    words = sum(len(v.split()) for b in books.values() for ch in b["chapters"].values() for v in ch)
    print(f"words    {words:,}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
