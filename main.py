from pathlib import Path
from yt_dlp import YoutubeDL

url = input("Enter YouTube URL: ").strip()

downloads_folder = Path.home() / "Downloads"

options = {
    "format": "bv*+ba/b",
    "merge_output_format": "mp4",
    "outtmpl": str(downloads_folder / "%(title)s.%(ext)s"),
    "noplaylist": True,
}

with YoutubeDL(options) as ydl:
    ydl.download([url])

print(f"Video saved to: {downloads_folder}")