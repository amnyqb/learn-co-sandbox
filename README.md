
# vid_down

A local M3U8 video downloader and converter. Paste a stream URL, choose a format and size preset, then download the finished file. A small browser interface talks to FFmpeg running on your own machine; no upload or hosted conversion service is involved.

## Run

The cloud workspace and your computer have separate files and shell settings. Installing an alias here does not install it on your Mac. Clone the app branch onto your computer first:

```sh
git clone --branch codex/vid-down https://github.com/amnyqb/learn-co-sandbox.git vid_down
cd vid_down
```

For a Mac using Zsh, open a terminal in your downloaded `vid_down` directory and run:

```sh
brew install python@3.12 ffmpeg
"$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x run.sh
printf 'alias vid_down=%q\n' "$PWD/run.sh" >> ~/.zshrc
source ~/.zshrc
vid_down
```

If Homebrew is not installed, install it from https://brew.sh first. The alias uses the absolute checkout path, so it works from other directories. Run the alias installation again if you move the folder. Open `http://127.0.0.1:8000` in your Mac's browser after starting the app.

In the configured workspace, run `vid_down` from any directory. Open a new Bash terminal, or run `source ~/.bash_aliases` once in an existing terminal to load the alias. The command starts the app in the foreground, or prints its address if it is already running. Stop a server started in your terminal with Ctrl+C.

On another machine, after installing the dependencies below, run `./run.sh` from the project directory. To add the same shortcut to Bash or Zsh, put this line in your shell configuration, replacing the path with your checkout's absolute path:

```sh
alias vid_down='/absolute/path/to/vid_down/run.sh'
```

Requires Python 3.10+ and FFmpeg with H.264, HEVC, VP9, AAC, and Opus encoders. Install FFmpeg with your system package manager (`brew install ffmpeg` on macOS, or `sudo apt install ffmpeg` on Debian/Ubuntu).

From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

If you use uv instead:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python app.py
```

Open `http://127.0.0.1:8000` in your browser on the same machine. This cloud checkout already has `.venv` and FFmpeg installed, so only the last command is needed here. Stop the app with Ctrl+C. Set `VID_DOWN_PORT` to use a different port.

## Formats and file sizes

- **MP4 / H.264:** the default, with broad player and browser compatibility.
- **MP4 / HEVC:** often smaller at comparable quality; slower to encode and supported by fewer browsers. A desktop player such as VLC can play it.
- **WebM / VP9:** efficient compression with Opus audio.
- **Smaller file:** caps height at 720p and uses stronger compression.
- **Balanced:** caps height at 1080p and keeps more detail.
- **Keep original:** copies the original video and audio into MP4 without re-encoding. Fast and preserves quality; choose Balanced if the original codecs do not fit MP4.

The app never upscales video. Compression can still produce a larger file when the source is already highly compressed; the downloads list shows the actual result compared with the downloaded source copy.

Expand **More control** to enter a target size, Referer, parallel connection count, and download method. Target size uses two-pass encoding with a small margin for audio and container overhead; short videos may get an extra refinement pass. Treat the target as approximate. Very small targets can reduce quality substantially or be too small for the audio track.

## Parallel downloads

Automatic mode defaults to a **4-request ceiling shared per source across both jobs**. A source is an exact HTTP(S) origin (scheme, hostname, and port). Playlists, segments, keys, redirects, and FFmpeg compatibility requests all use that source's paced request budget. Starting a second job does not double the ceiling. If jobs specify different ceilings for the same source, the lower ceiling applies while both are active. Different origins have independent budgets.

Requests start gradually rather than in a burst. At the default ceiling, starts are spaced at least 100 ms apart, with a small random delay; higher manually selected ceilings shorten that interval. Concurrency also depends on the playlist and available file handles. **Connection ceiling** in More control allows 1–128; raise it only when the source permits that load. The app never increases above your selected ceiling. No setting guarantees that a source will permit the requests or prevents server blocking.

At startup the app tries to raise its own open-file soft limit to 2048, within the operating system's hard limit. If the limit cannot be raised, it automatically reduces higher requested ceilings to budget sockets and segment files across two jobs. A limit of 256 permits a ceiling of 32 before per-source limits apply. This prevents a low macOS file limit from being mistaken for a disk-space or permissions problem. Storage errors separately identify full disks/quotas, permissions, read-only disks, and exhausted file handles. For an older version, reduce connections to 4 as a workaround, then update and restart the app.

The downloader reuses HTTP connections, streams segments to disk without loading entire videos into memory, and preserves playlist order even when segments finish out of order. Video and separate audio tracks, AES-128 keys, fMP4 initialization data, and segment byte ranges are supported. It selects the highest-resolution video variant first. Progress shows completed resources, effective connection count, and measured average transfer speed.

Timeouts, interrupted transfers, and HTTP 408/425/429/500/502/503/504 receive up to four retries with backoff and small random delays. HTTP 429/503, or any retriable response with Retry-After, halves the shared source limit and pauses all jobs contacting that origin. Retry-After is treated as a minimum wait, including values longer than two minutes. For a wait longer than five minutes the job stops and reports the cooldown; other jobs still respect that source's pause. Recovery adds at most one connection after 30 seconds without another throttle and enough successful requests, within the selected ceiling. A new job retains the source's existing cooldown. HTTP 401/403 do not trigger automatic retries or a change of identity. Failed attempts are removed before retrying. Cancellation closes in-flight requests and removes temporary segments. The app downloads a finite stream completely before assembling and encoding it; temporary segments are removed after assembly.

**Automatic** tries parallel download first and falls back to FFmpeg for unsupported playlist features or local assembly compatibility. **Parallel segments only** keeps all fetching on the concurrent downloader. **FFmpeg compatibility** is a slower, sequential transport option. Automatic never treats denied access as a reason to reissue the same request with a different downloader.

## Access and alternatives

Expand **More control → Access & alternatives**:

- **Expired or signed URL:** replace it with a current M3U8 URL supplied by the source.
- **Referer or User-Agent requirements:** set the source page as Referer. The User-Agent helper copies this browser's actual User-Agent.
- **Session authentication:** enter your existing Cookie header or custom request headers as a JSON object, such as `{"Origin":"https://example.com"}`. Authorization headers are supported. Values are held for the active job and excluded from saved history. Custom headers and cookies are sent only to the exact origin (scheme, hostname, and port) of the primary Stream URL, and are stripped on redirects to another origin. Referer and User-Agent apply to all stream requests. All methods support origin-scoped credentials. FFmpeg compatibility uses a temporary loopback relay, with the same HTTPX credential scoping and TLS verification as parallel mode; FFmpeg never receives credentials or signed source URLs in its command line.
- **Alternative source:** supply up to five alternative M3U8 URLs, one per line. The app tries them in order when a source fails, recording only the hostname and safe error message. Supply alternatives for the same video; the app cannot establish content equivalence. Credentials remain scoped to the primary URL's origin, even when trying another source.
- **Configured proxy:** both transports respect existing HTTP_PROXY/HTTPS_PROXY and NO_PROXY settings. Configure your own approved proxy before starting the app. No separate proxy service is installed and TLS verification remains enabled.
- **Cloud network restrictions:** permit the required playlist, segment, and key domains in environment settings, or run the app on your own computer. A different downloader cannot override a cloud allowlist, login requirement, service access decision, CAPTCHA, or DRM.

HTTP 401/403 errors identify the responding host and the relevant access requirements. Requests are not retried indefinitely. No credentials, proxy values, signed URLs, or custom header values are written into job history.

## Downloads

The app handles completed, on-demand HLS streams with relative segment links, master playlists, separate audio tracks, and standard AES-128 streams supported by FFmpeg. It selects the highest-resolution video available in a master playlist, then applies your chosen output preset. DRM-protected streams and live recording are not supported. When running in a cloud environment, source, segment, and key domains also need to be permitted by that environment's network policy.

Two jobs can run concurrently; further jobs wait in the queue. Cancel stops the running process and removes temporary video and encoding files. Preview uses your browser's codec support. Remove asks for confirmation before deleting the output from this machine.

Files and job history live under `.local/` and persist across restarts. Incomplete jobs are marked interrupted when the app starts again. Temporary source files, segments, and two-pass statistics are removed after processing. Stream URLs and Referer URLs are not saved in job history. To move storage elsewhere, set `VID_DOWN_DATA` to an absolute directory path. Allow room for segments plus the assembled original during download, and for the original plus the encoded output during conversion.

The server binds to `127.0.0.1` and rejects cross-origin changes. Keep it local; it is a single-user tool without remote-user authentication.

## Test

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests generate a video and serve it as local HLS. They exercise actual MP4, HEVC, and WebM conversion, target sizes, original stream copying, highest-resolution master selection and separate audio, encrypted and fragmented streams, byte ranges, authentication, redirect credential scoping, rate-limit backoff, alternative URLs, FFmpeg fallback, cancellation and cleanup, persistence, and validation. A delayed local segment server measures serial versus concurrent fetching and checks request limits and playlist ordering. No external video provider is contacted; the benchmark is not a promise of speed on an arbitrary service.

Storage regression tests run two concurrent 96-segment downloads in a subprocess limited to 256 file handles, verify every downloaded byte and playlist order, and check that disk/permission errors and wrapped socket-limit failures remain distinct without exposing credentials.

Source-limit tests observe requests from two jobs to verify the shared four-request ceiling and spacing, shared 429/503 cooldowns, retention of throttling across new jobs, long Retry-After values, redirect and relay budgets, and cancellation without leaking request slots.
