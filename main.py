import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Ensure the terminal uses UTF-8 on Windows so Rich's Unicode characters
# (spinners, emoji, box-drawing) are encoded correctly.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # Python < 3.7 fallback

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.status import Status
from rich.table import Column, Table
from rich.text import Text
from rich.theme import Theme
from yt_dlp import YoutubeDL

# ---------------------------------------------------------------------------
# Rich console / theme
# ---------------------------------------------------------------------------

THEME = Theme(
    {
        "info":    "bold cyan",
        "success": "bold green",
        "warn":    "bold yellow",
        "error":   "bold red",
        "muted":   "dim white",
        "accent":  "bold magenta",
        "url":     "underline bright_blue",
        "heading": "bold white",
    }
)

console = Console(
    theme=THEME,
    highlight=False,
    legacy_windows=False,   # use ANSI codes instead of the Win32 legacy API
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

OUTPUT_DIR   = Path(r"C:\Users\juelb\Videos\yt-downloaded")
ARCHIVE_FILE = OUTPUT_DIR / ".yt-archive.txt"

_YT_RE = re.compile(
    r"https?://(?:(?:www\.|m\.|music\.)?youtube\.com/\S+|youtu\.be/\S+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _sanitise_folder_name(name: str) -> str:
    """Strip characters that are illegal in Windows/Linux folder names."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "playlist"


def _read_archive() -> set[str]:
    """Return the current set of entries in the yt-dlp download archive."""
    if not ARCHIVE_FILE.exists():
        return set()
    return set(ARCHIVE_FILE.read_text(encoding="utf-8").splitlines())


def extract_video_id(url: str) -> str | None:
    """
    Return a canonical ID string for a YouTube URL so that two URLs
    pointing to the same content compare equal, regardless of tracking
    parameters like &si= or &pp=.
    """
    parsed = urlparse(url)
    qs     = parse_qs(parsed.query)

    if "youtu.be" in parsed.netloc:
        vid = parsed.path.lstrip("/").split("/")[0]
        return f"video:{vid}" if vid else None

    if "list" in qs and "v" not in qs:
        return f"playlist:{qs['list'][0]}"

    if "v" in qs:
        return f"video:{qs['v'][0]}"

    return None


# ---------------------------------------------------------------------------
# YouTube metadata helpers
# ---------------------------------------------------------------------------

def get_playlist_info(url: str) -> tuple[bool, str | None, int | None, dict[str, dict]]:
    """
    Inspect a URL and return (is_playlist, title, n_entries, id_map).

    If the URL contains a playlist query parameter (list=...), we query
    yt-dlp for the playlist info so that entries & id_map are fully loaded.
    """
    parsed = urlparse(url)
    qs     = parse_qs(parsed.query)

    target_url = url
    if "list" in qs:
        target_url = f"https://www.youtube.com/playlist?list={qs['list'][0]}"

    quiet_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(quiet_opts) as ydl:
        info = ydl.extract_info(target_url, download=False)
    if info.get("_type") == "playlist":
        entries   = list(info.get("entries") or [])
        n_entries = len(entries) if entries else None
        id_map    = {}
        for i, e in enumerate(entries, 1):
            if e and isinstance(e, dict) and e.get("id"):
                id_map[e["id"]] = {
                    "title": e.get("title") or e.get("url") or e["id"],
                    "index": i,
                    "uploader": e.get("uploader") or e.get("channel") or "",
                }
        return True, info.get("title"), n_entries, id_map
    return False, None, None, {}


def is_playlist(url: str) -> bool:
    """Return True if the URL points to a YouTube playlist."""
    pl, _, _, _ = get_playlist_info(url)
    return pl


# ---------------------------------------------------------------------------
# Rich progress bar factories
# ---------------------------------------------------------------------------

_PP_LABELS: dict[str, str] = {
    "FFmpegExtractAudio": "✂  Extracting audio",
    "FFmpegMetadata":     "🏷  Writing metadata",
    "EmbedThumbnail":     "🖼  Embedding thumbnail",
    "FFmpegMerger":       "⚙  Merging streams",
    "FFmpegVideoRemuxer": "⚙  Remuxing video",
    "MoveFiles":          "📁  Moving file",
}

# PP names that mean the track is fully post-processed
_FINAL_PP = {"EmbedThumbnail", "MoveFiles", "FFmpegMerger", "FFmpegVideoRemuxer"}


def _make_overall_progress() -> Progress:
    """Global N-of-M progress bar shown at the very top."""
    return Progress(
        SpinnerColumn(style="white"),
        TextColumn(
            "[bold white]{task.description}[/]",
            table_column=Column(no_wrap=True),
        ),
        BarColumn(
            bar_width=None,
            style="bright_black",
            complete_style="bright_green",
            finished_style="bright_green",
        ),
        MofNCompleteColumn(),
        TextColumn("[muted]tracks[/]"),
        console=console,
        expand=True,
    )


def _make_track_progress() -> Progress:
    """Per-stage download + post-processing bars for the current track."""
    return Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("{task.description}"),
        BarColumn(
            bar_width=None,
            style="magenta",
            complete_style="green",
            finished_style="bright_green",
        ),
        DownloadColumn(),
        TransferSpeedColumn(),
        TextColumn("[muted]ETA[/]"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
        expand=True,
    )


# ---------------------------------------------------------------------------
# Unified live display
# ---------------------------------------------------------------------------

class _TrackDisplay:
    """
    A Rich renderable that combines:
      1. Overall playlist progress panel (top)
      2. Current-track panel containing the song header AND the live
         download/post-processing bars — all in one bordered box.

    Implements __rich_console__ so Rich's Live renderer calls it on every
    refresh cycle.
    """

    def __init__(self, overall: Progress, track: Progress) -> None:
        self._overall  = overall
        self._track    = track
        self._title:    str  = ""
        self._uploader: str  = ""
        self._badge:    str  = ""
        self._border:   str  = "cyan"
        self._icon:     str  = "🎵"
        self._visible:  bool = False

    def show(
        self,
        title:    str,
        uploader: str,
        badge:    str,
        border:   str = "cyan",
        icon:     str = "🎵",
    ) -> None:
        self._title    = title
        self._uploader = uploader
        self._badge    = badge
        self._border   = border
        self._icon     = icon
        self._visible  = True

    def hide(self) -> None:
        self._visible = False

    def __rich_console__(self, console, options):   # type: ignore[override]
        # ── Current-track panel ─────────────────────────────────────────
        if self._visible:
            # Header: icon + title + uploader
            header = Table.grid(padding=(0, 1))
            header.add_column(justify="center", no_wrap=True)
            header.add_column()

            title_text = Text()
            if self._icon == "⏭":
                title_text.append("⏭  ", style="bold yellow")
            title_text.append(self._title, style="bold white")
            if self._uploader:
                title_text.append(f"\n   {self._uploader}", style="dim white")

            header.add_row(self._icon, title_text)

            # Combine header + divider + progress bars into the panel content
            content = Group(header, Rule(style="dim"), self._track)

            yield Panel(
                content,
                title=self._badge,
                title_align="left",
                border_style=self._border,
                padding=(0, 1),
            )

        # ── Overall panel pinned to the bottom (only for playlists) ─────
        if self._overall.task_ids:
            yield Panel(
                self._overall,
                title=" [bold white]Playlist Progress[/] ",
                border_style="white dim",
                padding=(0, 0),
            )


# ---------------------------------------------------------------------------
# yt-dlp hook bridge
# ---------------------------------------------------------------------------

class _RichHooks:
    """
    Bridges yt-dlp's progress_hook, postprocessor_hook, and logger APIs
    into the unified _TrackDisplay live view.

    Lifecycle per track:
      downloading  → _start_track() → display.show() → bars fill
      finished     → bar label turns green
      pp started   → PP spinner appears (inside same panel)
      pp finished  → _finish_track() → compact history line printed,
                     display.hide(), overall bar advances
    Skipped tracks (archive / file-exists) emit a compact history line only.
    """

    def __init__(
        self,
        overall:   Progress,
        track:     Progress,
        display:   _TrackDisplay,
        n_entries: int | None,
        id_map:    dict[str, dict] | None = None,
    ) -> None:
        self._overall  = overall
        self._track    = track
        self._display  = display
        self._id_map   = id_map or {}   # video_id → {title, index}

        # Overall task
        self._overall_task: TaskID | None = None
        if n_entries and n_entries > 1:
            self._overall_task = self._overall.add_task(
                "📋  Overall Progress",
                total=n_entries,
                completed=0,
            )
        self._n_entries = n_entries

        # Per-track state
        self._dl_tasks:        dict[str, TaskID] = {}
        self._pp_task:         TaskID | None     = None
        self._seen:            set[str]          = set()
        self._printed_indices: set[int]          = set()
        self._completed:       int               = 0
        self._current_track:   dict              = {}   # title, uploader, pl_index, n_entries

    # ------------------------------------------------------------------
    # progress_hook
    # ------------------------------------------------------------------
    def progress_hook(self, d: dict) -> None:
        status   = d.get("status")
        filename = d.get("filename") or d.get("tmpfilename") or "?"
        info     = d.get("info_dict") or {}

        title     = info.get("title") or Path(filename).stem
        pl_index  = info.get("playlist_index")
        n_entries = info.get("n_entries") or info.get("playlist_count")
        uploader  = info.get("uploader") or info.get("channel") or ""

        # Lazily create overall task if we didn't know n_entries upfront
        if (n_entries and n_entries > 1
                and self._overall_task is None
                and self._n_entries is None):
            self._overall_task = self._overall.add_task(
                "📋  Overall Progress",
                total=n_entries,
                completed=0,
            )
            self._n_entries = n_entries

        if status == "downloading":
            if filename not in self._seen:
                self._seen.add(filename)
                self._start_track(title, uploader, pl_index, n_entries)

            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded  = d.get("downloaded_bytes") or 0

            if filename not in self._dl_tasks:
                self._dl_tasks[filename] = self._track.add_task(
                    "[bold cyan]⬇  Downloading[/]",
                    total=total_bytes or None,
                    completed=downloaded,
                )
            else:
                self._track.update(
                    self._dl_tasks[filename],
                    total=total_bytes or None,
                    completed=downloaded,
                )

        elif status == "finished":
            if filename in self._dl_tasks:
                t   = self._dl_tasks[filename]
                cur = self._track.tasks[t].total or 1
                self._track.update(
                    t,
                    completed=cur,
                    description="[bold green]✓  Downloaded[/]",
                )

        elif status == "already_downloaded":
            if filename not in self._seen:
                self._seen.add(filename)
                self._clear_tasks()
                self._print_track_panel(
                    title=title,
                    uploader=uploader,
                    pl_index=pl_index,
                    n_entries=n_entries,
                    skipped=True,
                    reason="file already exists on disk",
                )
                if pl_index is not None:
                    self._set_completed(pl_index)
                else:
                    self._set_completed(self._completed + 1)

        elif status == "error":
            if filename in self._dl_tasks:
                self._track.update(
                    self._dl_tasks[filename],
                    description="[bold red]✗  Download failed[/]",
                )
            self._finish_track(failed=True)

    # ------------------------------------------------------------------
    # postprocessor_hook
    # ------------------------------------------------------------------
    def pp_hook(self, d: dict) -> None:
        pp_name = d.get("postprocessor", "")
        status  = d.get("status")
        label   = _PP_LABELS.get(pp_name, f"⚙  {pp_name}")

        if status == "started":
            if self._pp_task is None:
                self._pp_task = self._track.add_task(
                    f"[magenta]{label}[/]",
                    total=None,
                )
            else:
                self._track.update(self._pp_task, description=f"[magenta]{label}[/]")

        elif status == "finished" and pp_name in _FINAL_PP:
            self._finish_track()

    # ------------------------------------------------------------------
    # on_archive_skip  (called by _RichLogger)
    # ------------------------------------------------------------------
    def on_archive_skip(self, video_id: str, log_title: str = "") -> None:
        """Print a boxed track card for a video already in the archive."""
        info     = self._id_map.get(video_id, {})
        title    = log_title or info.get("title") or ""
        uploader = info.get("uploader") or ""
        pl_index = info.get("index")
        n        = self._n_entries

        # Fallback index if unknown from id_map
        if pl_index is None:
            if self._n_entries and self._completed < self._n_entries:
                pl_index = self._completed + 1
            else:
                pl_index = self._completed or 1

        # Fallback title lookup by index if video_id wasn't in id_map
        if not title:
            for item in self._id_map.values():
                if item.get("index") == pl_index:
                    title = item.get("title") or ""
                    uploader = uploader or item.get("uploader") or ""
                    break

        if not title:
            title = f"Track {pl_index}" if pl_index else f"ID: {video_id}"

        self._print_track_panel(
            title=title,
            uploader=uploader,
            pl_index=pl_index,
            n_entries=n,
            skipped=True,
            reason="already in archive",
        )
        if pl_index is not None:
            self._set_completed(pl_index)
        else:
            self._set_completed(self._completed + 1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _start_track(
        self,
        title:     str,
        uploader:  str,
        pl_index:  int | None,
        n_entries: int | None,
        video_id:  str = "",
    ) -> None:
        """Catch up the overall bar, clear previous bars, show new panel."""
        # Check id_map if title or pl_index is missing
        if video_id and video_id in self._id_map:
            map_info  = self._id_map[video_id]
            title     = title or map_info.get("title") or ""
            uploader  = uploader or map_info.get("uploader") or ""
            pl_index  = pl_index or map_info.get("index")

        if not pl_index and self._n_entries and self._completed < self._n_entries:
            pl_index = self._completed + 1

        if pl_index and not title:
            for item in self._id_map.values():
                if item.get("index") == pl_index:
                    title    = item.get("title") or ""
                    uploader = uploader or item.get("uploader") or ""
                    break

        if not title:
            title = f"Track {pl_index}" if pl_index else "Audio Track"

        if pl_index is not None and pl_index - 1 > self._completed:
            self._set_completed(pl_index - 1)

        self._clear_tasks()

        self._current_track = {
            "title":     title,
            "uploader":  uploader,
            "pl_index":  pl_index,
            "n_entries": n_entries or self._n_entries,
        }

        n = n_entries or self._n_entries
        if pl_index is not None and n is not None:
            badge = (
                f" [dim]Track[/] [bold magenta]{pl_index}[/]"
                f" [dim]of[/] [bold]{n}[/] "
            )
        else:
            badge = " Now downloading "

        self._display.show(title, uploader, badge, border="cyan", icon="🎵")

    def _finish_track(self, failed: bool = False) -> None:
        """
        Called when a track is fully complete (PP done or error).
        Clears the live panel and prints a boxed card for the track.
        """
        ct       = self._current_track
        pl_index = ct.get("pl_index")
        title    = ct.get("title") or ""
        uploader = ct.get("uploader") or ""
        n        = ct.get("n_entries") or self._n_entries

        if not pl_index and self._n_entries and self._completed < self._n_entries:
            pl_index = self._completed + 1

        if not title and pl_index:
            for item in self._id_map.values():
                if item.get("index") == pl_index:
                    title    = item.get("title") or ""
                    uploader = uploader or item.get("uploader") or ""
                    break

        if not title or title == "Completed Track":
            self._clear_tasks()
            self._display.hide()
            return

        if not failed:
            self._print_track_panel(
                title=title,
                uploader=uploader,
                pl_index=pl_index,
                n_entries=n,
                skipped=False,
            )
            if pl_index is not None:
                self._set_completed(pl_index)
            else:
                self._set_completed(self._completed + 1)

        self._clear_tasks()
        self._display.hide()

    def _set_completed(self, n: int) -> None:
        """Advance the overall bar to exactly n if n > current completed."""
        if self._n_entries and n > self._n_entries:
            n = self._n_entries
        if n > self._completed:
            self._completed = n
            if self._overall_task is not None:
                self._overall.update(self._overall_task, completed=n)

    def _clear_tasks(self) -> None:
        """Remove all current download + PP tasks from the track progress."""
        for tid in list(self._dl_tasks.values()):
            try:
                self._track.remove_task(tid)
            except Exception:
                pass
        self._dl_tasks.clear()
        self._remove_pp_task()

    def _remove_pp_task(self) -> None:
        if self._pp_task is not None:
            try:
                self._track.remove_task(self._pp_task)
            except Exception:
                pass
            self._pp_task = None

    def _print_track_panel(
        self,
        title:     str,
        uploader:  str,
        pl_index:  int | None,
        n_entries: int | None,
        skipped:   bool = False,
        reason:    str  = "",
    ) -> None:
        """Print a boxed card (Panel) for a completed or skipped track."""
        if pl_index is not None:
            if pl_index in self._printed_indices:
                return  # Skip printing duplicate card for this track
            self._printed_indices.add(pl_index)

        n = n_entries or self._n_entries
        if pl_index and n:
            badge = f"Track {pl_index} of {n}"
        elif pl_index:
            badge = f"Track {pl_index}"
        else:
            badge = "Track"

        border_style = "dim yellow" if skipped else "cyan dim"
        icon_str     = "⏭  " if skipped else "🎵  "

        title_text = Text()
        title_text.append(icon_str, style="bold yellow" if skipped else "bold magenta")
        title_text.append(title, style="bold white")

        meta = uploader or ""
        if reason:
            meta = f"{meta}  ·  {reason}" if meta else reason
        if meta:
            title_text.append(f"\n   {meta}", style="dim white")

        panel = Panel(
            title_text,
            title=f" [dim]{badge}[/] ",
            title_align="left",
            border_style=border_style,
            padding=(0, 1),
        )
        self._track.console.print(panel)

    def finish(self) -> None:
        """Call after YoutubeDL.download() returns to clean up."""
        # Close any gap at the very end (e.g. playlist ends on archived tracks)
        if self._n_entries:
            self._set_completed(self._n_entries)
        self._clear_tasks()
        self._display.hide()


class _RichLogger:
    """
    Custom yt-dlp logger that intercepts archive-skip log messages and
    routes them through the Rich UI instead of printing raw text.

    yt-dlp emits:
        "[download] <video_id>: <title> has already been recorded in the archive"
    as a debug-level message for every skipped playlist entry.
    """

    _ARCHIVE_MARKER = "has already been recorded in"

    def __init__(self, hooks: _RichHooks) -> None:
        self._hooks = hooks

    def debug(self, msg: str) -> None:
        if self._ARCHIVE_MARKER in msg:
            video_id  = "unknown"
            log_title = ""
            try:
                # Format: "[download] <video_id>: <title> has already been recorded in …"
                content = msg.split("] ", 1)[1]
                prefix, _ = content.split(self._ARCHIVE_MARKER, 1)
                prefix = prefix.strip()
                if ":" in prefix:
                    video_id, log_title = prefix.split(":", 1)
                    video_id  = video_id.strip()
                    log_title = log_title.strip()
                else:
                    video_id = prefix.split()[0].rstrip(":")
            except (IndexError, ValueError):
                pass
            self._hooks.on_archive_skip(video_id, log_title)

    def info(self,    msg: str) -> None: pass  # noqa: E704
    def warning(self, msg: str) -> None: pass  # noqa: E704
    def error(self,   msg: str) -> None: pass  # noqa: E704


# ---------------------------------------------------------------------------
# Core download function
# ---------------------------------------------------------------------------

def download(
    url:           str,
    mode:          str,
    playlist:      bool,
    playlist_name: str | None       = None,
    n_entries:     int | None       = None,
    id_map:        dict[str, dict]  | None = None,
) -> None:
    """
    Download video or audio with a unified live Rich display.

    mode          : "video" | "audio"
    playlist      : True  → download entire playlist
                    False → single video only
    playlist_name : Playlist folder name under OUTPUT_DIR (playlists only).
    n_entries     : Known playlist size; drives the overall progress bar.
    id_map        : video_id → {title, index} pre-fetched from get_playlist_info.
    """
    if playlist and playlist_name:
        folder = OUTPUT_DIR / _sanitise_folder_name(playlist_name)
    else:
        folder = OUTPUT_DIR
    folder.mkdir(parents=True, exist_ok=True)

    outtmpl = str(folder / "%(title)s.%(ext)s")
    archive  = str(ARCHIVE_FILE)

    overall_progress = _make_overall_progress()
    track_progress   = _make_track_progress()
    display          = _TrackDisplay(overall_progress, track_progress)
    hooks            = _RichHooks(overall_progress, track_progress, display, n_entries,
                                  id_map=id_map)
    logger           = _RichLogger(hooks)

    common = {
        "outtmpl":                 outtmpl,
        "noplaylist":              not playlist,
        "download_archive":        archive,
        "ignoreerrors":            True,
        "retries":                 10,
        "fragment_retries":        10,
        "sleep_interval_requests": 1,
        "quiet":                   True,
        "no_warnings":             True,
        "logger":                  logger,
        "progress_hooks":          [hooks.progress_hook],
        "postprocessor_hooks":     [hooks.pp_hook],
    }

    if mode == "video":
        options = {
            **common,
            "format":              "bv*+ba/b",
            "merge_output_format": "mp4",
        }
    else:  # audio
        options = {
            **common,
            "format":         "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "0",
                },
                {
                    "key":          "FFmpegMetadata",
                    "add_metadata": True,
                },
                {
                    "key": "EmbedThumbnail",
                    "already_have_thumbnail": False,
                },
            ],
        }

    try:
        with YoutubeDL(options) as ydl:
            with Live(display, refresh_per_second=10, console=console):
                ydl.download([url])
    finally:
        hooks.finish()
        # Clean up leftover standalone thumbnail files (.webp, .jpg, etc.)
        if mode == "audio":
            for ext in ("*.webp", "*.jpg", "*.jpeg", "*.png"):
                for img_file in folder.glob(ext):
                    try:
                        img_file.unlink()
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Rich UI helpers
# ---------------------------------------------------------------------------

def prompt_choice(prompt: str, options: list[str]) -> int:
    """Display a Rich-styled numbered menu and return the 1-based index."""
    while True:
        console.print()
        console.rule(f"[heading]{prompt}[/]", style="dim")
        for i, option in enumerate(options, 1):
            console.print(f"  [accent][[bold]{i}[/]][/] {option}")
        console.print()
        raw = Prompt.ask("[info]Enter choice[/]").strip()
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(options):
                return choice
        console.print(
            f"  [error]✗[/]  Please enter a number between [bold]1[/] and [bold]{len(options)}[/]."
        )


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
    seen_ids:    dict[str, str]             = {}
    unique_urls: list[str]                  = []
    duplicates:  list[tuple[int, str, str]] = []

    with filepath.open(encoding="utf-8", errors="replace") as fh:
        for line_no, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if _YT_RE.match(line):
                url = line
            else:
                m   = _YT_RE.search(line)
                url = m.group(0) if m else None

            if url is None:
                continue

            canonical = extract_video_id(url)

            if canonical and canonical in seen_ids:
                duplicates.append((line_no, url, canonical))
                console.print(
                    f"  [warn]⚠[/]  Line {line_no}: duplicate of line already queued "
                    f"[muted]({canonical})[/] — skipping."
                )
            else:
                unique_urls.append(url)
                if canonical:
                    seen_ids[canonical] = url

    return unique_urls, duplicates


def handle_batch_file(filepath: Path) -> None:
    """Interactively download all unique URLs found inside a .txt file."""
    console.print(f"\n  [success]✓[/]  Text file detected: [bold]{filepath.name}[/]")
    console.print("  Scanning for URLs …\n")

    urls, duplicates = read_urls_from_file(filepath)

    if not urls:
        console.print("  [error]✗[/]  No valid YouTube URLs found in the file.")
        return

    # Show found URLs in a table
    table = Table(
        box=box.ROUNDED,
        border_style="dim cyan",
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("#",   style="accent", justify="right", no_wrap=True)
    table.add_column("URL", style="url",    overflow="fold")
    for i, u in enumerate(urls, 1):
        table.add_row(str(i), u)

    suffix = f" [warn]({len(duplicates)} duplicate(s) skipped)[/]" if duplicates else ""
    console.print(
        f"\n  [success]✓[/]  Found [bold]{len(urls)}[/] unique URL(s) to download{suffix}\n"
    )
    console.print(table)

    choice = prompt_choice(
        "What would you like to download for ALL URLs?",
        [
            "Highest quality video  (MP4)",
            "Highest quality audio  (MP3)",
            "Cancel",
        ],
    )
    if choice == 3:
        console.print("\n[warn]Cancelled.[/]")
        return

    mode  = "video" if choice == 1 else "audio"
    label = "videos" if mode == "video" else "audio files"

    console.print(
        f"\n  Download [bold]{label}[/] for all [bold]{len(urls)}[/] URL(s) to\n"
        f"  [url]{OUTPUT_DIR}[/]"
    )
    if not Confirm.ask("  Confirm?", default=False):
        console.print("\n[warn]Cancelled.[/]")
        return

    console.print(f"\n[info]Downloading …[/] [muted](saving to {OUTPUT_DIR})[/]\n")
    failed:             list[tuple[int, str, str]] = []
    already_downloaded: list[tuple[int, str]]      = []

    for idx, url in enumerate(urls, 1):
        console.rule(
            f"[heading][{idx}/{len(urls)}][/] [url]{url}[/]",
            style="dim",
        )
        try:
            archive_before = _read_archive()
            is_pl, pl_name, n_ent, ent_map = get_playlist_info(url)
            if is_pl and pl_name:
                console.print(
                    f"  [info]ℹ[/]  Playlist: [bold]{pl_name!r}[/] → saving to subfolder"
                )
            download(url, mode, playlist=is_pl, playlist_name=pl_name,
                     n_entries=n_ent, id_map=ent_map)
            archive_after = _read_archive()

            if not (archive_after - archive_before):
                console.print("  [warn]⚠[/]  Already downloaded — skipped.")
                already_downloaded.append((idx, url))
            else:
                console.print("  [success]✓[/]  Done.")
        except Exception as e:
            console.print(f"  [error]✗[/]  Failed: {e}")
            failed.append((idx, url, str(e)))

    _print_summary(len(urls), duplicates, already_downloaded, failed)


# ---------------------------------------------------------------------------
# Single-URL handler
# ---------------------------------------------------------------------------

def handle_single_url(url: str) -> None:
    """Interactively download a single video or playlist URL."""
    with Status("[info]Checking URL …[/]", console=console, spinner="dots"):
        try:
            playlist, playlist_name, n_entries, id_map = get_playlist_info(url)
        except Exception as e:
            console.print(f"\n  [error]✗[/]  Could not fetch URL info: {e}")
            return

    if playlist:
        dest  = OUTPUT_DIR / _sanitise_folder_name(playlist_name or "playlist")
        n_str = f"  ({n_entries} tracks)" if n_entries else ""
        console.print(
            Panel(
                Text.assemble(
                    ("Playlist detected\n", "bold green"),
                    (playlist_name or "Unknown playlist", "bold white"),
                    (n_str, "dim white"),
                ),
                border_style="green",
                expand=False,
            )
        )
        choice = prompt_choice(
            "What would you like to download?",
            [
                "All videos  (highest quality MP4)",
                "All audio   (highest quality MP3)",
                "Cancel",
            ],
        )
        if choice == 3:
            console.print("\n[warn]Cancelled.[/]")
            return

        mode  = "video" if choice == 1 else "audio"
        label = "videos" if mode == "video" else "audio files"
        console.print(
            f"\n  Download all [bold]{label}[/] from this playlist to\n"
            f"  [url]{dest}[/]"
        )
        if not Confirm.ask("  Confirm?", default=False):
            console.print("\n[warn]Cancelled.[/]")
            return

    else:
        dest          = OUTPUT_DIR
        playlist_name = None
        n_entries     = None
        console.print(
            Panel(
                "[success]Single video detected[/]",
                border_style="green",
                expand=False,
            )
        )
        choice = prompt_choice(
            "What would you like to download?",
            [
                "Highest quality video  (MP4)",
                "Highest quality audio  (MP3)",
                "Cancel",
            ],
        )
        if choice == 3:
            console.print("\n[warn]Cancelled.[/]")
            return

        mode = "video" if choice == 1 else "audio"

    console.print(f"\n[info]Downloading …[/] [muted](saving to {dest})[/]\n")
    archive_before = _read_archive()
    try:
        download(url, mode, playlist, playlist_name=playlist_name,
                 n_entries=n_entries, id_map=id_map if playlist else None)
        archive_after = _read_archive()
        if archive_after - archive_before:
            console.print(
                Panel(
                    Text.assemble(
                        ("✓  Done!\n", "bold green"),
                        ("Files saved to: ", "dim white"),
                        (str(dest), "underline bright_blue"),
                    ),
                    border_style="green",
                    expand=False,
                )
            )
        else:
            console.print(
                f"\n  [warn]⚠[/]  Already downloaded — skipped.\n"
                f"  [muted](Recorded in archive: {ARCHIVE_FILE})[/]"
            )
    except Exception as e:
        console.print(f"\n  [error]✗[/]  Download failed: {e}")


# ---------------------------------------------------------------------------
# Summary panel
# ---------------------------------------------------------------------------

def _print_summary(
    total:              int,
    duplicates:         list[tuple[int, str, str]],
    already_downloaded: list[tuple[int, str]],
    failed:             list[tuple[int, str, str]],
) -> None:
    skipped   = len(duplicates) + len(already_downloaded)
    succeeded = total - len(failed) - len(already_downloaded)

    stats = Table(box=box.SIMPLE_HEAD, border_style="dim", show_header=False, expand=False)
    stats.add_column("Label", style="muted",  no_wrap=True)
    stats.add_column("Value", style="heading", justify="right", no_wrap=True)
    stats.add_row("Total queued",           str(total))
    stats.add_row("[success]Downloaded[/]",  f"[success]{succeeded}[/]")
    stats.add_row("[warn]Skipped[/]",        f"[warn]{skipped}[/]")
    stats.add_row("[error]Failed[/]",        f"[error]{len(failed)}[/]")

    console.print()
    console.print(Panel(stats, title="[heading]  Summary  [/]", border_style="cyan", expand=False))

    if duplicates:
        console.print(f"\n[warn]── Duplicates skipped ({len(duplicates)}) ──[/]")
        for line_no, url, cid in duplicates:
            console.print(f"  Line [bold]{line_no}[/]: [url]{url}[/]")
            console.print(f"  [muted](same as {cid})[/]")

    if already_downloaded:
        console.print(f"\n[warn]── Already downloaded ({len(already_downloaded)}) ──[/]")
        for idx, url in already_downloaded:
            console.print(f"  [[bold]{idx}[/]] [url]{url}[/]")

    if failed:
        console.print(f"\n[error]── Errors ({len(failed)}) ──[/]")
        for idx, url, err in failed:
            console.print(f"  [[bold]{idx}[/]] [url]{url}[/]")
            console.print(f"  [error]→ {err}[/]")

    if not duplicates and not already_downloaded and not failed:
        console.print(f"\n  [success]✓  All {total} item(s) downloaded successfully![/]")

    console.print(f"\n  [muted]Files saved to:[/] [url]{OUTPUT_DIR}[/]\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ensure_output_dir()

    banner = Text(justify="center")
    banner.append("▶  YouTube Downloader\n", style="bold magenta")
    banner.append("Download videos & playlists as MP4 or MP3\n\n", style="dim white")
    banner.append("Developed by ", style="dim white")
    banner.append("Biswajit Basak", style="bold cyan")
    console.print(Panel(banner, border_style="magenta", padding=(1, 4)))

    hints = Table.grid(padding=(0, 2))
    hints.add_column(style="accent")
    hints.add_column(style="muted")
    hints.add_row("•", "A YouTube video URL")
    hints.add_row("•", "A YouTube playlist URL")
    hints.add_row("•", "A path to a .txt file with one URL per line")
    console.print(Panel(hints, title="[muted]You can enter[/]", border_style="dim", expand=False))

    raw = Prompt.ask("\n[info]URL or file path[/]").strip()
    if not raw:
        console.print("[warn]Nothing entered. Exiting.[/]")
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
            console.print(f"  [error]✗[/]  File not found: [bold]{candidate}[/]")
        return

    # --- treat as a URL ---
    if not _YT_RE.match(raw):
        console.print(
            "  [error]✗[/]  Input doesn't look like a YouTube URL or a .txt file path."
        )
        return

    handle_single_url(raw)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(
            "\n\n  [warn]⏹  Stopped.[/]  [muted]Download interrupted by user.[/]\n"
        )