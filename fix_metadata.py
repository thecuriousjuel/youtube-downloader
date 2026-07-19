"""
fix_metadata.py
---------------
Scans a folder for MP3 files with incomplete metadata, searches YouTube for
each song using its filename, and fills in what is missing.

Fields embedded : Album Art, Artist, Year, Genre
Bitrate         : Read from the audio stream and displayed (not an ID3 tag,
                  so it cannot be written — it is a property of the encoding).

Usage:
    uv run fix_metadata.py
    uv run fix_metadata.py "C:\\path\\to\\your\\music"
"""

import sys
import urllib.request
from pathlib import Path

from mutagen.mp3 import MP3
from mutagen.id3 import (
    APIC,
    TCON,
    TDRC,
    TPE1,
    ID3,
    error as ID3Error,
)
from yt_dlp import YoutubeDL


AUDIO_EXTENSIONS = {".mp3"}


# ---------------------------------------------------------------------------
# File inspection
# ---------------------------------------------------------------------------

def inspect_file(path: Path) -> dict:
    """
    Return a status dict for the given MP3 file.
    Keys: has_art, has_artist, has_year, has_genre, bitrate_kbps
    """
    result = {
        "has_art":     False,
        "has_artist":  False,
        "has_year":    False,
        "has_genre":   False,
        "bitrate_kbps": 0,
    }
    try:
        audio = MP3(path)
        result["bitrate_kbps"] = audio.info.bitrate // 1000

        tags = audio.tags  # ID3 object, or None
        if tags:
            result["has_art"]    = any(k.startswith("APIC") for k in tags)
            result["has_artist"] = bool(tags.get("TPE1") and str(tags["TPE1"]).strip())
            result["has_year"]   = bool(tags.get("TDRC") and str(tags["TDRC"]).strip())
            result["has_genre"]  = bool(tags.get("TCON") and str(tags["TCON"]).strip())
    except Exception:
        pass
    return result


def needs_fixing(status: dict) -> bool:
    return not all([
        status["has_art"],
        status["has_artist"],
        status["has_year"],
        status["has_genre"],
    ])


# ---------------------------------------------------------------------------
# YouTube metadata fetch
# ---------------------------------------------------------------------------

def fetch_metadata(query: str) -> dict | None:
    """
    Search YouTube for *query* and return a metadata dict for the top result.
    Does a full (non-flat) extraction to get uploader, categories, dates, etc.
    """
    ydl_opts = {
        "quiet":       True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    except Exception:
        return None

    if not info:
        return None

    entry = info["entries"][0] if info.get("entries") else info
    if not entry:
        return None

    video_id = entry.get("id", "")

    # Artist: YouTube Music field > creator > channel uploader
    artist = (
        entry.get("artist") or
        entry.get("creator") or
        entry.get("uploader", "")
    ).strip()

    # Year: release_year is more accurate than upload_date
    year = ""
    if entry.get("release_year"):
        year = str(entry["release_year"])
    elif entry.get("upload_date"):
        year = entry["upload_date"][:4]

    # Genre: YouTube categories (e.g. "Music", "Entertainment")
    categories = entry.get("categories") or []
    genre = categories[0] if categories else "Music"

    return {
        "title":    entry.get("title", ""),
        "artist":   artist,
        "year":     year,
        "genre":    genre,
        "video_id": video_id,
    }


def download_thumbnail(video_id: str) -> bytes | None:
    """Download the highest-res available thumbnail for a YouTube video."""
    for quality in ("maxresdefault", "sddefault", "hqdefault", "mqdefault", "default"):
        url = f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if len(data) > 1000:
                        return data
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_metadata(
    path: Path,
    artist: str,
    year: str,
    genre: str,
    image_data: bytes | None,
    status: dict,
) -> bool:
    """Write only the missing metadata fields into the MP3 ID3 tags."""
    try:
        try:
            tags = ID3(path)
        except ID3Error:
            tags = ID3()

        if not status["has_art"] and image_data:
            tags.delall("APIC")
            tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,          # Front cover
                desc="Cover",
                data=image_data,
            ))

        if not status["has_artist"] and artist:
            tags.add(TPE1(encoding=3, text=artist))

        if not status["has_year"] and year:
            tags.add(TDRC(encoding=3, text=year))

        if not status["has_genre"] and genre:
            tags.add(TCON(encoding=3, text=genre))

        tags.save(path)
        return True
    except Exception as e:
        print(f"        x  Write error: {e}")
        return False


# ---------------------------------------------------------------------------
# Main scan logic
# ---------------------------------------------------------------------------

def fix_metadata(folder: Path) -> None:
    print(f"\n  Scanning: {folder}\n")

    all_files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not all_files:
        print("  x  No MP3 files found in this folder.")
        return

    print(f"  Found {len(all_files)} MP3 file(s). Inspecting metadata ...\n")

    to_fix: list[tuple[Path, dict]] = [
        (f, s)
        for f in all_files
        if needs_fixing(s := inspect_file(f))
    ]

    if not to_fix:
        print("  v  All files already have complete metadata. Nothing to do.")
        return

    # Show what is missing per file
    print(f"  {len(to_fix)} file(s) need updates:\n")
    for path, status in to_fix:
        missing_fields = []
        if not status["has_art"]:    missing_fields.append("album art")
        if not status["has_artist"]: missing_fields.append("artist")
        if not status["has_year"]:   missing_fields.append("year")
        if not status["has_genre"]:  missing_fields.append("genre")
        kbps = f"  [{status['bitrate_kbps']} kbps]" if status["bitrate_kbps"] else ""
        print(f"    * {path.name}{kbps}")
        print(f"        Missing: {', '.join(missing_fields)}")

    confirm = input(f"\n  Fix all {len(to_fix)} file(s)? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    fixed: list[str] = []
    failed: list[str] = []

    for i, (path, status) in enumerate(to_fix, 1):
        query = path.stem
        kbps = f" [{status['bitrate_kbps']} kbps]" if status["bitrate_kbps"] else ""
        print(f"\n  [{i}/{len(to_fix)}] {path.name}{kbps}")
        print(f"        Searching YouTube for: \"{query}\" ...")

        meta = fetch_metadata(query)
        if not meta:
            print("        x  Could not find a YouTube result.")
            failed.append(path.name)
            continue

        print(f"        Artist : {meta['artist'] or '(not found)'}")
        print(f"        Year   : {meta['year']   or '(not found)'}")
        print(f"        Genre  : {meta['genre']  or '(not found)'}")

        # Download thumbnail only if album art is missing
        image_data = None
        if not status["has_art"]:
            print("        Downloading thumbnail ...")
            image_data = download_thumbnail(meta["video_id"])
            if not image_data:
                print("        !  Could not download thumbnail.")

        success = embed_metadata(
            path,
            artist=meta["artist"],
            year=meta["year"],
            genre=meta["genre"],
            image_data=image_data,
            status=status,
        )
        if success:
            print("        v  Metadata updated.")
            fixed.append(path.name)
        else:
            failed.append(path.name)

    # Summary
    print("\n" + "=" * 52)
    print("  SUMMARY")
    print("=" * 52)
    print(f"  Total processed : {len(to_fix)}")
    print(f"  Updated         : {len(fixed)}")
    print(f"  Failed          : {len(failed)}")
    if failed:
        print("\n  -- Failed --")
        for name in failed:
            print(f"    * {name}")
    if fixed:
        print(f"\n  v  Done! Metadata updated in {len(fixed)} file(s).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 52)
    print("        Metadata Fixer")
    print("=" * 52)

    if len(sys.argv) > 1:
        raw = " ".join(sys.argv[1:]).strip()
    else:
        raw = input("\nPath to your music folder: ").strip()

    # Strip surrounding quotes (drag-and-drop on Windows often adds them)
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
        raw = raw[1:-1].strip()

    folder = Path(raw)
    if not folder.exists():
        print(f"  x  Folder not found: {folder}")
        sys.exit(1)
    if not folder.is_dir():
        print(f"  x  Not a directory: {folder}")
        sys.exit(1)

    fix_metadata(folder)


if __name__ == "__main__":
    main()
