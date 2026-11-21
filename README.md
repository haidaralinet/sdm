# Subzeo Download Manager

## Run from source
    pip install -r requirements.txt
    python subzeo.py

## Browser extension (Chrome / Edge / Brave)
1. Open `chrome://extensions`, turn on Developer mode, click **Load unpacked**, pick the `extension` folder.
2. In Subzeo open **Settings** and copy the *Browser extension token*.
3. Click the Subzeo icon in the browser toolbar, paste the token, press **Save & test**.
Browser downloads now go to Subzeo (if Subzeo is not running, the browser downloads normally).
Right-click a link or video: *Download with Subzeo* / *Download this video with Subzeo*.

## Video / audio
Toolbar **Video** button (Ctrl+Shift+V). Needs `yt-dlp`; install `ffmpeg` for HD merging and MP3.

## Scheduler
Toolbar **Scheduler**: start/stop the queue at set times, and shut down / sleep / close when everything finishes.
Add downloads with "Start immediately" unchecked so they wait for the scheduled start.

## Windows installer
Run `build.bat` (needs Python; Inno Setup 6 for the installer). Output: `dist\Subzeo\Subzeo.exe` and `Output\Subzeo-Setup.exe`.
