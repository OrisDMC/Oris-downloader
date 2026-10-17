# Real YouTube downloads

This service downloads with yt-dlp, merges video and sound with FFmpeg, and decodes the completed file before making it available. Failed preparation is retried once with fresh stream URLs; yt-dlp also retries network requests and fragments. It never returns a YouTube watch page as a media file.

## Run on this computer

Requires Python 3.10+ and Node.js 22+ on PATH. From the VibeBuddy directory:

```powershell
npm run youtube:setup
npm run youtube:server
```

Keep the service terminal running while downloading. Dependencies and downloaded files stay inside this project under ignored `.runtime` and `.cache` folders. Restart the service after changes. The service uses port 8787 and listens only on this computer by default. Local Expo web pages on ports 8081–8083 or 19006 connect automatically.

## Phone or hosted app

Set the app's `.env.local` to the computer's reachable address, for example:

```dotenv
EXPO_PUBLIC_YOUTUBE_DOWNLOAD_SERVER=http://192.168.1.10:8787
```

Restart Expo after changing the address. On a trusted private network, start the service with:

```powershell
$env:YOUTUBE_HOST = '0.0.0.0'
npm run youtube:server
```

The phone must be on the same network and the firewall must allow port 8787. Native release apps may require HTTPS instead of local HTTP. For a hosted app, use an HTTPS service address and set `YOUTUBE_ALLOWED_ORIGINS` to a comma-separated list of the exact web app origins. This is a local development service: add authentication, per-user limits, HTTPS and production process management before exposing it to the public internet. Do not put private server credentials in `EXPO_PUBLIC_*` variables.

## If YouTube requires sign-in

YouTube may block the server's network address or require an authorized session. The app displays that failure and saves nothing. If you have an authorized Netscape-format YouTube cookies.txt file, explicitly configure its path on the service:

```powershell
$env:YOUTUBE_COOKIE_FILE = 'C:\private\youtube-cookies.txt'
npm run youtube:server
```

An export explicitly saved as `.runtime/youtube-cookies.txt` is reused automatically on service restart. This folder is excluded from Git. The service does not read browser profiles automatically. Keep that file outside the repository; do not paste its contents into chat or include it in a client build. Cookies do not guarantee that YouTube will allow the request. See [yt-dlp's cookie instructions](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp).

## Behavior

- Inspection reports available resolutions from the actual video. A selection caps resolution; the final file can use the nearest lower available size. Video output is compatible MP4 with sound; audio output is MP3 at 128 kbps.
- Video downloads default to smaller speech audio: AAC at 32 kbps mono, with the video stream unchanged. Choose Original sound to preserve source audio and stereo. Compact mode reduces music fidelity and does not guarantee a particular file size.
- Video size is shown after preparation, rather than guessed before downloading. Audio size before preparation is explicitly an estimate based on the video's duration.
- The browser downloads directly from the checked file with an attachment filename and exact byte count. No video-sized JavaScript blob is created. A manual Save button remains available if automatic downloading is blocked. Browser files are not entered into the app's offline Vault.
- Native downloads must match the server's exact byte count and expected file signature before entering the Vault. Incomplete transfers are removed.
- At most two preparation/inspection tasks run concurrently. Videos are limited to six hours and completed files to 2 GB. Failed jobs are removed; finished jobs expire after one hour. Download links expire with their jobs.
- Server preparation progress and device-transfer progress are distinct stages. Browser saving is handled by the browser; the app does not claim to know when the browser has finished writing to disk.

## Verify

```powershell
npm run youtube:test
npm test -- --runTestsByPath __tests__/youtubeDownloader.test.ts
npm run typecheck
```

The service tests generate a real MP4 with video and sound, decode it, and check byte-for-byte HTTP delivery. They also cover failed preparation retries and invalid downloads. They do not prove that YouTube accepts the current connection. Test a permitted public video in the app to verify that external dependency separately.

## Dependency maintenance

Dependencies are pinned in `requirements.txt` to the versions tested here. YouTube changes frequently; update yt-dlp and its matching default dependencies together, then repeat the checks. The supported JavaScript runtime and EJS requirements are documented in [yt-dlp's setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
