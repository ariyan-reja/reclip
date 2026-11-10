# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads and Playlist support — paste multiple URLs or playlist links
- Automatic URL deduplication
- Browser cookies support & advanced bot bypass
- Subtitle embedding and language selection
- Video trimming / direct slice downloading (lossless stream copy)
- Custom output folders
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend

## Quick Start

**Option 1: Windows (PowerShell)**
If you are on Windows, you can start the application by creating a virtual environment and running `app.py`:
```powershell
git clone https://github.com/ariyan-reja/reclip.git
cd reclip
python -m venv venv
.\venv\Scripts\pip install flask yt-dlp
.\venv\Scripts\python app.py
```

**Option 2: Mac / Linux / WSL**
If you have bash and `ffmpeg` installed, you can simply run the provided shell script:
```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone https://github.com/ariyan-reja/reclip.git
cd reclip
./reclip.sh
```

**Option 3: Docker**
If you have Docker installed:
```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

After starting the server, open **http://localhost:8899** in your browser.

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution, trim options, or subtitles if available
5. Click **Download** on individual videos, or **Download All**

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask
- **Frontend:** HTML/CSS/JS templates
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** Flask, yt-dlp

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.
