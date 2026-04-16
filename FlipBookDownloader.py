import os
import sys
import shutil
import subprocess
import importlib.util
import tempfile
import asyncio
import logging
import json
import base64
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# DEPENDENCY CHECK & INSTALLER
# ==============================================================================
REQUIRED_PACKAGES = {
    "httpx": "httpx",
    "img2pdf": "img2pdf",
    "PIL": "Pillow",
    "playwright": "playwright",
    "tqdm": "tqdm",
    "requests": "requests",
}

NODE_INSTALL_HINT = "https://nodejs.org/en/download  (install the LTS version)"


def _check_pip_packages():
    """Return list of missing pip packages (pip-install names)."""
    missing = []
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        if importlib.util.find_spec(module_name) is None:
            missing.append(pip_name)
    return missing


def _check_node():
    """Return True if `node` is on PATH and runs. Also verifies a usable version."""
    node = shutil.which("node")
    if not node:
        return False
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip().startswith("v"):
            return True
    except Exception:
        pass
    return False


def _check_chromium():
    """Return True if Playwright has Chromium installed. Only callable after playwright import is possible."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser_path = p.chromium.executable_path
            return bool(browser_path and os.path.exists(browser_path))
    except Exception:
        return False


def check_and_install_dependencies():
    print("========================================")
    print("🔎 Checking dependencies...")
    print("========================================")

    # --- Step 1: Python packages ---
    missing_pkgs = _check_pip_packages()
    if missing_pkgs:
        print(f"📦 Missing Python packages: {', '.join(missing_pkgs)}")
        ans = input("Auto-install via pip now? (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", *missing_pkgs])
                print("   ✅ Python packages installed.")
            except subprocess.CalledProcessError:
                print(f"\n❌ pip install failed. Please run manually: pip install {' '.join(missing_pkgs)}")
                sys.exit(1)
        else:
            print(f"\n❌ Cannot proceed without: {', '.join(missing_pkgs)}")
            print(f"   Run: pip install {' '.join(missing_pkgs)}")
            sys.exit(1)
    else:
        print("   ✅ Python packages: all present")

    # --- Step 2: Playwright's Chromium browser binary ---
    # (Safe to check now — the `playwright` package is guaranteed installed above.)
    if not _check_chromium():
        print("🌐 Playwright Chromium browser not installed.")
        ans = input("Auto-install now? This downloads ~200 MB and may take a minute. (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            try:
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
                print("   ✅ Chromium installed.")
            except subprocess.CalledProcessError:
                print("\n❌ playwright install failed. Please run manually: python -m playwright install chromium")
                sys.exit(1)
        else:
            print("\n❌ Cannot proceed without Chromium.")
            print("   Run: python -m playwright install chromium")
            sys.exit(1)
    else:
        print("   ✅ Playwright Chromium: present")

    # --- Step 3: Node.js (can't auto-install — user must install it themselves) ---
    # Node is only strictly required for DRM-protected books. We warn rather than block
    # so unprotected books still work even without Node installed.
    if _check_node():
        print("   ✅ Node.js: present")
    else:
        print()
        print("⚠️  Node.js is NOT installed.")
        print("   Node.js is required for DRM-protected books (decoding the WASM blob).")
        print(f"   Download from: {NODE_INSTALL_HINT}")
        print("   Unprotected books will still work fine without it.")
        print()

    print("========================================\n")


check_and_install_dependencies()

# ==============================================================================
# EXTERNAL IMPORTS (Safe to load now)
# ==============================================================================
import httpx
import img2pdf
import requests
from PIL import Image
from playwright.async_api import async_playwright
import tqdm

# ==============================================================================
# CUSTOM LOGGER
# ==============================================================================
class TqdmLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = False
if logger.hasHandlers():
    logger.handlers.clear()
handler = TqdmLoggingHandler()
handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(handler)

# ==============================================================================
# SHARED HELPERS
# ==============================================================================
def get_safe_filename(title, default="book"):
    if not title:
        title = default
    safe_title = re.sub(r'[\\/*?:"<>|]', "", str(title)).strip()
    if not safe_title.lower().endswith(".pdf"):
        safe_title += ".pdf"
    return safe_title[:100]


def detect_site(url: str) -> str:
    """Return 'anyflip', 'fliphtml5', or 'unknown'."""
    u = url.lower()
    if "anyflip.com" in u:
        return "anyflip"
    if "fliphtml5.com" in u:
        return "fliphtml5"
    return "unknown"


# ==============================================================================
# ANYFLIP — FAST PATH (unprotected books)
# ==============================================================================
ANYFLIP_MAX_PAGES = 500
ANYFLIP_THREADS = 10


def anyflip_extract_book_id(url):
    # Match anyflip.com/<user>/<book> with an optional trailing path (e.g. /basic/, /mobile/) or nothing
    m = re.search(r"anyflip\.com/([^/?#]+)/([^/?#]+)", url)
    if not m:
        raise ValueError("Invalid AnyFlip URL")
    return f"{m.group(1)}/{m.group(2)}"


def anyflip_is_protected(book_id, headers) -> bool:
    """Probe page 1 on the fast path. If it returns 403 (or similar small error body),
    the book is DRM-protected and we need the WASM decoder path."""
    url = f"https://online.anyflip.com/{book_id}/files/mobile/1.webp"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        # 200 + reasonable size = unprotected
        if r.status_code == 200 and len(r.content) > 1000:
            return False
        # 403, 401, or tiny error-page body = protected
        return True
    except Exception:
        # network issue — assume unprotected and let the main loop try
        return False


def anyflip_download_page(book_id, page, headers, out_dir):
    url = f"https://online.anyflip.com/{book_id}/files/mobile/{page}.webp"
    filename = out_dir / f"{page:04d}.webp"

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200 and len(r.content) > 1000:
            with open(filename, "wb") as f:
                f.write(r.content)
            return filename
    except Exception:
        pass

    return None


def anyflip_page_exists(book_id, page, headers):
    """HEAD-check a single page. Returns True if the page exists (200 + real body size),
    False if 403/404. Uses GET rather than HEAD because AnyFlip's CDN doesn't reliably
    support HEAD for these paths."""
    url = f"https://online.anyflip.com/{book_id}/files/mobile/{page}.webp"
    try:
        r = requests.get(url, headers=headers, timeout=10, stream=True)
        # Read just a chunk to confirm real content — we don't need the whole body
        exists = r.status_code == 200 and int(r.headers.get("content-length", "0") or "0") > 1000
        r.close()
        return exists
    except Exception:
        return False


def anyflip_find_last_page(book_id, headers, hard_max=ANYFLIP_MAX_PAGES):
    """Binary-search for the largest N such that page N exists.
    Saves hundreds of wasted 404 requests on shorter books.

    Strategy: exponential search upward to find an upper bound where the page
    does NOT exist, then binary-search between the last confirmed page and that
    upper bound. O(log N) requests instead of N."""
    logger.info("🔍 Finding page count...")

    # Step 1: exponential search to find an upper bound (first missing page)
    if not anyflip_page_exists(book_id, 1, headers):
        return 0  # no pages at all

    lo = 1
    probe = 2
    while probe <= hard_max and anyflip_page_exists(book_id, probe, headers):
        lo = probe
        probe *= 2

    # If we exited because probe exceeded hard_max but hard_max itself exists, we're capped.
    if probe > hard_max:
        if anyflip_page_exists(book_id, hard_max, headers):
            logger.info(f"   → Book has ≥{hard_max} pages (hit hard cap)")
            return hard_max
        hi = hard_max
    else:
        hi = probe  # probe is first page known NOT to exist

    # Step 2: binary search between lo (exists) and hi (does not exist)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if anyflip_page_exists(book_id, mid, headers):
            lo = mid
        else:
            hi = mid

    logger.info(f"   → Book has {lo} pages")
    return lo


def anyflip_download_pages_fast(book_id, out_dir):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://anyflip.com/{book_id}/basic/",
    }

    # Binary-search first so we only attempt pages that actually exist
    last_page = anyflip_find_last_page(book_id, headers)
    if last_page == 0:
        logger.error("No pages found.")
        return []

    images = []

    with ThreadPoolExecutor(max_workers=ANYFLIP_THREADS) as executor:
        futures = {
            executor.submit(anyflip_download_page, book_id, page, headers, out_dir): page
            for page in range(1, last_page + 1)
        }

        with tqdm.tqdm(total=last_page, desc="📥 Downloading pages", unit="page", leave=True) as pbar:
            for future in as_completed(futures):
                result = future.result()
                if result:
                    images.append(result)
                pbar.update(1)

    return images


def _validate_image(path):
    """Check that a downloaded image is not truncated or corrupt before we feed it to the PDF.
    Returns True if the image opens cleanly."""
    try:
        with Image.open(path) as im:
            im.verify()  # Pillow's cheap integrity check
        # verify() leaves the image unusable afterward, so reopen to confirm decode works
        with Image.open(path) as im:
            im.load()
        return True
    except Exception as e:
        logger.warning(f"   ⚠️ Corrupt image discarded: {os.path.basename(str(path))} ({type(e).__name__})")
        return False


def anyflip_make_pdf(images, output_path):
    images = sorted(images)
    if not images:
        logger.error("No images downloaded.")
        return False

    # Filter out corrupt/truncated images before creating the PDF
    valid = [img for img in images if _validate_image(img)]
    if not valid:
        logger.error("No valid images to build PDF.")
        return False
    if len(valid) < len(images):
        logger.warning(f"   Discarded {len(images) - len(valid)} corrupt page(s); building PDF from {len(valid)}.")

    pil_images = []
    for img in tqdm.tqdm(valid, desc="🖨️  Creating PDF", unit="page", leave=True):
        pil_images.append(Image.open(img).convert("RGB"))

    pil_images[0].save(output_path, save_all=True, append_images=pil_images[1:])
    logger.info(f"🎉 PDF Successfully Saved: {output_path}")
    return True


def run_anyflip_fast(book_id: str, custom_filename: str):
    """Fast-path AnyFlip downloader for unprotected books. Returns True on success."""
    with tempfile.TemporaryDirectory() as temp_dir:
        pages_dir = Path(temp_dir) / "pages"
        pages_dir.mkdir(exist_ok=True)

        images = anyflip_download_pages_fast(book_id, pages_dir)

        if custom_filename:
            output_path = get_safe_filename(custom_filename)
        else:
            output_path = get_safe_filename(book_id.replace("/", "_"))

        logger.info(f"Determined Output Filename: {output_path}")
        return anyflip_make_pdf(images, output_path)


async def run_anyflip(book_url: str, custom_filename: str = ""):
    """Dispatches AnyFlip URLs: tries the fast path first; if protected, falls back to the WASM decoder."""
    book_id = anyflip_extract_book_id(book_url)
    logger.info(f"Target Book ID: {book_id}")

    probe_headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://anyflip.com/{book_id}/basic/",
    }

    logger.info("Probing book for DRM protection...")
    protected = anyflip_is_protected(book_id, probe_headers)

    if not protected:
        logger.info("🟢 Book is unprotected — using fast direct-download path.")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_anyflip_fast, book_id, custom_filename)
        return

    logger.info("🔒 Book is DRM-protected — falling back to WASM decoder path.")
    await run_wasm_decoder_path(
        platform="anyflip",
        book_id=book_id,
        landing_url=f"https://anyflip.com/{book_id}/",
        config_host="online.anyflip.com",
        image_host="online.anyflip.com",
        custom_filename=custom_filename,
    )


# ==============================================================================
# FLIPHTML5 DOWNLOADER — EMBEDDED NODE.JS DECODER
# ==============================================================================
DECODER_JS_CONTENT = r"""
const nodeFS = require('fs');
const path = require('path');

function log(msg) { console.error(`[Decoder] ${msg}`); }

function localStringToUTF8(str, outPtr, maxBytesToWrite) {
    if (!(maxBytesToWrite > 0)) return 0;
    let startPtr = outPtr;
    let endPtr = startPtr + maxBytesToWrite - 1;
    for (let i = 0; i < str.length; ++i) {
        let u = str.charCodeAt(i);
        if (outPtr + 1 < endPtr) {
            Module.HEAPU8[outPtr++] = u;
        }
    }
    Module.HEAPU8[outPtr] = 0;
}

function localUTF8ToString(ptr) {
    if (!Module.HEAPU8) return "HEAPU8 not ready";
    let str = '';
    let idx = ptr;
    while (true) {
        let char = Module.HEAPU8[idx++];
        if (char === 0) break;
        str += String.fromCharCode(char);
    }
    return str;
}

global.Module = {
    wasmBinary: nodeFS.readFileSync(path.join(__dirname, 'deString.wasm')),
    onRuntimeInitialized: function () {
        ensurePolyfills();
        runLogic();
    },
    onAbort: function (what) { log("Aborted: " + what); },
    print: function (text) { log("Module stdout: " + text); },
    printErr: function (text) { log("Module stderr: " + text); }
};

function ensurePolyfills() {
    Module.stringToUTF8 = localStringToUTF8;
    Module.UTF8ToString = localUTF8ToString;
    if (!Module._malloc && Module.asm) Module._malloc = Module.asm._malloc || Module.asm.malloc;
    if (!Module._free && Module.asm) Module._free = Module.asm._free || Module.asm.free;
}

function runLogic() {
    try {
        const inputFile = process.argv[2];
        if (!inputFile || !nodeFS.existsSync(inputFile)) {
            log(`File not found: ${inputFile}`);
            process.exit(1);
        }

        let content = nodeFS.readFileSync(inputFile, 'utf8');
        global.htmlConfig = {};
        global.fliphtml5_pages = [];
        global.window = {};

        try { (0, eval)(content); } catch (e) { }

        if (!global.fliphtml5_pages || global.fliphtml5_pages.length === 0) {
            const pagesMatch = content.match(/var\s+fliphtml5_pages\s*=\s*(\[[\s\S]*?\]);/);
            if (pagesMatch) {
                try { global.fliphtml5_pages = JSON.parse(pagesMatch[1]); } 
                catch (e) { try { global.fliphtml5_pages = eval(pagesMatch[1]); } catch (e2) { } }
            }
        }

        if (!global.htmlConfig || Object.keys(global.htmlConfig).length === 0) {
            const configMatch = content.match(/var\s+htmlConfig\s*=\s*(\{[\s\S]*?\});/);
            if (configMatch) {
                try { global.htmlConfig = eval("(" + configMatch[1] + ")"); } catch (e) { }
            }
        }

        if (!global.htmlConfig || !global.htmlConfig.bookConfig) {
            const match3 = content.match(/bookConfig"\s*:\s*"([^"]+)"/);
            if (match3) {
                if (!global.htmlConfig) global.htmlConfig = {};
                global.htmlConfig.bookConfig = match3[1];
            }
        }

        let finalConfig = global.htmlConfig || {};

        if (finalConfig.bookConfig && typeof finalConfig.bookConfig === 'string') {
            const decodedStr = runDeString(finalConfig.bookConfig);
            try {
                finalConfig = { ...finalConfig, ...JSON.parse(decodedStr) };
            } catch (e) { }
        }

        if (global.fliphtml5_pages && global.fliphtml5_pages.length > 0) {
            finalConfig.fliphtml5_pages = global.fliphtml5_pages;
        }

        if (finalConfig.fliphtml5_pages && typeof finalConfig.fliphtml5_pages === 'string' && finalConfig.fliphtml5_pages.startsWith('v')) {
            try {
                const decodedPages = runDeString(finalConfig.fliphtml5_pages);
                try {
                    finalConfig.fliphtml5_pages = JSON.parse(decodedPages);
                } catch (e) {
                    const lastBrace = decodedPages.lastIndexOf(']');
                    if (lastBrace !== -1) {
                        finalConfig.fliphtml5_pages = JSON.parse(decodedPages.substring(0, lastBrace + 1));
                    }
                }
            } catch (e) { }
        }

        if (finalConfig.bookConfig) delete finalConfig.bookConfig;
        process.stdout.write(JSON.stringify(finalConfig), () => { process.exit(0); });

    } catch (e) {
        log("Error executing logic: " + e);
        process.exit(1);
    }
}

function runDeString(input) {
    const len = input.length * 4 + 1;
    if (!Module._malloc) {
        if (Module.asm && Module.asm.malloc) Module._malloc = Module.asm.malloc;
        else if (Module.asm && Module.asm._malloc) Module._malloc = Module.asm._malloc;
    }
    const ptr = Module._malloc(len);
    localStringToUTF8(input, ptr, len);
    const deStringFn = Module._DeString || (Module.asm ? Module.asm.DeString : null) || global._DeString;
    const resPtr = deStringFn(ptr);
    const result = localUTF8ToString(resPtr);
    if (Module._free) Module._free(ptr);
    return result;
}

let jsContent = nodeFS.readFileSync(path.join(__dirname, 'deString.js'), 'utf8');
const patchInitRegex = /Module\.onRuntimeInitialized\s*=\s*function\(\)\s*\{Module\.isReady\s*=\s*true;?\}/g;
if (patchInitRegex.test(jsContent)) jsContent = jsContent.replace(patchInitRegex, "/* patched out */");
else if (jsContent.includes("Module.onRuntimeInitialized = function() {Module.isReady = true;}")) jsContent = jsContent.replace("Module.onRuntimeInitialized = function() {Module.isReady = true;}", "/* patched out */");

let dataUriStart = jsContent.indexOf("data:application/octet-stream;base64,AGFzb");
if (dataUriStart !== -1) {
    let quoteChar = jsContent[dataUriStart - 1];
    let dataUriEnd = jsContent.indexOf(quoteChar, dataUriStart);
    if (dataUriEnd !== -1) {
        jsContent = jsContent.substring(0, dataUriStart - 1) + '"deString.wasm"' + jsContent.substring(dataUriEnd + 1);
    }
}

try { eval(jsContent); } catch (e) { }

setTimeout(() => {
    if (Module.asm) { ensurePolyfills(); runLogic(); } 
    else { process.exit(1); }
}, 1500);
"""

# ==============================================================================
# FLIPHTML5 / ANYFLIP PYTHON LOGIC
# ==============================================================================
async def auto_fetch_keys_and_config(book_url: str, output_dir: str, config_host: str, book_id: str):
    """Launch headless Chromium, visit the book, and capture:
       - deString.js / deString.wasm (for the decoder)
       - config.js (from whichever path the book uses)
       - cookies (including any cf_clearance) for later image downloads
    Using a real browser bypasses Cloudflare's bot challenge the same way a user would."""
    logger.info(f"🕵️ Launching headless browser: {book_url}")

    result = {
        "js": False,
        "wasm": False,
        "config_content": None,
        "cookies": {},
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        captured_config = {"text": None, "path": None}

        async def handle_response(response):
            url = response.url
            if "deString.js" in url and not result["js"]:
                try:
                    body = await response.body()
                    with open(os.path.join(output_dir, "deString.js"), "wb") as f:
                        f.write(body)
                    result["js"] = True
                except Exception:
                    pass
            elif "deString.wasm" in url and not result["wasm"]:
                try:
                    body = await response.body()
                    with open(os.path.join(output_dir, "deString.wasm"), "wb") as f:
                        f.write(body)
                    result["wasm"] = True
                except Exception:
                    pass
            # Capture config.js as the browser loads it naturally
            elif "config.js" in url and "htmlConfig" in (await _safe_text(response)):
                text = await _safe_text(response)
                if text and "htmlConfig" in text and captured_config["text"] is None:
                    captured_config["text"] = text
                    captured_config["path"] = url

        page.on("response", handle_response)

        try:
            await page.goto(book_url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            logger.info(f"  (page load note: {type(e).__name__})")

        # Poll until both keys are in hand, rather than guessing a fixed sleep.
        # This fixes the intermittent "Book 1 fails, Book 2 succeeds" issue where
        # deString.wasm hadn't finished streaming before the browser closed.
        max_wait_seconds = 20
        poll_interval = 0.5
        waited = 0.0
        while waited < max_wait_seconds:
            if result["js"] and result["wasm"]:
                break
            await asyncio.sleep(poll_interval)
            waited += poll_interval
        if not (result["js"] and result["wasm"]):
            logger.info(f"  (key capture incomplete after {max_wait_seconds}s: js={result['js']}, wasm={result['wasm']} — will attempt fallback)")

        # If the browser didn't naturally load the config (e.g. some viewers load it via XHR
        # that we missed, or the UA's flipbook is lazy), fetch it explicitly from within the
        # browser context — this still rides on the Cloudflare clearance the browser earned.
        if captured_config["text"] is None:
            candidate_paths = [
                "mobile/javascript/config.js",
                "javascript/config.js",
                "config.js",
            ]
            for suffix in candidate_paths:
                try:
                    cfg_url = f"https://{config_host}/{book_id}/{suffix}"
                    resp = await page.goto(cfg_url, wait_until="load", timeout=15000)
                    if resp and resp.status == 200:
                        text = await resp.text()
                        if "htmlConfig" in text:
                            captured_config["text"] = text
                            captured_config["path"] = cfg_url
                            logger.info(f"  Fetched config via browser: {suffix}")
                            break
                except Exception:
                    pass

        # Snapshot cookies for later httpx use
        try:
            cookies = await context.cookies()
            result["cookies"] = {c["name"]: c["value"] for c in cookies}
        except Exception:
            pass

        await browser.close()

        result["config_content"] = captured_config["text"]

        # Fallback: extract embedded base64 WASM from deString.js if separate .wasm wasn't served
        if result["js"] and not result["wasm"]:
            js_path = os.path.join(output_dir, "deString.js")
            if os.path.exists(js_path):
                try:
                    with open(js_path, "r", encoding="utf-8", errors="ignore") as f:
                        js_content = f.read()

                    match = re.search(r'data:application/(?:octet-stream|wasm);base64,([A-Za-z0-9+/=]+)', js_content)
                    if match:
                        wasm_bytes = base64.b64decode(match.group(1))
                        with open(os.path.join(output_dir, "deString.wasm"), "wb") as f:
                            f.write(wasm_bytes)
                        result["wasm"] = True
                except Exception:
                    pass

        return result


async def _safe_text(response):
    """Defensive helper — some responses throw on .text() (binary, redirects, etc)."""
    try:
        return await response.text()
    except Exception:
        return ""


async def download_image_with_fallback(client, url_candidates, path_template, semaphore, max_retries=3):
    """Download with fallback URLs, retries per URL, and a concurrency cap.
    Logs the final failure reason so we can see what's going wrong."""
    async with semaphore:
        last_error = None
        for url in url_candidates:
            for attempt in range(max_retries):
                try:
                    response = await client.get(url, timeout=30.0)
                    if response.status_code == 200 and len(response.content) > 1000:
                        ext = ".webp" if ".webp" in url.lower() else ".jpg"
                        final_path = path_template + ext
                        with open(final_path, 'wb') as f:
                            f.write(response.content)
                        logger.info(f"📥 Downloaded: {url}")
                        return final_path
                    elif response.status_code in (429, 503):
                        # Rate limited — back off exponentially
                        wait = 2 ** attempt
                        await asyncio.sleep(wait)
                        last_error = f"HTTP {response.status_code}"
                        continue
                    else:
                        last_error = f"HTTP {response.status_code} size={len(response.content)}"
                        break  # not worth retrying a 404 or 403 at the same URL
                except Exception as e:
                    last_error = f"{type(e).__name__}: {str(e)[:80]}"
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 + attempt)  # brief backoff before retry
        logger.warning(f"❌ All download attempts failed for {path_template} (last error: {last_error})")
        return None


async def run_wasm_decoder_path(
    platform: str,
    book_id: str,
    landing_url: str,
    config_host: str,
    image_host: str,
    custom_filename: str = "",
):
    """Shared WASM decoder path. Works for both FlipHTML5 and DRM-protected AnyFlip
    because both platforms use the same underlying deString.js/.wasm obfuscation.

    Args:
        platform: 'fliphtml5' or 'anyflip' (used for log labels + AnyFlip config path).
        book_id: e.g. 'cmjgl/jbyl' (two-segment slug).
        landing_url: the page Playwright should visit to intercept the WASM keys.
        config_host: host serving the config.js (e.g. 'online.anyflip.com').
        image_host: host serving the page images.
        custom_filename: optional user-supplied filename.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        decoder_script = os.path.join(temp_dir, "flipbook_decoder.js")
        with open(decoder_script, "w", encoding="utf-8") as f:
            f.write(DECODER_JS_CONTENT)

        # Single browser session captures keys, config, and cookies
        capture = await auto_fetch_keys_and_config(
            book_url=landing_url,
            output_dir=temp_dir,
            config_host=config_host,
            book_id=book_id,
        )

        config_content = capture["config_content"]
        browser_cookies = capture["cookies"]

        # Fallback: if the browser didn't capture the config, try httpx with Cloudflare cookies
        # (usually unnecessary but a belt-and-braces safety net).
        if not config_content:
            logger.info("Config not captured via browser — retrying with httpx + browser cookies...")
            browser_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": landing_url,
            }
            if platform == "anyflip":
                possible_paths = ["mobile/javascript/config.js", "javascript/config.js", "config.js"]
            else:
                possible_paths = ["config.js", "javascript/config.js"]

            async with httpx.AsyncClient(follow_redirects=True, headers=browser_headers, cookies=browser_cookies) as client:
                for suffix in possible_paths:
                    url = f"https://{config_host}/{book_id}/{suffix}"
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200 and "htmlConfig" in resp.text:
                            config_content = resp.text
                            logger.info(f"  Loaded config via httpx fallback: {suffix}")
                            break
                        else:
                            logger.info(f"  tried {suffix}: status={resp.status_code} size={len(resp.text)}")
                    except Exception as e:
                        logger.info(f"  tried {suffix}: {type(e).__name__}")

        if not config_content:
            logger.error("Could not find configuration file. Skipping.")
            return

        config_path = os.path.join(temp_dir, "config.js")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_content)

        logger.info("Running embedded WASM decoder...")
        try:
            result = subprocess.run(
                ["node", str(decoder_script), config_path],
                capture_output=True, text=True, check=True
            )
            raw_json = result.stdout.strip()
            start = raw_json.find('{')
            end = raw_json.rfind('}')
            if start != -1 and end != -1:
                raw_json = raw_json[start:end + 1]
            book_data = json.loads(raw_json)
        except Exception as e:
            logger.error(f"Decoding failed: {e}")
            if hasattr(e, 'stderr'):
                logger.error(e.stderr)
            return

        # Determine filename
        if custom_filename:
            output_path = get_safe_filename(custom_filename)
        else:
            raw_title = (
                book_data.get('bookTitle')
                or book_data.get('title')
                or book_data.get('name')
                or book_id.replace("/", "_")
            )
            output_path = get_safe_filename(raw_title)

        logger.info(f"Determined Output Filename: {output_path}")

        pages = book_data.get('fliphtml5_pages') or book_data.get('pages')
        if not pages:
            for v in book_data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and 'n' in v[0]:
                    pages = v
                    break

        if not pages:
            logger.error("No pages found in configuration. Skipping.")
            return

        logger.info(f"Found {len(pages)} pages.")

        tasks = []
        image_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://{config_host}/{book_id}/",
        }
        # Throttle: cap concurrent downloads, prevent connection-pool exhaustion, and
        # give the CDN breathing room — this is the fix for the "32 pages failed" issue.
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
        semaphore = asyncio.Semaphore(8)
        async with httpx.AsyncClient(follow_redirects=True, headers=image_headers, cookies=browser_cookies, limits=limits, timeout=30.0) as client:
            for i, page in enumerate(pages):
                page_num = i + 1
                # Prefer 'n' (canonical filename) over 'l' (sometimes a thumbnail variant).
                suffix = page.get('n', page.get('l'))

                if isinstance(suffix, list):
                    suffix = suffix[0] if suffix else None
                if not suffix:
                    continue

                clean_suffix = suffix.lstrip('./').strip()

                url_candidates = []
                if clean_suffix.startswith('http'):
                    url_candidates.append(clean_suffix)
                elif clean_suffix.startswith('files/'):
                    url_candidates.append(f"https://{image_host}/{book_id}/{clean_suffix}")
                else:
                    url_candidates.append(f"https://{image_host}/{book_id}/files/large/{clean_suffix}")

                # Fallbacks — try common path shapes
                url_candidates.extend([
                    f"https://{image_host}/{book_id}/files/large/{page_num}.jpg",
                    f"https://{image_host}/{book_id}/files/large/{page_num}.webp",
                    f"https://{image_host}/{book_id}/files/page/{page_num}.jpg",
                    f"https://{image_host}/{book_id}/files/page/{page_num}.webp",
                    f"https://{image_host}/{book_id}/files/mobile/{page_num}.webp",
                    f"https://{image_host}/{book_id}/files/mobile/{page_num}.jpg",
                ])

                seen = set()
                deduped = []
                for u in url_candidates:
                    u = u.replace("/./", "/")
                    if u not in seen:
                        seen.add(u)
                        deduped.append(u)

                fpath_template = os.path.join(temp_dir, f"{page_num:04d}")
                tasks.append(download_image_with_fallback(client, deduped, fpath_template, semaphore))

            downloaded_files = []
            with tqdm.tqdm(total=len(tasks), desc="📥 Downloading Pages", unit="page", leave=True) as pbar:
                for coro in asyncio.as_completed(tasks):
                    result = await coro
                    downloaded_files.append(result)
                    pbar.update(1)

        valid_files = sorted([f for f in downloaded_files if f])
        if not valid_files:
            logger.error("No images downloaded.")
            return

        # Discard any corrupt/truncated files before conversion
        verified_files = [f for f in valid_files if _validate_image(f)]
        if not verified_files:
            logger.error("No valid images to build PDF.")
            return
        if len(verified_files) < len(valid_files):
            logger.warning(f"   Discarded {len(valid_files) - len(verified_files)} corrupt page(s).")

        logger.info(f"Converting {len(verified_files)} images to PDF...")
        final_images = []
        # img2pdf can't read webp directly, so convert any webp pages to PNG first
        for img in tqdm.tqdm(verified_files, desc="🖨️  Preparing pages", unit="page", leave=True):
            if img.lower().endswith('.webp'):
                try:
                    png = img.replace('.webp', '.png')
                    with Image.open(img) as im:
                        im.save(png, 'PNG')
                    final_images.append(png)
                except Exception:
                    final_images.append(img)
            else:
                final_images.append(img)

        with open(output_path, "wb") as f:
            f.write(img2pdf.convert(final_images))

        logger.info(f"🎉 PDF Successfully Saved: {output_path}")


async def run_fliphtml5(book_url: str, custom_filename: str = ""):
    """Thin wrapper: extract the book_id from a FlipHTML5 URL and run the shared decoder path."""
    clean_url = book_url.split("#")[0].split("?")[0]

    book_id = clean_url
    if "online.fliphtml5.com/" in book_id:
        book_id = book_id.split("online.fliphtml5.com/")[-1].strip("/")
    elif "fliphtml5.com/" in book_id:
        book_id = book_id.split("fliphtml5.com/")[-1].strip("/")
    book_id = book_id.strip("/")
    logger.info(f"Target Book ID: {book_id}")

    await run_wasm_decoder_path(
        platform="fliphtml5",
        book_id=book_id,
        landing_url=clean_url,
        config_host="online.fliphtml5.com",
        image_host="online.fliphtml5.com",
        custom_filename=custom_filename,
    )


# ==============================================================================
# DISPATCHER
# ==============================================================================
async def process_queue_item(item):
    url = item["url"]
    custom_name = item["filename"]
    site = detect_site(url)

    if site == "anyflip":
        logger.info(f"🟢 Detected: AnyFlip")
        await run_anyflip(url, custom_name)
    elif site == "fliphtml5":
        logger.info(f"🟢 Detected: FlipHTML5")
        await run_fliphtml5(url, custom_name)
    else:
        logger.error(f"❌ Unrecognized URL (not anyflip.com or fliphtml5.com): {url}")


async def main():
    print("========================================")
    print("   📖 FlipBook Batch Downloader 📖")
    print("   Supports: AnyFlip + FlipHTML5")
    print("========================================")

    queue = []

    while True:
        url_input = input("\n🔗 Enter URL (or type 'done' to start): ").strip()

        if url_input.lower() in ['done', 'd']:
            break
        if not url_input:
            continue

        site = detect_site(url_input)
        if site == "unknown":
            print("⚠️  URL doesn't look like AnyFlip or FlipHTML5. Skipping.")
            continue
        print(f"   → Detected: {site}")

        name_input = input("📄 Enter custom filename (Leave blank for Auto-title): ").strip()

        queue.append({
            "url": url_input,
            "filename": name_input if name_input.lower() != 'auto' else ""
        })
        print(f"✅ Added to queue. (Total: {len(queue)})")

    if not queue:
        print("No URLs entered. Exiting.")
        return

    print("\n🚀 Starting Download Queue...")
    for index, item in enumerate(queue, 1):
        print(f"\n--- Processing Book {index}/{len(queue)} ---")
        try:
            await process_queue_item(item)
        except Exception as e:
            logger.error(f"Critical failure on {item['url']}: {e}")

    print("\n========================================")
    print("          ✅ All Done. Enjoy! ✅")
    print("========================================")


if __name__ == "__main__":
    asyncio.run(main())
