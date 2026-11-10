# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)

**[Live Demo →](https://ariyan-reja.github.io/reclip/)** — runs in your browser with simulated data, no install needed.

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads and Playlist support — paste multiple URLs or playlist links
- Automatic URL deduplication
- Browser cookies support & automatic YouTube age-restriction / bot-check handling
- Subtitle embedding and language selection
- Video trimming / direct slice downloading (lossless stream copy)
- Custom output folders
- Runs on Android via Termux
- Built-in Setup & Update guide
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

**Option 3: Android (Termux)**
Install [Termux](https://f-droid.org/packages/com.termux/) from F-Droid or GitHub (not the Play Store), then:
```bash
pkg update -y && pkg upgrade -y
pkg install -y python ffmpeg git nodejs
pip install -U flask yt-dlp yt-dlp-ejs
git clone https://github.com/ariyan-reja/reclip.git
cd reclip
termux-setup-storage    # optional: allow saving to phone storage
python app.py
```
Then open **http://localhost:8899** in the phone's browser. `nodejs` and `yt-dlp-ejs` are needed for YouTube to offer more than 360p. Files are saved to `Download/ReClip` by default.

**Option 4: Docker**
If you have Docker installed:
```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

After starting the server, open **http://localhost:8899** in your browser. Step-by-step setup and update instructions for every platform are also available inside the app (**Setup & Update**).

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
- **Dependencies:** Flask, yt-dlp (Android also needs Node.js and yt-dlp-ejs for HD YouTube)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.
