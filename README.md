# FlipBook Downloader

A Python tool that downloads flipbook publications as PDF files. Supports both unprotected books (direct download) and DRM-protected books (via embedded WASM decoder).

## Features

- Auto-detects the flipbook platform from the URL and picks the right download strategy
- Handles unprotected books with a fast threaded downloader
- Handles DRM-protected books by launching a headless browser, capturing the encryption keys, decoding the obfuscated page index, and downloading from the real image URLs
- Batch queue: paste multiple URLs, walk away, come back to a folder of PDFs
- Binary-searches to find the true page count instead of blindly requesting hundreds of non-existent pages
- Validates downloaded images and discards corrupt ones before assembling the PDF
- Retries with exponential backoff on rate-limited or timed-out requests
- Passes Cloudflare bot challenges by reusing browser cookies for image downloads
- Built-in dependency checker with guided installation

## Supported Platforms

Flipbook publishers with URLs shaped like `<platform>.com/<user>/<book>/` where the book is hosted on a subdomain starting with `online.`. The two major flipbook hosts share the same underlying obfuscation, so both are handled by the same code path.

## Requirements

The script checks all of these at startup and offers to install what it can.

| Dependency | Required for | Auto-install |
|---|---|---|
| Python 3.8 or newer | Everything | No, install from python.org |
| pip packages: `httpx`, `img2pdf`, `Pillow`, `playwright`, `tqdm`, `requests` | Everything | Yes, via pip |
| Playwright Chromium browser binary | DRM-protected books | Yes, via `playwright install chromium` |
| Node.js LTS | DRM-protected books only | No, download from [nodejs.org](https://nodejs.org/en/download) |

If Node.js is missing, unprotected books will still work. You only need Node when you hit a book that requires the WASM decoder path.

### Manual installation

If you would rather install everything yourself instead of letting the script prompt you:

```
pip install httpx img2pdf Pillow playwright tqdm requests
python -m playwright install chromium
```

Then install Node.js LTS from [nodejs.org](https://nodejs.org/en/download).

## How It Works

The script handles two very different situations under one interface.

**Unprotected books.** Page images are served at predictable URLs like `/files/mobile/1.webp`, `/files/mobile/2.webp`, and so on. The script binary-searches to find how many pages actually exist, downloads them in parallel using a thread pool, and stitches them into a PDF.

**DRM-protected books.** The predictable page URLs return HTTP 403. The real page URLs are content-hashed filenames (like `ae178bb7f19d7ef4285184c0efc1b12f.webp`) hidden inside an obfuscated blob in the book's `config.js`. Decoding that blob requires a WASM module the site loads into the browser at runtime. The script handles this by:

1. Launching a headless Chromium instance via Playwright against the book's landing page
2. Intercepting the `deString.js` and `deString.wasm` files as they load
3. Capturing the `config.js` and any Cloudflare clearance cookies the browser earned
4. Running Node.js with an embedded script that loads the WASM module and decodes the blob
5. Extracting the real image URLs from the decoded JSON
6. Downloading each image with throttled concurrency, the browser's cookies, and retry-with-backoff
7. Validating each downloaded file and assembling the surviving pages into a PDF

The probe to decide which path to take is a single HTTP request against page 1 of the fast path. If it returns a real image, the fast path runs. If it returns 403, the WASM path kicks in.

## How To Run

1. Clone or download the repo
2. Open a terminal in the repo folder
3. Run the script:

```
python FlipBookDownloader.py
```

On first run, the dependency checker will walk you through installing anything missing.

Once it starts, paste one or more URLs, optionally give each a custom filename, then type `done` to kick off the queue. Finished PDFs land in whatever directory you ran the script from.

## Sample Run

```
PS C:\Users\YourName\Downloads> python FlipBookDownloader.py
========================================
 Checking dependencies...
========================================
   Python packages: all present
   Playwright Chromium: present
   Node.js: present
========================================

========================================
   FlipBook Batch Downloader
   Supports: AnyFlip + FlipHTML5
========================================

 Enter URL (or type 'done' to start): https://example-flipbook-host.com/user123/samplebook/
   -> Detected: anyflip
 Enter custom filename (Leave blank for Auto-title):
 Added to queue. (Total: 1)

 Enter URL (or type 'done' to start): done

 Starting Download Queue...

--- Processing Book 1/1 ---
INFO: Detected: AnyFlip
INFO: Target Book ID: user123/samplebook
INFO: Probing book for DRM protection...
INFO: Book is unprotected - using fast direct-download path.
INFO: Finding page count...
INFO:    -> Book has 285 pages
Downloading pages: 100%|███████████████████| 285/285 [00:18<00:00, 15.68page/s]
INFO: Determined Output Filename: user123_samplebook.pdf
Creating PDF: 100%|████████████████████████| 285/285 [00:09<00:00, 30.49page/s]
INFO: PDF Successfully Saved: user123_samplebook.pdf

========================================
          All Done. Enjoy!
========================================
```

## Troubleshooting

**"No such file: deString.wasm" when processing a protected book.** The headless browser couldn't intercept the WASM key in time. Run the script again, it is almost always transient. The script waits up to 20 seconds for both keys to land and falls back to extracting the WASM from the base64-embedded copy in `deString.js` if the separate file never arrives, so this error has become rare.

**"Could not find configuration file."** The site's Cloudflare bot-detection rejected the request. The script handles this by reusing the headless browser's cookies, but if your IP is particularly heavily flagged you may need a different network. VPN toggling usually resolves it.

**"All download attempts failed."** Check the last-error reason in the log. `ReadTimeout` or `HTTP 429` means you are being rate-limited, wait a few minutes and try again, or reduce concurrency in the script (the semaphore value near the bottom of `run_wasm_decoder_path`).

**Node.js errors when a protected book is being processed.** Make sure `node --version` works in your terminal. If you just installed Node, restart your terminal so it picks up the PATH change.

**The PDF is missing pages.** The script retries failed downloads with exponential backoff, but aggressive rate-limiting can still produce gaps. Re-running the same URL usually fills them in.

## File Cleanup

All downloaded images live in Python `tempfile.TemporaryDirectory` folders that are automatically deleted when the book finishes, success or failure. The only file that persists is the final PDF in your working directory. If you hard-kill the script mid-run, stray folders named `tmpXXXXXX` may remain in your system temp directory (`%TEMP%` on Windows, `/tmp` on macOS/Linux) and can be safely deleted.

## Disclaimer

This tool is provided for personal use only. You are solely responsible for how you use it and for ensuring your use complies with the terms of service of any site you interact with, as well as applicable copyright and intellectual property laws in your jurisdiction.

The author accepts no responsibility or liability for any material downloaded using this tool, for any misuse of the tool, or for any legal consequences arising from its use. Downloading copyrighted content without the rightsholder's permission may be illegal where you live. If you do not have the right to download a given flipbook, do not download it.

This tool is not affiliated with, endorsed by, or sponsored by any flipbook hosting platform.

## License

Released into the public domain under [The Unlicense](https://unlicense.org/). Do whatever you want with it.
