import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL

OUTPUT_DIR = Path(r"C:\Users\juelb\Videos\yt-downloaded")
ARCHIVE_FILE = OUTPUT_DIR / ".yt-archive.txt"

# Matches youtube.com (www / m / music) and youtu.be URLs
_YT_RE = re.compile(
    r"https?://(?:(?:www\.|m\.|music\.)?youtube\.com/\S+|youtu\.be/\S+)",
    re.IGNORECASE,
)


def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def prompt_choice(prompt: str, options: list[str]) -> int:
    """Display a numbered menu and return the 1-based index of the user's choice."""
    while True:
        print(f"\n{prompt}")
        for i, option in enumerate(options, 1):
            print(f"  [{i}] {option}")
        raw = input("Enter choice: ").strip()
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(options):
                return choice
        print(f"  ✗  Please enter a number between 1 and {len(options)}.")


def extract_video_id(url: str) -> str | None:
    """
    Return a canonical ID string for a YouTube URL so that two URLs
    pointing to the same content compare equal, regardless of tracking
    parameters like &si= or &pp=.

    Returns:
        "video:<ID>"    for watch URLs
        "playlist:<ID>" for playlist-only URLs
        None            if no recognised ID is found
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # youtu.be/<ID>
    if "youtu.be" in parsed.netloc:
        vid = parsed.path.lstrip("/").split("/")[0]
        return f"video:{vid}" if vid else None

    # playlist with no individual video → playlist ID
    if "list" in qs and "v" not in qs:
        return f"playlist:{qs['list'][0]}"

    # watch URL (may also have a list= but the primary key is v=)
    if "v" in qs:
        return f"video:{qs['v'][0]}"

    return None


def is_playlist(url: str) -> bool:
    """Return True if the URL points to a YouTube playlist."""
    quiet_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(quiet_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info.get("_type") == "playlist"


def download(url: str, mode: str, playlist: bool) -> None:
    """
    Download video or audio.

    mode    : "video" | "audio"
    playlist: True  → download entire playlist
              False → single video only
    """
    outtmpl = str(OUTPUT_DIR / "%(title)s.%(ext)s")
    archive = str(ARCHIVE_FILE)

    # Common options applied to every download
    common = {
        "outtmpl": outtmpl,
        "noplaylist": not playlist,
        "download_archive": archive,
        # Don't abort the whole playlist when one video is unavailable,
        # private, region-locked, or returns any other error.
        "ignoreerrors": True,
        # Retry each fragment/request up to 10 times before giving up.
        "retries": 10,
        "fragment_retries": 10,
        # Small sleep between requests to avoid triggering rate-limits
        # on large playlists (YouTube typically serves 100 items per page).
        "sleep_interval_requests": 1,
    }

    if mode == "video":
        options = {
            **common,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
        }
    else:  # audio
        options = {
            **common,
            "format": "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                },
                {
                    # Embeds title, artist (uploader), year, genre from yt-dlp
                    # info dict into the MP3's ID3 tags via FFmpeg.
                    "key": "FFmpegMetadata",
                    "add_metadata": True,
                },
                {
                    "key": "EmbedThumbnail",
                },
            ],
        }

    with YoutubeDL(options) as ydl:
        ydl.download([url])


# ---------------------------------------------------------------------------
# .txt batch-file helpers
# ---------------------------------------------------------------------------

def read_urls_from_file(
    filepath: Path,
) -> tuple[list[str], list[tuple[int, str, str]]]:
    """
    Parse a .txt file and return:
      unique_urls  – deduplicated list of YouTube URLs (first occurrence wins)
      duplicates   – list of (line_number, url, canonical_id) for skipped dupes
    """
    seen_ids: dict[str, str] = {}   # canonical_id → first URL seen
    unique_urls: list[str] = []
    duplicates: list[tuple[int, str, str]] = []

    with filepath.open(encoding="utf-8", errors="replace") as fh:
        for line_no, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            # Accept the whole line or extract first embedded URL
            if _YT_RE.match(line):
                url = line
            else:
                m = _YT_RE.search(line)
                url = m.group(0) if m else None

            if url is None:
                continue

            canonical = extract_video_id(url)

            if canonical and canonical in seen_ids:
                duplicates.append((line_no, url, canonical))
                print(
                    f"  ⚠  Line {line_no}: duplicate of line already queued "
                    f"({canonical}) — skipping."
                )
            else:
                unique_urls.append(url)
                if canonical:
                    seen_ids[canonical] = url

    return unique_urls, duplicates


def handle_batch_file(filepath: Path) -> None:
    """Interactively download all unique URLs found inside a .txt file."""
    print(f"\n  ✓  Text file detected: {filepath.name}")
    print("  Scanning for URLs …\n")

    urls, duplicates = read_urls_from_file(filepath)

    if not urls:
        print("  ✗  No valid YouTube URLs found in the file.")
        return

    print(f"\n  ✓  Found {len(urls)} unique URL(s) to download"
          + (f" ({len(duplicates)} duplicate(s) skipped)." if duplicates else "."))
    for i, u in enumerate(urls, 1):
        print(f"       {i}. {u}")

    choice = prompt_choice(
        "What would you like to download for ALL URLs?",
        [
            "Highest quality video  (MP4)",
            "Highest quality audio  (MP3)",
            "Cancel",
        ],
    )
    if choice == 3:
        print("Cancelled.")
        return

    mode = "video" if choice == 1 else "audio"
    label = "videos" if mode == "video" else "audio files"

    confirm = input(
        f"\n  Download {label} for all {len(urls)} URL(s) to\n"
        f"  {OUTPUT_DIR}\n"
        f"  Confirm? [y/N]: "
    ).strip().lower()

    if confirm != "y":
        print("Cancelled.")
        return

    print(f"\nDownloading … (saving to {OUTPUT_DIR})\n")
    failed: list[tuple[int, str, str]] = []
    already_downloaded: list[tuple[int, str]] = []

    for idx, url in enumerate(urls, 1):
        print(f"\n── [{idx}/{len(urls)}] {url}")
        try:
            archive_before = _read_archive()
            is_pl = is_playlist(url)
            download(url, mode, playlist=is_pl)
            archive_after = _read_archive()

            # If the archive didn't grow, yt-dlp skipped it (already downloaded)
            new_entries = archive_after - archive_before
            if not new_entries:
                print("  ⚠  Already downloaded — skipped.")
                already_downloaded.append((idx, url))
            else:
                print("  ✓  Done.")
        except Exception as e:
            print(f"  ✗  Failed: {e}")
            failed.append((idx, url, str(e)))

    _print_summary(len(urls), duplicates, already_downloaded, failed)


def _read_archive() -> set[str]:
    """Return the current set of entries in the yt-dlp download archive."""
    if not ARCHIVE_FILE.exists():
        return set()
    return set(ARCHIVE_FILE.read_text(encoding="utf-8").splitlines())


def _print_summary(
    total: int,
    duplicates: list[tuple[int, str, str]],
    already_downloaded: list[tuple[int, str]],
    failed: list[tuple[int, str, str]],
) -> None:
    print("\n" + "=" * 50)
    print("  SUMMARY")
    print("=" * 50)
    skipped = len(duplicates) + len(already_downloaded)
    succeeded = total - len(failed) - len(already_downloaded)
    print(f"  Total queued   : {total}")
    print(f"  Downloaded     : {succeeded}")
    print(f"  Skipped        : {skipped}")
    print(f"  Failed         : {len(failed)}")

    if duplicates:
        print(f"\n  ── Duplicates skipped ({len(duplicates)}) ──")
        for line_no, url, cid in duplicates:
            print(f"    Line {line_no}: {url}")
            print(f"             (same as {cid})")

    if already_downloaded:
        print(f"\n  ── Already downloaded ({len(already_downloaded)}) ──")
        for idx, url in already_downloaded:
            print(f"    [{idx}] {url}")

    if failed:
        print(f"\n  ── Errors ({len(failed)}) ──")
        for idx, url, err in failed:
            print(f"    [{idx}] {url}\n         → {err}")

    if not duplicates and not already_downloaded and not failed:
        print(f"\n  ✓  All {total} item(s) downloaded successfully!")

    print(f"\n  Files saved to: {OUTPUT_DIR}")


# ---------------------------------------------------------------------------
# Single-URL helpers
# ---------------------------------------------------------------------------

def handle_single_url(url: str) -> None:
    """Interactively download a single video or playlist URL."""
    print("\nChecking URL …")
    try:
        playlist = is_playlist(url)
    except Exception as e:
        print(f"  ✗  Could not fetch URL info: {e}")
        return

    if playlist:
        print("\n  ✓  Playlist detected.")
        choice = prompt_choice(
            "What would you like to download?",
            [
                "All videos  (highest quality MP4)",
                "All audio   (highest quality MP3)",
                "Cancel",
            ],
        )
        if choice == 3:
            print("Cancelled.")
            return

        mode = "video" if choice == 1 else "audio"
        label = "videos" if mode == "video" else "audio files"
        confirm = input(
            f"\n  Download all {label} from this playlist to\n"
            f"  {OUTPUT_DIR}\n"
            f"  Confirm? [y/N]: "
        ).strip().lower()

        if confirm != "y":
            print("Cancelled.")
            return

    else:
        print("\n  ✓  Single video detected.")
        choice = prompt_choice(
            "What would you like to download?",
            [
                "Highest quality video  (MP4)",
                "Highest quality audio  (MP3)",
                "Cancel",
            ],
        )
        if choice == 3:
            print("Cancelled.")
            return

        mode = "video" if choice == 1 else "audio"

    print(f"\nDownloading … (saving to {OUTPUT_DIR})\n")
    archive_before = _read_archive()
    try:
        download(url, mode, playlist)
        archive_after = _read_archive()
        if archive_after - archive_before:
            print(f"\n  ✓  Done! Files saved to: {OUTPUT_DIR}")
        else:
            print(
                f"\n  ⚠  Already downloaded — skipped.\n"
                f"  (Recorded in archive: {ARCHIVE_FILE})"
            )
    except Exception as e:
        print(f"\n  ✗  Download failed: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ensure_output_dir()

    print("=" * 50)
    print("       YouTube Downloader")
    print("=" * 50)
    print("\nYou can enter:")
    print("  • A YouTube video URL")
    print("  • A YouTube playlist URL")
    print("  • A path to a .txt file with one URL per line")

    raw = input("\nURL or file path: ").strip()
    if not raw:
        print("Nothing entered. Exiting.")
        return

    # Strip surrounding quotes (drag-and-drop on Windows often adds them)
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
        raw = raw[1:-1].strip()

    # --- check if it's a file path ---
    candidate = Path(raw)
    if candidate.suffix.lower() == ".txt":
        if candidate.exists():
            handle_batch_file(candidate)
        else:
            print(f"  ✗  File not found: {candidate}")
        return

    # --- treat as a URL ---
    if not _YT_RE.match(raw):
        print("  ✗  Input doesn't look like a YouTube URL or a .txt file path.")
        return

    handle_single_url(raw)


if __name__ == "__main__":
    main()