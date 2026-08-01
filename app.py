from __future__ import annotations

import ctypes
import json
import platform
import queue
import re
import secrets
import statistics
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from PIL import Image, ImageChops, ImageGrab, ImageStat, ImageTk

APP_NAME = "Box Flip Automator"
APP_VERSION = "1.9.0 Mac — Zoom Fix"
CONFIG_PATH = Path.home() / ".box_flip_automator_v19_mac.json"
LEGACY_CONFIG_PATH = Path.home() / ".box_flip_automator_v19.json"
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

if IS_WINDOWS:
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
else:
    wintypes = None

if IS_MAC:
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except Exception:
        Quartz = None
        Vision = None
        NSURL = None
else:
    Quartz = None
    Vision = None
    NSURL = None


@dataclass
class ScreenBounds:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass
class BoxTarget:
    center_x: int
    center_y: int
    rect: tuple[int, int, int, int] | None = None


@dataclass
class BoxPair:
    top: BoxTarget
    bottom: BoxTarget


class Theme:
    # Sleek red/gold "good fortune" palette. UI-only change; automation logic is unchanged.
    BG = "#09090B"
    PANEL = "#121014"
    PANEL_2 = "#181318"
    PANEL_3 = "#211419"
    TEXT = "#F7F2EA"
    MUTED = "#A79BA1"
    RED = "#D62435"
    RED_BRIGHT = "#F04455"
    RED_DARK = "#7B1320"
    GOLD = "#E7B85B"
    GOLD_SOFT = "#F4D18A"
    GREEN = "#39C878"
    BLUE = "#55A6FF"
    BORDER = "#3A252C"
    BORDER_HOT = "#7A2836"
    INPUT = "#0D0C0F"


REGION_COLORS = {
    "boxes": "#2d8cff",
    "wager": "#35bd72",
    "done": "#b05cff",
}


def mac_screen_capture_allowed() -> bool:
    if not IS_MAC or Quartz is None:
        return True
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def request_mac_screen_capture() -> bool:
    if not IS_MAC or Quartz is None:
        return True
    try:
        if Quartz.CGPreflightScreenCaptureAccess():
            return True
        Quartz.CGRequestScreenCaptureAccess()
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def mac_accessibility_allowed(prompt: bool = False) -> bool:
    if not IS_MAC or Quartz is None:
        return True
    try:
        if prompt and hasattr(Quartz, "AXIsProcessTrustedWithOptions"):
            option = getattr(Quartz, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
            return bool(Quartz.AXIsProcessTrustedWithOptions({option: True}))
        if hasattr(Quartz, "AXIsProcessTrusted"):
            return bool(Quartz.AXIsProcessTrusted())
    except Exception:
        return False
    return False


def _mac_logical_screen_bounds() -> ScreenBounds | None:
    """Return the Mac display size in Quartz logical points, not Retina pixels.

    The original v1.9 Mac build works correctly for permissions and automation on
    the target Mac, but its Set overlay can receive a Retina screenshot several
    times larger than the logical desktop.  We leave the permission/automation
    path untouched and only use Quartz's display geometry to shrink that picture
    back to the visible desktop size.
    """
    if not IS_MAC or Quartz is None:
        return None
    try:
        display_id = Quartz.CGMainDisplayID()
        rect = Quartz.CGDisplayBounds(display_id)
        width = int(round(float(rect.size.width)))
        height = int(round(float(rect.size.height)))
        if width > 0 and height > 0:
            return ScreenBounds(0, 0, width, height)
    except Exception:
        pass
    return None


def _fit_mac_capture_to_logical_screen(image: Image.Image, bounds: ScreenBounds) -> Image.Image:
    """Zoom a Retina capture OUT to the Mac's logical desktop dimensions.

    Example: if macOS hands us a 5x capture, this displays it at 20% size.
    This is intentionally a display/selection correction only; it does not alter
    the original permission checks, mouse automation, wager flow, or game logic.
    """
    if image.width == bounds.width and image.height == bounds.height:
        return image
    if bounds.width <= 0 or bounds.height <= 0:
        return image
    return image.resize((bounds.width, bounds.height), Image.Resampling.LANCZOS)


def get_virtual_screen_bounds() -> ScreenBounds:
    if IS_WINDOWS:
        user32 = ctypes.windll.user32
        return ScreenBounds(
            user32.GetSystemMetrics(76),
            user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78),
            user32.GetSystemMetrics(79),
        )
    if IS_MAC:
        # IMPORTANT: use Quartz logical POINTS here rather than screenshot pixels.
        # This is the only behavioral change from the original working v1.9 Mac.
        logical = _mac_logical_screen_bounds()
        if logical is not None:
            return logical
        image = ImageGrab.grab(scale_down=True)
        return ScreenBounds(0, 0, image.width, image.height)
    image = ImageGrab.grab()
    return ScreenBounds(0, 0, image.width, image.height)


def capture_virtual_screen(bounds: ScreenBounds | None = None) -> tuple[Image.Image, ScreenBounds]:
    bounds = bounds or get_virtual_screen_bounds()
    if IS_WINDOWS:
        image = ImageGrab.grab(
            bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom),
            all_screens=True,
        )
    elif IS_MAC:
        # Let macOS/Pillow capture however large it wants (Retina included), then
        # zoom the picture OUT to the logical desktop.  On the reported Mac this
        # should counter the ~5x zoom automatically rather than hard-coding 20%.
        image = ImageGrab.grab(scale_down=True)
        image = _fit_mac_capture_to_logical_screen(image, bounds)
    else:
        image = ImageGrab.grab()
        bounds = ScreenBounds(0, 0, image.width, image.height)
    return image.convert("RGB"), bounds


def crop_global(image: Image.Image, bounds: ScreenBounds, rect: tuple[int, int, int, int]) -> Image.Image:
    x1, y1, x2, y2 = rect
    return image.crop((x1 - bounds.left, y1 - bounds.top, x2 - bounds.left, y2 - bounds.top))


def capture_region(rect: tuple[int, int, int, int]) -> Image.Image:
    if IS_MAC:
        # Direct bbox capture is substantially faster during automation and, with
        # scale_down=True, stays in the same logical coordinate space as Tk/Quartz.
        return ImageGrab.grab(bbox=rect, scale_down=True).convert("RGB")
    image, bounds = capture_virtual_screen()
    return crop_global(image, bounds, rect)


def secure_sequence(count: int) -> list[int]:
    return [secrets.randbelow(2) + 1 for _ in range(count)]


def american_implied_probability(odds: int) -> float:
    """Convert American odds into an implied probability in the 0..1 range."""
    if odds == 0:
        raise ValueError("American odds cannot be zero.")
    if odds < 0:
        magnitude = abs(odds)
        return magnitude / (magnitude + 100.0)
    return 100.0 / (odds + 100.0)


def capped_pair_probability(top_odds: int, bottom_odds: int, max_favorite: float = 0.75) -> float:
    """Return P(top wins), normalized for the pair and capped to preserve randomness."""
    max_favorite = min(0.95, max(0.50, max_favorite))
    top_raw = american_implied_probability(top_odds)
    bottom_raw = american_implied_probability(bottom_odds)
    total = top_raw + bottom_raw
    if total <= 0:
        return 0.5
    top_probability = top_raw / total
    if top_probability >= 0.5:
        return min(top_probability, max_favorite)
    return max(top_probability, 1.0 - max_favorite)



def mlb_adjusted_top_probability(top_odds: int, bottom_odds: int, max_favorite: float = 0.75) -> float:
    """MLB 2021-2025 calibration: blend market probability with the 42.9% underdog baseline.

    The historical prior is strongest in near-even games and deliberately fades as the
    market becomes more lopsided. This keeps the market line primary while incorporating
    the five-season MLB upset-rate finding. The final probability still respects the
    app's favorite cap so randomness remains.
    """
    top_raw = american_implied_probability(top_odds)
    bottom_raw = american_implied_probability(bottom_odds)
    total = top_raw + bottom_raw
    if total <= 0:
        return 0.5
    market_top = top_raw / total
    market_dog = min(market_top, 1.0 - market_top)
    market_fav = 1.0 - market_dog

    # Research baseline: MLB underdogs won about 42.9% from 2021-2025.
    dog_prior = 0.429

    # Let the five-year prior influence close games more than heavy-favorite games.
    # Near 50/50: 30% prior / 70% market. Around 75/25: 10% prior / 90% market.
    strength = min(1.0, max(0.0, (market_fav - 0.50) / 0.25))
    prior_weight = 0.30 - (0.20 * strength)
    calibrated_dog = (1.0 - prior_weight) * market_dog + prior_weight * dog_prior
    calibrated_dog = min(0.50, max(0.05, calibrated_dog))

    top_is_dog = market_top < 0.5
    top_probability = calibrated_dog if top_is_dog else 1.0 - calibrated_dog

    max_favorite = min(0.75, max(0.50, max_favorite))
    return min(max(top_probability, 1.0 - max_favorite), max_favorite)


def weighted_sequence(top_probabilities: list[float]) -> list[int]:
    results: list[int] = []
    scale = 1_000_000
    for probability in top_probabilities:
        threshold = round(min(1.0, max(0.0, probability)) * scale)
        results.append(1 if secrets.randbelow(scale) < threshold else 2)
    return results


def _colored_text_mask(image: Image.Image) -> Image.Image:
    """Site-tuned mask for the small saturated odds text inside the captured rows."""
    rgb = image.convert("RGB")
    pixels = rgb.load()
    mask = Image.new("L", rgb.size, 0)
    out = mask.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = pixels[x, y]
            high = max(r, g, b)
            low = min(r, g, b)
            if high - low >= 12 and low <= 246:
                out[x, y] = 255
    return mask


def _box_inner_crop(region_image: Image.Image, target: BoxTarget) -> Image.Image | None:
    if not target.rect:
        return None
    x1, y1, x2, y2 = target.rect
    width = max(1, x2 - x1)
    left = x1 + round(width * 0.12)
    right = x2 - round(width * 0.12)
    top = min(y2, y1 + 3)
    bottom = max(top + 1, y2 - 2)
    return region_image.crop((left, top, right, bottom))


def _fallback_estimate_odds(box_image: Image.Image) -> tuple[int | None, str]:
    """Conservative fallback tuned to the supplied tiny odds font."""
    mask = _colored_text_mask(box_image)
    bbox = mask.getbbox()
    if not bbox:
        return None, "unreadable"
    cropped = mask.crop(bbox)
    width = cropped.width
    probe_width = min(3, cropped.width)
    probe = cropped.crop((0, 0, probe_width, cropped.height))
    col_counts = [sum(1 for y in range(probe.height) if probe.getpixel((x, y)) > 0) for x in range(probe.width)]
    sign = 1 if col_counts and max(col_counts) >= 4 else -1
    if width >= 21:
        magnitude = 1200
        confidence = "tier-heavy"
    else:
        if sign < 0:
            if width <= 16:
                magnitude = 140
            elif width <= 18:
                magnitude = 300
            else:
                magnitude = 500
        else:
            if width <= 18:
                magnitude = 130
            elif width <= 20:
                magnitude = 300
            else:
                magnitude = 500
        confidence = "tier-estimate"
    return sign * magnitude, confidence


def build_windows_ocr_sheet(region_image: Image.Image, pairs: list[BoxPair]) -> tuple[Image.Image, list[Image.Image]]:
    """Build a high-contrast enlarged sheet so Windows OCR sees one odds value per line."""
    boxes: list[Image.Image] = []
    for pair in pairs:
        for target in (pair.top, pair.bottom):
            crop = _box_inner_crop(region_image, target)
            if crop is None:
                continue
            mask = _colored_text_mask(crop)
            bbox = mask.getbbox()
            if bbox:
                mask = mask.crop(bbox)
            line = Image.new("L", (max(1, mask.width + 10), max(1, mask.height + 8)), 255)
            ink = Image.new("L", mask.size, 255)
            ink.paste(0, mask=mask)
            line.paste(ink, (5, 4))
            boxes.append(line.convert("RGB"))
    scale = 8
    line_height = 72
    sheet_width = 360
    sheet = Image.new("RGB", (sheet_width, max(line_height, line_height * len(boxes))), "white")
    for index, line in enumerate(boxes):
        enlarged = line.resize((line.width * scale, line.height * scale), Image.Resampling.NEAREST)
        x = max(8, (sheet_width - enlarged.width) // 2)
        y = index * line_height + max(2, (line_height - enlarged.height) // 2)
        sheet.paste(enlarged, (x, y))
    return sheet, boxes


def _run_windows_ocr(image: Image.Image, timeout: float = 4.0) -> list[str]:
    """Use the OCR engine built into Windows 10/11; no Tesseract install is required."""
    if not IS_WINDOWS:
        return []
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="boxflip_odds_", suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        image.save(temp_path)
        escaped = str(temp_path).replace("'", "''")
        script = rf'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
function Await-WinRT($Operation, [Type]$ResultType) {{
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
    }} | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}}
$path = '{escaped}'
$file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
$decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {{ exit 2 }}
$result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$result.Lines | ForEach-Object {{ $_.Text }}
'''
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        if completed.returncode != 0:
            return []
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except Exception:
        return []
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _run_mac_ocr(image: Image.Image, timeout: float = 4.0) -> list[str]:
    """Use Apple's built-in Vision text recognizer; no Tesseract install is required."""
    if not IS_MAC or Vision is None or NSURL is None:
        return []
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="boxflip_odds_", suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        image.save(temp_path)
        url = NSURL.fileURLWithPath_(str(temp_path))
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)
        try:
            request.setRecognitionLanguages_(["en-US"])
        except Exception:
            pass
        result = handler.performRequests_error_([request], None)
        if isinstance(result, tuple) and result and not bool(result[0]):
            return []
        observations = request.results() or []
        lines: list[str] = []
        for observation in observations:
            candidates = observation.topCandidates_(1)
            if candidates:
                text = str(candidates[0].string()).strip()
                if text:
                    lines.append(text)
        return lines
    except Exception:
        return []
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _parse_ocr_odds(lines: list[str], expected: int) -> list[int] | None:
    values: list[int] = []
    for line in lines:
        cleaned = line.replace("—", "-").replace("–", "-").replace("−", "-").replace(" ", "")
        matches = re.findall(r"[+-]?\d{3,5}", cleaned)
        for token in matches:
            try:
                value = int(token)
            except ValueError:
                continue
            if 100 <= abs(value) <= 20000:
                values.append(value)
    if len(values) != expected:
        return None
    return values


def read_odds_for_pairs(region_image: Image.Image, pairs: list[BoxPair]) -> tuple[list[tuple[int, int]], str]:
    expected = len(pairs) * 2
    if expected == 0:
        return [], "no-pairs"

    fallback_values: list[int | None] = []
    fallback_tags: list[str] = []
    for pair in pairs:
        for target in (pair.top, pair.bottom):
            crop = _box_inner_crop(region_image, target)
            if crop is None:
                fallback_values.append(None)
                fallback_tags.append("unreadable")
                continue
            value, tag = _fallback_estimate_odds(crop)
            fallback_values.append(value)
            fallback_tags.append(tag)

    sheet, _ = build_windows_ocr_sheet(region_image, pairs)
    if IS_MAC:
        parsed = _parse_ocr_odds(_run_mac_ocr(sheet), expected)
        source = "Apple Vision OCR"
    else:
        parsed = _parse_ocr_odds(_run_windows_ocr(sheet), expected)
        source = "Windows OCR"
    if parsed is not None:
        # OCR sometimes drops a tiny + or - sign. Reconcile the sign with the
        # site-tuned glyph detector while preserving OCR's numeric magnitude.
        for index, fallback in enumerate(fallback_values):
            if fallback is not None:
                parsed[index] = abs(parsed[index]) if fallback > 0 else -abs(parsed[index])
    else:
        parsed = [value if value is not None else 100 for value in fallback_values]
        source = "site-tuned fallback" if all(tag != "unreadable" for tag in fallback_tags) else "partial fallback"

    pair_values = [(parsed[i], parsed[i + 1]) for i in range(0, len(parsed), 2)]
    return pair_values, source


def odds_weighted_sequence(region_image: Image.Image, pairs: list[BoxPair], max_favorite: float, mlb_algo: bool = False) -> tuple[list[int], list[tuple[int, int, float]], str]:
    pair_odds, source = read_odds_for_pairs(region_image, pairs)
    if len(pair_odds) != len(pairs):
        sequence = secure_sequence(len(pairs))
        return sequence, [], "50/50 fallback"
    details: list[tuple[int, int, float]] = []
    probabilities: list[float] = []
    for top_odds, bottom_odds in pair_odds:
        try:
            probability = (
                mlb_adjusted_top_probability(top_odds, bottom_odds, max_favorite)
                if mlb_algo else
                capped_pair_probability(top_odds, bottom_odds, max_favorite)
            )
        except Exception:
            probability = 0.5
        probabilities.append(probability)
        details.append((top_odds, bottom_odds, probability))
    if mlb_algo:
        source = f"MLB 5yr + {source}"
    return weighted_sequence(probabilities), details, source


def merge_positions(values: list[int], max_gap: int = 1) -> list[list[int]]:
    groups: list[list[int]] = []
    for value in values:
        if not groups or value > groups[-1][-1] + max_gap:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def detect_colored_horizontal_lines(image: Image.Image) -> list[int]:
    """Detect vivid horizontal borders used by the supplied top/bottom boxes."""
    work = image.convert("HSV")
    if work.width > 700:
        work = work.resize((700, work.height), Image.Resampling.BILINEAR)

    px = work.load()
    step = max(1, work.width // 350)
    sample_x = list(range(0, work.width, step))
    line_rows: list[int] = []

    for y in range(work.height):
        saturated = 0
        for x in sample_x:
            _h, s, v = px[x, y]
            if s >= 62 and v >= 35:
                saturated += 1
        if saturated / max(1, len(sample_x)) >= 0.48:
            line_rows.append(y)

    clusters = merge_positions(line_rows, max_gap=2)
    return [round(sum(group) / len(group)) for group in clusters if len(group) <= 8]


def choose_line_pairing_offset(lines: list[int]) -> int:
    best_offset = 0
    best_score = float("-inf")
    for offset in (0, 1):
        heights = [lines[i + 1] - lines[i] for i in range(offset, len(lines) - 1, 2)]
        if not heights:
            continue
        valid = [h for h in heights if 8 <= h <= 140]
        if not valid:
            score = -1000.0
        else:
            median = statistics.median(valid)
            tolerance = max(5.0, median * 0.38)
            consistent = sum(1 for h in heights if 8 <= h <= 140 and abs(h - median) <= tolerance)
            mad = statistics.median(abs(h - median) for h in valid)
            score = consistent * 20 - mad - offset * 0.1
        if score > best_score:
            best_score = score
            best_offset = offset
    return best_offset


def detect_box_pairs(image: Image.Image, global_x: int, global_y: int) -> tuple[list[BoxPair], str]:
    lines = detect_colored_horizontal_lines(image)
    if len(lines) < 4:
        return [], "Not enough colored borders found. Use Manual Points."

    offset = choose_line_pairing_offset(lines)
    boxes: list[BoxTarget] = []
    heights: list[int] = []
    margin_x = max(2, round(image.width * 0.03))

    for i in range(offset, len(lines) - 1, 2):
        top_y = lines[i]
        bottom_y = lines[i + 1]
        height = bottom_y - top_y
        if 8 <= height <= 140:
            heights.append(height)
            boxes.append(
                BoxTarget(
                    center_x=global_x + image.width // 2,
                    center_y=global_y + round((top_y + bottom_y) / 2),
                    rect=(margin_x, top_y, image.width - margin_x, bottom_y),
                )
            )

    if len(boxes) < 2:
        return [], "Borders were found but could not be paired. Use Manual Points."
    if len(boxes) % 2:
        boxes = boxes[:-1]

    pairs = [BoxPair(boxes[i], boxes[i + 1]) for i in range(0, len(boxes), 2)]
    median_height = round(statistics.median(heights)) if heights else 0
    return pairs, f"{len(pairs)} sets detected ({median_height}px box height)."


def three_column_slices(width: int) -> list[tuple[int, int]]:
    """Return three left-to-right slices for a highlighted 3-column board."""
    if width < 90:
        return []
    # Use exact thirds. The supplied board has small gutters; those naturally
    # fall near the boundaries and do not affect the colored-border detector.
    cuts = [0, round(width / 3), round(2 * width / 3), width]
    return [(cuts[i], cuts[i + 1]) for i in range(3)]


def detect_three_columns(
    image: Image.Image, global_x: int, global_y: int
) -> tuple[list[list[BoxPair]], list[tuple[int, int]], str]:
    """Detect the same top/bottom rows independently in left/middle/right columns."""
    slices = three_column_slices(image.width)
    if len(slices) != 3:
        return [], [], "Region 1 is too narrow for a three-column mode."
    columns: list[list[BoxPair]] = []
    statuses: list[str] = []
    for index, (x1, x2) in enumerate(slices):
        crop = image.crop((x1, 0, x2, image.height))
        pairs, status = detect_box_pairs(crop, global_x + x1, global_y)
        columns.append(pairs)
        statuses.append(status)
    counts = [len(items) for items in columns]
    if any(count == 0 for count in counts):
        return columns, slices, f"Three-column scan incomplete: L/M/R detected {counts[0]}/{counts[1]}/{counts[2]} sets."
    return columns, slices, f"Three-column scan ready: L/M/R detected {counts[0]}/{counts[1]}/{counts[2]} sets."


def difference_score(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        b = b.resize(a.size, Image.Resampling.BILINEAR)
    max_width = 260
    if a.width > max_width:
        ratio = max_width / a.width
        size = (max_width, max(1, round(a.height * ratio)))
        a = a.resize(size, Image.Resampling.BILINEAR)
        b = b.resize(size, Image.Resampling.BILINEAR)
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    return ImageStat.Stat(diff).mean[0] / 255.0


def hsv_mask(image: Image.Image, hue_min: int, hue_max: int, sat_min: int, val_min: int) -> Image.Image:
    h, s, v = image.convert("HSV").split()
    h_lut = [255 if hue_min <= value <= hue_max else 0 for value in range(256)]
    s_lut = [255 if value >= sat_min else 0 for value in range(256)]
    v_lut = [255 if value >= val_min else 0 for value in range(256)]
    return ImageChops.multiply(ImageChops.multiply(h.point(h_lut), s.point(s_lut)), v.point(v_lut))


def find_solid_color_rect(image: Image.Image, color: str) -> tuple[int, int, int, int] | None:
    if color == "green":
        mask = hsv_mask(image, 52, 112, 70, 40)
        min_w, min_h, min_density = 40, 6, 0.30
    elif color == "blue":
        mask = hsv_mask(image, 125, 180, 85, 65)
        min_w, min_h, min_density = 45, 12, 0.30
    else:
        raise ValueError("Unsupported color")

    bbox = mask.getbbox()
    if not bbox:
        return None
    x1, y1, x2, y2 = bbox
    if x2 - x1 < min_w or y2 - y1 < min_h:
        return None
    density = ImageStat.Stat(mask.crop(bbox)).mean[0] / 255.0
    if density < min_density:
        return None
    return bbox




def _neutral_border_pixel(pixel: tuple[int, int, int]) -> bool:
    """True for the light neutral gray typically used around input controls."""
    r, g, b = pixel
    spread = max(r, g, b) - min(r, g, b)
    avg = (r + g + b) / 3.0
    return spread <= 20 and 175 <= avg <= 242


def _longest_true_run(values: list[bool]) -> tuple[int, int] | None:
    best_start = best_end = -1
    start = -1
    for index, value in enumerate(values + [False]):
        if value and start < 0:
            start = index
        elif not value and start >= 0:
            if index - start > best_end - best_start:
                best_start, best_end = start, index
            start = -1
    if best_start < 0:
        return None
    return best_start, best_end


def find_wager_input_rect(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Find the left WAGER input box inside Region 2.

    Region 2 normally includes both the top wager panel and the lower green
    action button.  Older builds clicked the horizontal center of the whole
    region, which can land on the TO WIN box or the divider.  This detector
    looks for the long neutral-gray top/bottom borders of the left input.
    """
    image = image.convert("RGB")
    width, height = image.size
    if width < 40 or height < 25:
        return None

    green = find_solid_color_rect(image, "green")
    green_top = green[1] if green else height
    search_bottom = min(green_top, max(20, round(height * 0.62)))
    search_width = max(30, round(width * 0.68))
    min_run = max(28, round(width * 0.22))

    row_candidates: list[tuple[int, int, int]] = []
    pixels = image.load()
    for y in range(search_bottom):
        run = _longest_true_run([_neutral_border_pixel(pixels[x, y]) for x in range(search_width)])
        if run and run[1] - run[0] >= min_run:
            row_candidates.append((y, run[0], run[1]))

    # Collapse adjacent scan lines into one representative line.
    groups: list[list[tuple[int, int, int]]] = []
    for item in row_candidates:
        if not groups or item[0] > groups[-1][-1][0] + 1:
            groups.append([item])
        else:
            groups[-1].append(item)
    reps: list[tuple[int, int, int]] = []
    for group in groups:
        reps.append(max(group, key=lambda item: item[2] - item[1]))

    best: tuple[float, tuple[int, int, int, int]] | None = None
    for i, top in enumerate(reps):
        for bottom in reps[i + 1:]:
            box_h = bottom[0] - top[0]
            if not (18 <= box_h <= max(80, round(height * 0.45))):
                continue
            overlap_left = max(top[1], bottom[1])
            overlap_right = min(top[2], bottom[2])
            overlap = overlap_right - overlap_left
            if overlap < min_run:
                continue
            # WAGER is the leftmost upper input. Penalize candidates farther
            # right and unusually tall boxes/dividers.
            candidate_w = overlap
            # In the supplied layout WAGER is the left of two side-by-side
            # fields, so its border is about half of Region 2. Full-width
            # separators (Round Robin / movement rows) must not win.
            if candidate_w > width * 0.62 or candidate_w < width * 0.25:
                continue
            target_h = min(38, max(24, round(height * 0.23)))
            target_w = width * 0.48
            score = (
                overlap
                - abs(candidate_w - target_w) * 2.0
                - abs(box_h - target_h) * 1.2
                - overlap_left * 0.35
                - top[0] * 0.30
            )
            rect = (overlap_left, top[0], overlap_right, bottom[0] + 1)
            if best is None or score > best[0]:
                best = (score, rect)

    if best is None:
        return None

    x1, y1, x2, y2 = best[1]
    # Reject candidates that are implausibly centered/right-sided.
    if x1 > width * 0.35 or x2 - x1 < min_run:
        return None
    return x1, y1, x2, y2


def wager_click_point(image: Image.Image) -> tuple[int, int, str]:
    """Return a reliable local click point for the WAGER entry field."""
    rect = find_wager_input_rect(image)
    if rect:
        x1, y1, x2, y2 = rect
        return round((x1 + x2) / 2), round((y1 + y2) / 2), "smart field detector"

    # Safe fallback for the supplied layout: WAGER occupies the upper-left
    # quarter of Region 2, while TO WIN is on the upper-right.
    return max(6, round(image.width * 0.25)), max(8, round(image.height * 0.26)), "upper-left fallback"


def valid_entry_value(value: str) -> bool:
    if not value or len(value) > 10 or value.count(".") > 1:
        return False
    if any(char not in "0123456789." for char in value):
        return False
    if value == ".":
        return False
    if "." in value and len(value.split(".", 1)[1]) > 2:
        return False
    return True


def _mac_post_mouse(event_type: int, x: int, y: int) -> None:
    if Quartz is None:
        raise RuntimeError("Quartz is unavailable in this Mac build.")
    event = Quartz.CGEventCreateMouseEvent(None, event_type, (float(x), float(y)), Quartz.kCGMouseButtonLeft)
    if event is None:
        raise RuntimeError("macOS could not create a mouse event.")
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def win_click(x: int, y: int) -> None:
    """Cross-platform click helper; name retained for v1.9 compatibility."""
    if IS_WINDOWS:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.035)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        return
    if IS_MAC:
        if Quartz is None:
            raise RuntimeError("Quartz automation framework is unavailable.")
        move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (float(x), float(y)), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
        time.sleep(0.035)
        _mac_post_mouse(Quartz.kCGEventLeftMouseDown, x, y)
        _mac_post_mouse(Quartz.kCGEventLeftMouseUp, x, y)
        return
    raise RuntimeError("Automatic clicking is supported only on Windows and macOS.")


def _mac_key_tap(keycode: int, flags: int = 0) -> None:
    if Quartz is None:
        raise RuntimeError("Quartz is unavailable in this Mac build.")
    down = Quartz.CGEventCreateKeyboardEvent(None, int(keycode), True)
    up = Quartz.CGEventCreateKeyboardEvent(None, int(keycode), False)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.022)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def win_key_tap(vk: int) -> None:
    if IS_WINDOWS:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.022)
        user32.keybd_event(vk, 0, 0x0002, 0)
        return
    if IS_MAC:
        _mac_key_tap(vk)
        return
    raise RuntimeError("Keyboard automation is supported only on Windows and macOS.")


def win_select_all() -> None:
    if IS_WINDOWS:
        user32 = ctypes.windll.user32
        user32.keybd_event(0x11, 0, 0, 0)
        win_key_tap(0x41)
        user32.keybd_event(0x11, 0, 0x0002, 0)
        return
    if IS_MAC:
        # Hardware keycode 0 = A on standard Mac layouts. Command+A selects all.
        _mac_key_tap(0, Quartz.kCGEventFlagMaskCommand if Quartz is not None else 0)
        return
    raise RuntimeError("Select-all automation is supported only on Windows and macOS.")


def win_type_text(text: str) -> None:
    if IS_WINDOWS:
        key_map = {str(i): 0x30 + i for i in range(10)}
        key_map["."] = 0xBE
        for char in text:
            vk = key_map.get(char)
            if vk is None:
                raise ValueError("The wager value may contain only digits and a decimal point.")
            win_key_tap(vk)
            time.sleep(0.035)
        return
    if IS_MAC:
        # ANSI hardware keycodes for the number row and period. The app only permits
        # numeric wager text, which makes this more reliable than clipboard injection.
        key_map = {
            "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
            "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, ".": 47,
        }
        for char in text:
            keycode = key_map.get(char)
            if keycode is None:
                raise ValueError("The wager value may contain only digits and a decimal point.")
            _mac_key_tap(keycode)
            time.sleep(0.035)
        return
    raise RuntimeError("Text automation is supported only on Windows and macOS.")


def escape_pressed() -> bool:
    if IS_WINDOWS:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)
    if IS_MAC and Quartz is not None:
        try:
            # 53 is the hardware Escape keycode. Combined-session state works while
            # the main Tk window is hidden during the automated sequence.
            return bool(Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, 53))
        except Exception:
            return False
    return False


def mouse_in_failsafe_corner() -> bool:
    if IS_WINDOWS:
        point = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return point.x <= 2 and point.y <= 2
    if IS_MAC and Quartz is not None:
        try:
            event = Quartz.CGEventCreate(None)
            point = Quartz.CGEventGetLocation(event)
            return point.x <= 3 and point.y <= 3
        except Exception:
            return False
    return False


class SelectionOverlay(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        image: Image.Image,
        bounds: ScreenBounds,
        instruction: str,
        on_complete: Callable[[tuple[int, int, int, int]], None],
        on_cancel: Callable[[], None],
        min_width: int = 25,
        min_height: int = 20,
    ) -> None:
        super().__init__(parent)
        self.image = image
        self.bounds = bounds
        self.on_complete = on_complete
        self.on_cancel = on_cancel
        self.min_width = min_width
        self.min_height = min_height
        self.start: tuple[int, int] | None = None
        self.rect_id: int | None = None
        self.photo = ImageTk.PhotoImage(image)

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.98)
        except tk.TclError:
            pass
        self.geometry(f"{bounds.width}x{bounds.height}{bounds.left:+d}{bounds.top:+d}")

        self.canvas = tk.Canvas(self, width=bounds.width, height=bounds.height, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_rectangle(0, 0, bounds.width, 42, fill="#10151c", outline="")
        self.canvas.create_text(14, 21, text=instruction, fill="white", anchor="w", font=("Segoe UI", 11, "bold"))
        self.canvas.create_text(bounds.width - 14, 21, text="Esc = cancel", fill="#d5dce5", anchor="e", font=("Segoe UI", 9))

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.focus_force()
        self.grab_set()

    def _press(self, event: tk.Event) -> None:
        self.start = (event.x, event.y)
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#ffcc4d",
            width=3,
            fill="#3489ff",
            stipple="gray25",
        )

    def _drag(self, event: tk.Event) -> None:
        if self.start and self.rect_id is not None:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], event.x, event.y)

    def _release(self, event: tk.Event) -> None:
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = event.x, event.y
        x1, x2 = sorted((max(0, x1), min(self.bounds.width, x2)))
        y1, y2 = sorted((max(0, y1), min(self.bounds.height, y2)))
        if x2 - x1 < self.min_width or y2 - y1 < self.min_height:
            messagebox.showwarning("Selection too small", "Drag a larger rectangle around the target area.", parent=self)
            return
        self.grab_release()
        self.destroy()
        self.on_complete((x1, y1, x2, y2))

    def _cancel(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.on_cancel()


class ManualMarkDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, image: Image.Image, global_rect: tuple[int, int, int, int], on_complete: Callable[[list[BoxPair]], None]) -> None:
        super().__init__(parent)
        self.title("Manual Box Points")
        self.configure(bg=Theme.BG)
        self.transient(parent)
        self.grab_set()
        self.image = image
        self.global_rect = global_rect
        self.on_complete = on_complete
        self.points: list[tuple[int, int]] = []

        max_w, max_h = 760, 650
        scale = min(1.0, max_w / image.width, max_h / image.height)
        self.display = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
        self.scale = scale
        self.photo = ImageTk.PhotoImage(self.display)

        tk.Label(
            self,
            text="Click TOP then BOTTOM for every set, moving downward. Finish requires an even number of points.",
            bg=Theme.BG,
            fg=Theme.TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 5))
        self.canvas = tk.Canvas(self, width=self.display.width, height=self.display.height, highlightthickness=1, highlightbackground=Theme.BORDER)
        self.canvas.pack(padx=10, pady=5)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.bind("<Button-1>", self._click)

        row = tk.Frame(self, bg=Theme.BG)
        row.pack(fill="x", padx=10, pady=10)
        self.status = tk.Label(row, text="0 points", bg=Theme.BG, fg=Theme.MUTED)
        self.status.pack(side="left")
        ttk.Button(row, text="Undo", command=self._undo).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Finish", style="Accent.TButton", command=self._finish).pack(side="right")
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Control-z>", self._undo)

    def _click(self, event: tk.Event) -> None:
        self.points.append((event.x, event.y))
        number = len(self.points)
        color = "#3aa4ff" if number % 2 else "#36c878"
        self.canvas.create_oval(event.x - 5, event.y - 5, event.x + 5, event.y + 5, fill=color, outline="white", width=1, tags="point")
        self.canvas.create_text(event.x + 9, event.y, text=str(number), fill="black", anchor="w", font=("Segoe UI", 8, "bold"), tags="point")
        self.status.configure(text=f"{number} points — next: {'BOTTOM' if number % 2 else 'TOP'}")

    def _undo(self, _event: tk.Event | None = None) -> None:
        if not self.points:
            return
        self.points.pop()
        self.canvas.delete("point")
        old = list(self.points)
        self.points.clear()
        for x, y in old:
            self._click(type("Evt", (), {"x": x, "y": y})())

    def _finish(self) -> None:
        if len(self.points) < 2 or len(self.points) % 2:
            messagebox.showwarning("Incomplete points", "Mark a top and bottom point for every set.", parent=self)
            return
        gx1, gy1, _gx2, _gy2 = self.global_rect
        scaled = [(gx1 + round(x / self.scale), gy1 + round(y / self.scale)) for x, y in self.points]
        pairs = []
        for i in range(0, len(scaled), 2):
            pairs.append(BoxPair(BoxTarget(*scaled[i]), BoxTarget(*scaled[i + 1])))
        self.grab_release()
        self.destroy()
        self.on_complete(pairs)


class BoxFlipApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.configure(bg=Theme.BG)
        self.geometry("448x820")
        self.minsize(420, 750)
        try:
            if IS_MAC:
                icon_path = Path(__file__).with_name("boxflip.png")
                if icon_path.exists():
                    self._app_icon = tk.PhotoImage(file=str(icon_path))
                    self.iconphoto(True, self._app_icon)
            else:
                self.iconbitmap(str(Path(__file__).with_name("boxflip.ico")))
        except Exception:
            pass

        self.regions: dict[str, tuple[int, int, int, int] | None] = {"boxes": None, "wager": None, "done": None}
        self.pairs: list[BoxPair] = []
        self.column_pairs: list[list[BoxPair]] = []
        self.last_sequence: list[int] = []
        self.running = False
        self.stop_event = threading.Event()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._configure_styles()
        self._build_ui()
        self._load_config()
        self._refresh_region_labels()
        self._poll_events()
        if IS_MAC:
            self.after(700, self._refresh_mac_permissions)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "TButton",
            font=("Segoe UI", 8, "bold"),
            padding=(8, 5),
            background=Theme.PANEL_2,
            foreground=Theme.TEXT,
            bordercolor=Theme.BORDER,
            lightcolor=Theme.BORDER,
            darkcolor=Theme.BORDER,
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", "#2A1820"), ("pressed", "#351923"), ("disabled", "#151317")],
            foreground=[("disabled", "#625A5F")],
            bordercolor=[("active", Theme.BORDER_HOT)],
        )
        style.configure(
            "Capture.TButton",
            background="#271218", foreground=Theme.GOLD_SOFT, bordercolor="#61202D",
            font=("Segoe UI", 8, "bold"), padding=(8, 5),
        )
        style.map("Capture.TButton", background=[("active", "#38151E")], bordercolor=[("active", Theme.RED_BRIGHT)])
        style.configure(
            "Accent.TButton",
            background=Theme.RED, foreground="white", bordercolor=Theme.RED_BRIGHT,
            font=("Segoe UI", 9, "bold"), padding=(10, 7),
        )
        style.map("Accent.TButton", background=[("active", Theme.RED_BRIGHT), ("pressed", "#B91F30"), ("disabled", "#3A2026")])
        style.configure(
            "Stop.TButton",
            background="#191216", foreground="#F4D9DE", bordercolor="#5E2530",
            font=("Segoe UI", 8, "bold"), padding=(8, 6),
        )
        style.map("Stop.TButton", background=[("active", "#2A151B")])
        style.configure(
            "TEntry",
            fieldbackground=Theme.INPUT, foreground=Theme.TEXT, insertcolor=Theme.GOLD_SOFT,
            bordercolor=Theme.BORDER, lightcolor=Theme.BORDER, darkcolor=Theme.BORDER,
            padding=(6, 5),
        )
        style.map("TEntry", bordercolor=[("focus", Theme.GOLD)])
        style.configure(
            "TSpinbox", fieldbackground=Theme.INPUT, foreground=Theme.TEXT, arrowcolor=Theme.GOLD_SOFT,
            bordercolor=Theme.BORDER, padding=(6, 5),
        )

    def _section(self, parent: tk.Widget, title: str, compact: bool = False) -> tk.Frame:
        frame = tk.Frame(parent, bg=Theme.PANEL, highlightbackground=Theme.BORDER, highlightthickness=1, bd=0)
        top = 6 if compact else 8
        tk.Label(
            frame, text=title.upper(), bg=Theme.PANEL, fg=Theme.GOLD_SOFT,
            font=("Segoe UI Semibold", 8), anchor="w"
        ).pack(anchor="w", padx=10, pady=(top, 4))
        return frame

    def _divider(self, parent: tk.Widget) -> None:
        tk.Frame(parent, height=1, bg="#28181E").pack(fill="x", padx=10)

    def _build_ui(self) -> None:
        # Header: narrow, practical, and intentionally free of decorative clutter.
        header = tk.Frame(self, bg=Theme.BG)
        header.pack(fill="x", padx=10, pady=(9, 6))
        title_col = tk.Frame(header, bg=Theme.BG)
        title_col.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_col, text="BOX FLIP", bg=Theme.BG, fg=Theme.TEXT,
            font=("Segoe UI Semibold", 15)
        ).pack(anchor="w")
        tk.Label(
            title_col, text="AUTOMATOR  ·  v1.9", bg=Theme.BG, fg=Theme.GOLD,
            font=("Segoe UI Semibold", 7)
        ).pack(anchor="w", pady=(0, 1))
        self.ready_pill = tk.Label(
            header, text="●  READY", bg="#171218", fg=Theme.GREEN,
            font=("Segoe UI Semibold", 7), padx=9, pady=5,
            highlightbackground=Theme.BORDER, highlightthickness=1
        )
        self.ready_pill.pack(side="right", padx=(5, 0))
        if IS_MAC:
            ttk.Button(header, text="Mac permissions", command=self._request_mac_permissions).pack(side="right", padx=(0, 5))
        ttk.Button(header, text="Show regions", command=self._flash_regions).pack(side="right")

        # Capture regions: same controls as v1.5, made much denser.
        regions = self._section(self, "Screen regions", compact=True)
        regions.pack(fill="x", padx=10, pady=3)
        body = tk.Frame(regions, bg=Theme.PANEL)
        body.pack(fill="x", padx=8, pady=(0, 7))
        body.grid_columnconfigure(1, weight=1)

        self.region_labels: dict[str, tk.Label] = {}
        rows = [
            ("boxes", "01", "FLIP BOXES", "Set", "Top = 1  ·  Bottom = 2"),
            ("wager", "02", "WAGER + GREEN", "Set", "Wager field + green control"),
            ("done", "03", "BLUE DONE", "Set", "Final Done area"),
        ]
        for row_index, (key, number, label, button_text, hint) in enumerate(rows):
            line = tk.Frame(body, bg=Theme.PANEL_2, highlightbackground="#2B1C22", highlightthickness=1)
            line.grid(row=row_index, column=0, columnspan=3, sticky="ew", pady=2)
            line.grid_columnconfigure(1, weight=1)
            tk.Label(line, text=number, bg=Theme.PANEL_2, fg=Theme.RED_BRIGHT, font=("Consolas", 8, "bold"), width=3).grid(row=0, column=0, rowspan=2, padx=(6, 2), pady=5)
            tk.Label(line, text=label, bg=Theme.PANEL_2, fg=Theme.TEXT, anchor="w", font=("Segoe UI Semibold", 8)).grid(row=0, column=1, sticky="sw", pady=(4, 0))
            status = tk.Label(line, text=hint, bg=Theme.PANEL_2, fg=Theme.MUTED, anchor="w", font=("Segoe UI", 7))
            status.grid(row=1, column=1, sticky="nw", pady=(0, 4))
            self.region_labels[key] = status
            btns = tk.Frame(line, bg=Theme.PANEL_2)
            btns.grid(row=0, column=2, rowspan=2, padx=5)
            ttk.Button(btns, text=button_text, width=5, style="Capture.TButton", command=lambda k=key: self._start_capture(k)).pack(side="left")
            if key == "boxes":
                self.manual_button = ttk.Button(btns, text="Manual", width=6, command=self._manual_points, state="disabled")
                self.manual_button.pack(side="left", padx=(4, 0))

        # Settings in two columns so the window stays tall instead of wide.
        settings = self._section(self, "Run settings", compact=True)
        settings.pack(fill="x", padx=10, pady=3)
        grid = tk.Frame(settings, bg=Theme.PANEL)
        grid.pack(fill="x", padx=8, pady=(0, 7))
        grid.grid_columnconfigure(1, weight=1)
        grid.grid_columnconfigure(3, weight=1)

        self.value_var = tk.StringVar(value=".10")
        self.rounds_var = tk.StringVar(value="20")
        self.box_seconds_var = tk.StringVar(value="3.0")
        self.wager_at_var = tk.StringVar(value="6.0")
        self.accept_after_var = tk.StringVar(value="1.0")
        self.done_after_var = tk.StringVar(value="3.0")
        self.round_gap_var = tk.StringVar(value="1.0")
        self.wait_window_var = tk.StringVar(value="5.0")
        self.odds_weighting_var = tk.BooleanVar(value=True)
        self.mlb_algo_var = tk.BooleanVar(value=False)
        self.max_favorite_var = tk.StringVar(value="75")
        self.across_line_var = tk.BooleanVar(value=False)
        self.random_lines_var = tk.BooleanVar(value=False)

        fields = [
            (0, 0, "Wager", self.value_var),
            (0, 2, "Rounds", self.rounds_var),
            (1, 0, "Box phase", self.box_seconds_var),
            (1, 2, "Wager check", self.wager_at_var),
            (2, 0, "Green delay", self.accept_after_var),
            (2, 2, "Done delay", self.done_after_var),
            (3, 0, "Round gap", self.round_gap_var),
            (3, 2, "Find window", self.wait_window_var),
        ]
        for row, col, label, var in fields:
            tk.Label(grid, text=label, bg=Theme.PANEL, fg=Theme.MUTED, font=("Segoe UI", 7)).grid(row=row, column=col, sticky="w", padx=(2, 4), pady=3)
            ttk.Entry(grid, textvariable=var, width=9).grid(row=row, column=col + 1, sticky="ew", padx=(0, 8), pady=3)

        line_modes = tk.Frame(grid, bg=Theme.PANEL_2, highlightbackground="#3B252D", highlightthickness=1)
        line_modes.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(5, 1), padx=1)
        tk.Checkbutton(
            line_modes, text="Across the Line", variable=self.across_line_var, command=self._across_line_changed,
            bg=Theme.PANEL_2, fg=Theme.GOLD_SOFT, activebackground=Theme.PANEL_2, activeforeground=Theme.GOLD_SOFT,
            selectcolor=Theme.RED_DARK, font=("Segoe UI Semibold", 8), bd=0, highlightthickness=0
        ).pack(side="left", padx=(7, 8), pady=5)
        tk.Checkbutton(
            line_modes, text="Random Lines", variable=self.random_lines_var, command=self._random_lines_changed,
            bg=Theme.PANEL_2, fg=Theme.GOLD_SOFT, activebackground=Theme.PANEL_2, activeforeground=Theme.GOLD_SOFT,
            selectcolor=Theme.RED_DARK, font=("Segoe UI Semibold", 8), bd=0, highlightthickness=0
        ).pack(side="left", padx=(0, 7), pady=5)

        # Odds controls remain functionally identical.
        odds = self._section(self, "Odds weighting", compact=True)
        odds.pack(fill="x", padx=10, pady=3)
        odds_body = tk.Frame(odds, bg=Theme.PANEL)
        odds_body.pack(fill="x", padx=8, pady=(0, 7))
        odds_body.grid_columnconfigure(2, weight=1)
        odds_toggle = tk.Checkbutton(
            odds_body, text="Use odds", variable=self.odds_weighting_var,
            bg=Theme.PANEL, fg=Theme.TEXT, activebackground=Theme.PANEL, activeforeground=Theme.TEXT,
            selectcolor=Theme.RED_DARK, font=("Segoe UI Semibold", 8), bd=0, highlightthickness=0
        )
        odds_toggle.grid(row=0, column=0, sticky="w", padx=(2, 5))
        self.mlb_algo_toggle = tk.Checkbutton(
            odds_body, text="MLB 5yr", variable=self.mlb_algo_var, command=self._mlb_algo_changed,
            bg=Theme.PANEL, fg=Theme.GOLD_SOFT, activebackground=Theme.PANEL, activeforeground=Theme.GOLD_SOFT,
            selectcolor=Theme.RED_DARK, font=("Segoe UI Semibold", 8), bd=0, highlightthickness=0
        )
        self.mlb_algo_toggle.grid(row=0, column=1, sticky="w", padx=(0, 5))
        tk.Label(odds_body, text="Max favorite", bg=Theme.PANEL, fg=Theme.MUTED, font=("Segoe UI", 7)).grid(row=0, column=2, sticky="e", padx=(0, 4))
        ttk.Entry(odds_body, textvariable=self.max_favorite_var, width=5).grid(row=0, column=3, sticky="w")
        tk.Label(odds_body, text="%", bg=Theme.PANEL, fg=Theme.GOLD, font=("Segoe UI", 7, "bold")).grid(row=0, column=4, sticky="w", padx=(3, 6))
        ttk.Button(odds_body, text="Read odds", width=8, command=self._preview_odds).grid(row=0, column=5, sticky="e")
        tk.Label(
            odds_body, text="MLB 5yr blends the market line with the 42.9% historical underdog baseline.",
            bg=Theme.PANEL, fg=Theme.MUTED, font=("Segoe UI", 7)
        ).grid(row=1, column=0, columnspan=6, sticky="w", padx=2, pady=(4, 0))

        # Live status: compact preview + shallow log, not a giant text box.
        progress = self._section(self, "Sequence + status", compact=True)
        progress.pack(fill="both", expand=True, padx=10, pady=3)
        self.sequence_label = tk.Label(
            progress, text="Preview: capture boxes, then generate.", bg=Theme.PANEL, fg=Theme.GOLD_SOFT,
            anchor="w", justify="left", font=("Consolas", 8, "bold"), wraplength=395
        )
        self.sequence_label.pack(fill="x", padx=9, pady=(0, 4))
        self.status_label = tk.Label(
            progress, text="Ready.", bg=Theme.PANEL_2, fg=Theme.TEXT, anchor="w", padx=8, pady=5,
            font=("Segoe UI Semibold", 8), highlightbackground="#2A1B21", highlightthickness=1
        )
        self.status_label.pack(fill="x", padx=8, pady=(0, 4))
        self.log = tk.Text(
            progress, height=4, bg="#0C0B0D", fg="#D9CDD1", insertbackground="white",
            relief="flat", font=("Consolas", 7), state="disabled", wrap="word",
            highlightbackground="#24171C", highlightthickness=1
        )
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 7))

        # Actions stay exactly the same functions/buttons as v1.5, just restyled and stacked.
        actions = tk.Frame(self, bg=Theme.BG)
        actions.pack(fill="x", padx=10, pady=(4, 3))
        self.start_button = ttk.Button(actions, text="Start repeating", style="Accent.TButton", command=self._begin_run)
        self.start_button.pack(fill="x")
        secondary = tk.Frame(actions, bg=Theme.BG)
        secondary.pack(fill="x", pady=(5, 0))
        self.preview_button = ttk.Button(secondary, text="New preview", command=self._new_preview)
        self.preview_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(secondary, text="Stop", style="Stop.TButton", command=self._request_stop, state="disabled")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(5, 0))

        footer = tk.Label(
            self, text="ESC or top-left corner = emergency stop", bg=Theme.BG, fg="#8F767F",
            font=("Segoe UI", 7)
        )
        footer.pack(anchor="center", pady=(2, 7))

    def _refresh_mac_permissions(self) -> None:
        if not IS_MAC:
            return
        screen_ok = mac_screen_capture_allowed()
        access_ok = mac_accessibility_allowed(False)
        if screen_ok and access_ok:
            self.ready_pill.configure(text="●  MAC READY", fg=Theme.GREEN)
        else:
            missing = []
            if not screen_ok:
                missing.append("Screen")
            if not access_ok:
                missing.append("Accessibility")
            self.ready_pill.configure(text="●  PERMISSIONS", fg=Theme.GOLD)
            if not self.running:
                self.status_label.configure(text=f"Mac permission needed: {', '.join(missing)}. Click Mac permissions.")

    def _request_mac_permissions(self) -> None:
        if not IS_MAC:
            return
        if Quartz is None:
            messagebox.showerror(
                "Mac framework missing",
                "This build is missing its bundled Quartz framework. Rebuild the Mac package from the supplied GitHub workflow.",
                parent=self,
            )
            return
        request_mac_screen_capture()
        mac_accessibility_allowed(prompt=True)
        self.after(700, self._refresh_mac_permissions)
        messagebox.showinfo(
            "Mac permissions",
            "Box Flip Automator needs two macOS permissions:\n\n"
            "1. Screen & System Audio Recording — lets it see the selected areas.\n"
            "2. Accessibility — lets it move the mouse and type/click.\n\n"
            "macOS may require you to enable Box Flip Automator in System Settings → Privacy & Security and then reopen the app. "
            "No Python, Homebrew, or separate OCR software is required.",
            parent=self,
        )

    def _ensure_mac_permissions(self, *, screen: bool, accessibility: bool) -> bool:
        if not IS_MAC:
            return True
        missing: list[str] = []
        if screen and not mac_screen_capture_allowed():
            request_mac_screen_capture()
            if not mac_screen_capture_allowed():
                missing.append("Screen & System Audio Recording")
        if accessibility and not mac_accessibility_allowed(False):
            mac_accessibility_allowed(prompt=True)
            if not mac_accessibility_allowed(False):
                missing.append("Accessibility")
        if missing:
            self._refresh_mac_permissions()
            messagebox.showwarning(
                "Mac permission required",
                "Enable Box Flip Automator under System Settings → Privacy & Security for: "
                + ", ".join(missing)
                + ". Then quit and reopen the app if macOS asks you to.",
                parent=self,
            )
            return False
        self._refresh_mac_permissions()
        return True

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _three_column_mode_active(self) -> bool:
        return bool(self.across_line_var.get() or self.random_lines_var.get())

    def _refresh_region_labels(self) -> None:
        boxes = self.regions.get("boxes")
        wager = self.regions.get("wager")
        done = self.regions.get("done")
        if boxes:
            if self._three_column_mode_active() and self.column_pairs:
                counts = "/".join(str(len(items)) for items in self.column_pairs)
                mode = "Random Lines" if self.random_lines_var.get() else "Across the Line"
                self.region_labels["boxes"].configure(text=f"{mode} saved — L/M/R {counts}", fg=Theme.GREEN)
                self.manual_button.configure(state="disabled")
            else:
                self.region_labels["boxes"].configure(text=f"Saved — {len(self.pairs)} top/bottom sets", fg=Theme.GREEN if self.pairs else Theme.GOLD)
                self.manual_button.configure(state="normal")
        else:
            if self.random_lines_var.get():
                hint = "All 3 columns: random column on every row"
            elif self.across_line_var.get():
                hint = "All 3 columns: left → middle → right"
            else:
                hint = "Full column: top=1, bottom=2"
            self.region_labels["boxes"].configure(text=hint, fg=Theme.MUTED)
            self.manual_button.configure(state="disabled")
        if wager:
            self.region_labels["wager"].configure(text=f"Saved — {wager[2]-wager[0]}×{wager[3]-wager[1]} px", fg=Theme.GREEN)
        else:
            self.region_labels["wager"].configure(text="One area containing both controls", fg=Theme.MUTED)
        if done:
            self.region_labels["done"].configure(text=f"Saved — {done[2]-done[0]}×{done[3]-done[1]} px", fg=Theme.GREEN)
        else:
            self.region_labels["done"].configure(text="Area where the Done popup appears", fg=Theme.MUTED)

    def _rescan_region_for_mode(self) -> None:
        if self.running:
            return
        rect = self.regions.get("boxes")
        if rect:
            try:
                image = capture_region(rect)
                if self._three_column_mode_active():
                    columns, _slices, status = detect_three_columns(image, rect[0], rect[1])
                    self.column_pairs = columns
                    self.pairs = columns[0] if columns else []
                    self._log(status)
                else:
                    pairs, status = detect_box_pairs(image, rect[0], rect[1])
                    self.pairs = pairs
                    self.column_pairs = []
                    self._log(status)
            except Exception as exc:
                self._log(f"Region 1 re-scan failed: {exc}")
        self._refresh_region_labels()
        self._save_config()

    def _across_line_changed(self) -> None:
        if self.across_line_var.get():
            self.random_lines_var.set(False)
        self._rescan_region_for_mode()

    def _random_lines_changed(self) -> None:
        if self.random_lines_var.get():
            self.across_line_var.set(False)
        self._rescan_region_for_mode()

    def _start_capture(self, kind: str) -> None:
        if self.running:
            return
        if not self._ensure_mac_permissions(screen=True, accessibility=False):
            return
        self.withdraw()
        self.after(400, lambda: self._capture_after_hide(kind))

    def _capture_after_hide(self, kind: str) -> None:
        try:
            image, bounds = capture_virtual_screen()
        except Exception as exc:
            self.deiconify()
            messagebox.showerror("Capture failed", str(exc), parent=self)
            return

        instructions = {
            "boxes": (
                "REGION 1 — THREE COLUMNS: Drag around ALL THREE columns of top-and-bottom boxes."
                if self._three_column_mode_active() else
                "REGION 1: Drag around the complete column of top-and-bottom boxes."
            ),
            "wager": "REGION 2: Drag around the entire future WAGER field and green button area.",
            "done": "REGION 3: Drag around the full location where the blue Done button appears.",
        }
        min_heights = {"boxes": 60, "wager": 30, "done": 20}

        def complete(local_rect: tuple[int, int, int, int]) -> None:
            lx1, ly1, lx2, ly2 = local_rect
            global_rect = (bounds.left + lx1, bounds.top + ly1, bounds.left + lx2, bounds.top + ly2)
            self.regions[kind] = global_rect
            if kind == "boxes":
                roi = image.crop(local_rect)
                if self._three_column_mode_active():
                    columns, _slices, status = detect_three_columns(roi, global_rect[0], global_rect[1])
                    self.column_pairs = columns
                    self.pairs = columns[0] if columns else []
                    ready = len(columns) == 3 and all(columns)
                    self._log(status)
                    if ready:
                        self.last_sequence = secure_sequence(len(columns[0]))
                        self._show_sequence(self.last_sequence, "3-column preview")
                    else:
                        messagebox.showwarning("Columns not detected", status + "\n\nHighlight all three columns tightly and try again.", parent=self)
                else:
                    pairs, status = detect_box_pairs(roi, global_rect[0], global_rect[1])
                    self.pairs = pairs
                    self.column_pairs = []
                    self._log(status)
                    if pairs:
                        self.last_sequence = secure_sequence(len(pairs))
                        self._show_sequence(self.last_sequence, "Preview")
                    else:
                        messagebox.showwarning("Boxes not detected", status, parent=self)
            else:
                self._log(f"Region {2 if kind == 'wager' else 3} saved.")
            self._save_config()
            self.deiconify()
            self.lift()
            self._refresh_region_labels()

        def cancel() -> None:
            self.deiconify()
            self.lift()

        SelectionOverlay(
            self,
            image,
            bounds,
            instructions[kind],
            complete,
            cancel,
            min_width=25,
            min_height=min_heights[kind],
        )

    def _manual_points(self) -> None:
        rect = self.regions.get("boxes")
        if not rect:
            return
        try:
            image = capture_region(rect)
        except Exception as exc:
            messagebox.showerror("Capture failed", str(exc), parent=self)
            return

        def completed(pairs: list[BoxPair]) -> None:
            self.pairs = pairs
            self.last_sequence = secure_sequence(len(pairs))
            self._show_sequence(self.last_sequence, "Preview")
            self._save_config()
            self._refresh_region_labels()
            self._log(f"Manually saved {len(pairs)} top/bottom sets.")

        ManualMarkDialog(self, image, rect, completed)

    def _flash_regions(self) -> None:
        active = [(key, rect) for key, rect in self.regions.items() if rect]
        if not active:
            messagebox.showinfo("No regions", "Capture at least one region first.", parent=self)
            return
        overlays: list[tk.Toplevel] = []
        labels = {"boxes": "1 BOXES", "wager": "2 WAGER + GREEN", "done": "3 DONE"}
        for key, rect in active:
            assert rect is not None
            x1, y1, x2, y2 = rect
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-alpha", 0.32)
            except tk.TclError:
                pass
            win.configure(bg=REGION_COLORS[key])
            win.geometry(f"{x2-x1}x{y2-y1}{x1:+d}{y1:+d}")
            tk.Label(win, text=labels[key], bg=REGION_COLORS[key], fg="white", font=("Segoe UI", 9, "bold")).pack(anchor="nw", padx=4, pady=2)
            overlays.append(win)
        self.after(1200, lambda: [win.destroy() for win in overlays if win.winfo_exists()])

    def _new_preview(self) -> None:
        if not self.pairs:
            messagebox.showinfo("No box sets", "Capture Region 1 first.", parent=self)
            return
        if self.random_lines_var.get():
            self._preview_random_lines()
            return
        if self.across_line_var.get():
            self._preview_across_line()
            return
        if self.odds_weighting_var.get():
            self._preview_odds()
            return
        self.last_sequence = secure_sequence(len(self.pairs))
        self._show_sequence(self.last_sequence, "50/50 preview")

    def _preview_across_line(self) -> None:
        rect = self.regions.get("boxes")
        if not rect:
            return
        try:
            image = capture_region(rect)
            columns, slices, status = detect_three_columns(image, rect[0], rect[1])
            if len(columns) != 3 or any(not column for column in columns):
                raise RuntimeError(status)
            self.column_pairs = columns
            x1, x2 = slices[0]
            left_image = image.crop((x1, 0, x2, image.height))
            if self.odds_weighting_var.get():
                max_favorite = float(self.max_favorite_var.get()) / 100.0
                sequence, _details, source = odds_weighted_sequence(left_image, columns[0], max_favorite, bool(self.mlb_algo_var.get()))
                prefix = f"LEFT odds ({source})"
            else:
                sequence = secure_sequence(len(columns[0]))
                prefix = "LEFT 50/50"
            self.last_sequence = sequence
            self._show_sequence(sequence, prefix)
            self._log(status + " Across the Line rotates LEFT → MIDDLE → RIGHT after each completed wager.")
            self._refresh_region_labels()
        except Exception as exc:
            messagebox.showerror("Across the Line preview failed", str(exc), parent=self)

    def _preview_random_lines(self) -> None:
        rect = self.regions.get("boxes")
        if not rect:
            return
        try:
            image = capture_region(rect)
            columns, slices, status = detect_three_columns(image, rect[0], rect[1])
            if len(columns) != 3 or any(not column for column in columns):
                raise RuntimeError(status)
            counts = [len(c) for c in columns]
            if len(set(counts)) != 1:
                raise RuntimeError(f"Random Lines needs matching row counts; L/M/R detected {counts[0]}/{counts[1]}/{counts[2]}.")
            self.column_pairs = columns
            row_count = counts[0]
            choices = [secrets.randbelow(3) for _ in range(row_count)]
            if self.odds_weighting_var.get():
                max_favorite = float(self.max_favorite_var.get()) / 100.0
                column_sequences = []
                for index, (x1, x2) in enumerate(slices):
                    crop = image.crop((x1, 0, x2, image.height))
                    seq, _details, _source = odds_weighted_sequence(crop, columns[index], max_favorite, bool(self.mlb_algo_var.get()))
                    column_sequences.append(seq)
                sequence = [column_sequences[col][row] for row, col in enumerate(choices)]
                prefix = "Random Lines odds"
            else:
                sequence = secure_sequence(row_count)
                prefix = "Random Lines 50/50"
            labels = ("L", "M", "R")
            display = [f"{labels[col]}{result}" for col, result in zip(choices, sequence)]
            self.last_sequence = sequence
            self._show_sequence(display, prefix)
            self._log(status + " Random Lines chooses a fresh column on every row from top to bottom.")
            self._refresh_region_labels()
        except Exception as exc:
            messagebox.showerror("Random Lines preview failed", str(exc), parent=self)


    def _mlb_algo_changed(self) -> None:
        if self.mlb_algo_var.get():
            self.odds_weighting_var.set(True)
            self.status_label.configure(text="MLB 5yr algorithm ON — odds + 42.9% underdog calibration.")
            self._log("MLB 5yr algorithm enabled. Market odds stay primary; five-season underdog baseline is blended in.")
        else:
            self.status_label.configure(text="MLB 5yr algorithm OFF — standard odds weighting.")
        self._save_config()

    def _preview_odds(self) -> None:
        if self.running:
            return
        rect = self.regions.get("boxes")
        if not rect or not self.pairs:
            messagebox.showinfo("No box sets", "Capture Region 1 first.", parent=self)
            return
        try:
            max_favorite = float(self.max_favorite_var.get()) / 100.0
        except ValueError:
            messagebox.showerror("Invalid cap", "Max favorite must be a number from 50 to 75.", parent=self)
            return
        if not 0.50 <= max_favorite <= 0.75:
            messagebox.showerror("Invalid cap", "Max favorite must be from 50% to 75%.", parent=self)
            return
        self.status_label.configure(text="Reading the odds in Region 1…")
        self.withdraw()
        self.after(350, lambda: self._preview_odds_after_hide(rect, max_favorite))

    def _preview_odds_after_hide(self, rect: tuple[int, int, int, int], max_favorite: float) -> None:
        try:
            image = capture_region(rect)
            fresh_pairs, status = detect_box_pairs(image, rect[0], rect[1])
            if len(fresh_pairs) != len(self.pairs):
                raise RuntimeError(f"Expected {len(self.pairs)} sets but found {len(fresh_pairs)}. {status}")
            sequence, details, source = odds_weighted_sequence(image, fresh_pairs, max_favorite, bool(self.mlb_algo_var.get()))
            self.last_sequence = sequence
            self._show_sequence(sequence, "Odds preview")
            self._log(f"Odds read with {source}; favorite cap {max_favorite*100:.0f}%.")
            for index, (top_odds, bottom_odds, top_probability) in enumerate(details, 1):
                self._log(
                    f"Set {index}: {top_odds:+d} / {bottom_odds:+d} -> "
                    f"top {top_probability*100:.0f}% / bottom {(1-top_probability)*100:.0f}%."
                )
            self.status_label.configure(text=f"Odds preview ready — {len(sequence)} weighted flips.")
        except Exception as exc:
            self.status_label.configure(text="Odds preview failed; use 50/50 or recapture Region 1.")
            messagebox.showerror("Odds read failed", str(exc), parent=self)
        finally:
            self.deiconify()
            self.lift()

    def _show_sequence(self, sequence: list[int], prefix: str) -> None:
        text = " ".join(map(str, sequence))
        self.sequence_label.configure(text=f"{prefix}: {text}")

    def _parse_settings(self) -> dict[str, float | int | str | bool]:
        value = self.value_var.get().strip()
        if not valid_entry_value(value):
            raise ValueError("Value must contain only digits and an optional decimal point, with at most two decimal places.")
        try:
            rounds = int(self.rounds_var.get())
            box_seconds = float(self.box_seconds_var.get())
            wager_at = float(self.wager_at_var.get())
            accept_after = float(self.accept_after_var.get())
            done_after = float(self.done_after_var.get())
            round_gap = float(self.round_gap_var.get())
            wait_window = float(self.wait_window_var.get())
            max_favorite = float(self.max_favorite_var.get()) / 100.0
        except ValueError as exc:
            raise ValueError("Rounds, timing fields, and Max favorite must be numbers.") from exc
        if not 1 <= rounds <= 999:
            raise ValueError("Rounds must be from 1 to 999.")
        if not 0.2 <= box_seconds <= 30:
            raise ValueError("Box phase must be from 0.2 to 30 seconds.")
        if not box_seconds <= wager_at <= 60:
            raise ValueError("Wager-at time must be at least the box phase and no more than 60 seconds.")
        for name, number in (("Green-after", accept_after), ("Done-after", done_after), ("Round gap", round_gap), ("Find window", wait_window)):
            if not 0.1 <= number <= 30:
                raise ValueError(f"{name} must be from 0.1 to 30 seconds.")
        if not 0.50 <= max_favorite <= 0.75:
            raise ValueError("Max favorite must be from 50% to 75%. 75% is the +50% bias cap.")
        return {
            "value": value,
            "rounds": rounds,
            "box_seconds": box_seconds,
            "wager_at": wager_at,
            "accept_after": accept_after,
            "done_after": done_after,
            "round_gap": round_gap,
            "wait_window": wait_window,
            "odds_weighting": bool(self.odds_weighting_var.get()),
            "mlb_algo": bool(self.mlb_algo_var.get()),
            "max_favorite": max_favorite,
            "across_line": bool(self.across_line_var.get()),
            "random_lines": bool(self.random_lines_var.get()),
        }

    def _begin_run(self) -> None:
        if self.running:
            return
        missing = [name for name, rect in self.regions.items() if rect is None]
        if missing or not self.pairs:
            messagebox.showerror("Setup incomplete", "Capture all three regions and make sure Region 1 contains detected box sets.", parent=self)
            return
        if self._three_column_mode_active() and (len(self.column_pairs) != 3 or any(not column for column in self.column_pairs)):
            mode = "Random Lines" if self.random_lines_var.get() else "Across the Line"
            messagebox.showerror(f"{mode} incomplete", f"Turn on {mode}, then Set Region 1 around all three columns so Left, Middle, and Right can be detected.", parent=self)
            return
        try:
            settings = self._parse_settings()
        except ValueError as exc:
            messagebox.showerror("Invalid setting", str(exc), parent=self)
            return
        if not (IS_WINDOWS or IS_MAC):
            messagebox.showerror("Unsupported platform", "Mouse and keyboard automation requires Windows or macOS.", parent=self)
            return
        if IS_MAC and Quartz is None:
            messagebox.showerror("Mac framework missing", "Quartz automation support is missing from this build.", parent=self)
            return
        if not self._ensure_mac_permissions(screen=True, accessibility=True):
            return

        self._save_config()
        self.running = True
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.preview_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if settings.get("random_lines"):
            mode_text = "Random Lines (random column per row)"
        elif settings.get("across_line"):
            mode_text = "Across the Line L→M→R"
        else:
            mode_text = "standard"
        self._log(f"Starting {settings['rounds']} round(s) with value {settings['value']}; mode={mode_text}; odds weighting={settings['odds_weighting']}, cap={float(settings['max_favorite'])*100:.0f}%.")
        self._countdown(3, settings)

    def _countdown(self, seconds: int, settings: dict[str, float | int | str | bool]) -> None:
        if self.stop_event.is_set():
            self._finish_run(0, "Stopped before starting.")
            return
        if seconds > 0:
            self.status_label.configure(text=f"Starting in {seconds}… Keep the target page still.")
            self.after(1000, lambda: self._countdown(seconds - 1, settings))
            return
        self.status_label.configure(text="Running — app hidden. Esc or top-left stops.")
        self.withdraw()
        thread = threading.Thread(target=self._automation_worker, args=(settings,), daemon=True)
        thread.start()

    def _request_stop(self) -> None:
        self.stop_event.set()
        self.status_label.configure(text="Stopping safely…")

    def _should_stop(self) -> bool:
        return self.stop_event.is_set() or escape_pressed() or mouse_in_failsafe_corner()

    def _sleep_until(self, target: float) -> bool:
        while time.monotonic() < target:
            if self._should_stop():
                return False
            time.sleep(min(0.04, max(0.0, target - time.monotonic())))
        return not self._should_stop()

    def _wait_for_region_change(self, rect: tuple[int, int, int, int], baseline: Image.Image, timeout: float) -> Image.Image | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            if self._should_stop():
                return None
            current = capture_region(rect)
            if difference_score(baseline, current) >= 0.018:
                return current
            time.sleep(0.14)
        return None

    def _wait_for_color(self, rect: tuple[int, int, int, int], color: str, timeout: float) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            if self._should_stop():
                return None
            current = capture_region(rect)
            found = find_solid_color_rect(current, color)
            if found:
                return current, found
            time.sleep(0.14)
        return None

    def _automation_worker(self, settings: dict[str, float | int | str | bool]) -> None:
        completed = 0
        stop_reason = "Requested rounds completed."
        try:
            boxes_rect = self.regions["boxes"]
            wager_rect = self.regions["wager"]
            done_rect = self.regions["done"]
            assert boxes_rect and wager_rect and done_rect
            rounds = int(settings["rounds"])
            value = str(settings["value"])
            box_seconds = float(settings["box_seconds"])
            wager_at = float(settings["wager_at"])
            accept_after = float(settings["accept_after"])
            done_after = float(settings["done_after"])
            round_gap = float(settings["round_gap"])
            wait_window = float(settings["wait_window"])
            odds_weighting = bool(settings.get("odds_weighting", True))
            mlb_algo = bool(settings.get("mlb_algo", False))
            max_favorite = float(settings.get("max_favorite", 0.75))
            across_line = bool(settings.get("across_line", False))
            random_lines = bool(settings.get("random_lines", False))
            column_names = ("LEFT", "MIDDLE", "RIGHT")
            column_short = ("L", "M", "R")

            for round_number in range(1, rounds + 1):
                if self._should_stop():
                    stop_reason = "Emergency stop requested."
                    break

                boxes_image = capture_region(boxes_rect)
                active_name = "BOXES"
                display_sequence: list[int | str]

                if across_line or random_lines:
                    columns, slices, detect_status = detect_three_columns(boxes_image, boxes_rect[0], boxes_rect[1])
                    if len(columns) != 3 or any(not column for column in columns):
                        mode = "Random Lines" if random_lines else "Across the Line"
                        stop_reason = f"{mode} scan failed before round {round_number}: {detect_status}"
                        break
                    counts = [len(column) for column in columns]
                    if random_lines and len(set(counts)) != 1:
                        stop_reason = f"Random Lines needs matching row counts; L/M/R detected {counts[0]}/{counts[1]}/{counts[2]}."
                        break

                    if random_lines:
                        active_name = "RANDOM LINES"
                        row_count = counts[0]
                        chosen_columns = [secrets.randbelow(3) for _ in range(row_count)]
                        round_pairs = [columns[col][row] for row, col in enumerate(chosen_columns)]

                        if odds_weighting:
                            column_sequences: list[list[int]] = []
                            for column_index, (x1, x2) in enumerate(slices):
                                crop = boxes_image.crop((x1, 0, x2, boxes_image.height))
                                col_sequence, details, source = odds_weighted_sequence(crop, columns[column_index], max_favorite, mlb_algo)
                                column_sequences.append(col_sequence)
                                odds_text = "; ".join(
                                    f"{top:+d}/{bottom:+d}={top_p*100:.0f}/{(1-top_p)*100:.0f}"
                                    for top, bottom, top_p in details
                                )
                                self.event_queue.put(("log", f"Round {round_number} {column_names[column_index]} odds {source} -> {odds_text}"))
                            sequence = [column_sequences[col][row] for row, col in enumerate(chosen_columns)]
                        else:
                            sequence = secure_sequence(row_count)

                        display_sequence = [f"{column_short[col]}{result}" for col, result in zip(chosen_columns, sequence)]
                        route = " → ".join(column_short[col] for col in chosen_columns)
                        self.event_queue.put(("log", f"Round {round_number}: Random Lines route top→bottom: {route}."))
                    else:
                        column_index = (round_number - 1) % 3
                        active_name = column_names[column_index]
                        x1, x2 = slices[column_index]
                        active_image = boxes_image.crop((x1, 0, x2, boxes_image.height))
                        round_pairs = columns[column_index]
                        expected_pairs = self.column_pairs[column_index] if len(self.column_pairs) == 3 else round_pairs
                        self.event_queue.put(("log", f"Round {round_number}: Across the Line → {active_name} column."))

                        if odds_weighting:
                            if len(round_pairs) == len(expected_pairs):
                                sequence, odds_details, odds_source = odds_weighted_sequence(active_image, round_pairs, max_favorite, mlb_algo)
                                odds_text = "; ".join(
                                    f"{top:+d}/{bottom:+d}={top_p*100:.0f}/{(1-top_p)*100:.0f}"
                                    for top, bottom, top_p in odds_details
                                )
                                self.event_queue.put(("log", f"Round {round_number} {active_name}: odds {odds_source} -> {odds_text}"))
                            else:
                                sequence = secure_sequence(len(round_pairs))
                                self.event_queue.put(("log", f"Round {round_number} {active_name}: pair-count mismatch; using 50/50."))
                        else:
                            sequence = secure_sequence(len(round_pairs))
                        display_sequence = list(sequence)
                else:
                    round_pairs, detect_status = detect_box_pairs(boxes_image, boxes_rect[0], boxes_rect[1])
                    if not round_pairs:
                        round_pairs = self.pairs
                    if odds_weighting:
                        if len(round_pairs) == len(self.pairs):
                            sequence, odds_details, odds_source = odds_weighted_sequence(boxes_image, round_pairs, max_favorite, mlb_algo)
                            odds_text = "; ".join(
                                f"{top:+d}/{bottom:+d}={top_p*100:.0f}/{(1-top_p)*100:.0f}"
                                for top, bottom, top_p in odds_details
                            )
                            self.event_queue.put(("log", f"Round {round_number}: odds {odds_source} -> {odds_text}"))
                        else:
                            sequence = secure_sequence(len(round_pairs))
                            self.event_queue.put(("log", f"Round {round_number}: pair-count mismatch; using 50/50."))
                    else:
                        sequence = secure_sequence(len(round_pairs))
                    display_sequence = list(sequence)

                self.event_queue.put(("round", (round_number, rounds, display_sequence, active_name)))
                wager_baseline = capture_region(wager_rect)
                round_started = time.monotonic()

                if len(round_pairs) == 1:
                    click_times = [round_started]
                else:
                    click_times = [round_started + (box_seconds * i / (len(round_pairs) - 1)) for i in range(len(round_pairs))]

                for index, (pair, result) in enumerate(zip(round_pairs, sequence)):
                    if not self._sleep_until(click_times[index]):
                        stop_reason = "Emergency stop requested during box clicks."
                        break
                    target = pair.top if result == 1 else pair.bottom
                    win_click(target.center_x, target.center_y)
                else:
                    if not self._sleep_until(round_started + wager_at):
                        stop_reason = "Emergency stop requested before WAGER."
                        break

                    wager_image = self._wait_for_region_change(wager_rect, wager_baseline, wait_window)
                    if wager_image is None:
                        stop_reason = "WAGER area did not appear or change. The available wagers may be exhausted."
                        break

                    wx1, wy1, wx2, wy2 = wager_rect
                    local_wager_x, local_wager_y, wager_method = wager_click_point(wager_image)
                    wager_x = wx1 + local_wager_x
                    wager_y = wy1 + local_wager_y
                    win_click(wager_x, wager_y)
                    time.sleep(0.18)
                    win_select_all()
                    time.sleep(0.08)
                    win_type_text(value)
                    self.event_queue.put(("log", f"Round {round_number}: entered {value} using {wager_method}."))

                    if not self._sleep_until(time.monotonic() + accept_after):
                        stop_reason = "Emergency stop requested before green button."
                        break
                    green = self._wait_for_color(wager_rect, "green", wait_window)
                    if green is None:
                        stop_reason = "Green button did not appear in Region 2."
                        break
                    _green_image, (gx1, gy1, gx2, gy2) = green
                    win_click(wx1 + round((gx1 + gx2) / 2), wy1 + round((gy1 + gy2) / 2))
                    accepted_at = time.monotonic()
                    self.event_queue.put(("log", f"Round {round_number}: clicked green button."))

                    if not self._sleep_until(accepted_at + done_after):
                        stop_reason = "Emergency stop requested before Done."
                        break
                    blue = self._wait_for_color(done_rect, "blue", wait_window)
                    if blue is None:
                        stop_reason = "Blue Done button did not appear in Region 3."
                        break
                    _blue_image, (bx1, by1, bx2, by2) = blue
                    dx1, dy1, _dx2, _dy2 = done_rect
                    win_click(dx1 + round((bx1 + bx2) / 2), dy1 + round((by1 + by2) / 2))
                    completed += 1
                    self.event_queue.put(("log", f"Round {round_number}: clicked Done — round complete."))

                    if round_number < rounds and not self._sleep_until(time.monotonic() + round_gap):
                        stop_reason = "Emergency stop requested between rounds."
                        break
                    continue

                break

        except Exception as exc:
            stop_reason = f"Automation error: {exc}"
        self.event_queue.put(("finished", (completed, stop_reason)))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.event_queue.get_nowait()
                if event == "log":
                    self._log(str(payload))
                elif event == "round":
                    round_number, rounds, sequence, active_name = payload  # type: ignore[misc]
                    self.last_sequence = list(sequence)
                    suffix = f" · {active_name}" if active_name != "BOXES" else ""
                    self._show_sequence(self.last_sequence, f"Round {round_number}/{rounds}{suffix}")
                    self.status_label.configure(text=f"Running round {round_number} of {rounds}{suffix}…")
                elif event == "finished":
                    completed, reason = payload  # type: ignore[misc]
                    self._finish_run(int(completed), str(reason))
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _finish_run(self, completed: int, reason: str) -> None:
        self.running = False
        self.stop_event.clear()
        self.deiconify()
        self.lift()
        self.start_button.configure(state="normal")
        self.preview_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_label.configure(text=f"Finished: {completed} round(s). {reason}")
        self._log(f"Finished after {completed} round(s): {reason}")

    def _load_config(self) -> None:
        source_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
        if not source_path.exists():
            return
        try:
            data = json.loads(source_path.read_text(encoding="utf-8"))
            for key in self.regions:
                rect = data.get("regions", {}).get(key)
                if isinstance(rect, list) and len(rect) == 4:
                    self.regions[key] = tuple(int(v) for v in rect)  # type: ignore[assignment]
            pairs_data = data.get("pairs", [])
            pairs: list[BoxPair] = []
            for item in pairs_data:
                top = item.get("top")
                bottom = item.get("bottom")
                if isinstance(top, list) and isinstance(bottom, list) and len(top) == 2 and len(bottom) == 2:
                    pairs.append(BoxPair(BoxTarget(int(top[0]), int(top[1])), BoxTarget(int(bottom[0]), int(bottom[1]))))
            self.pairs = pairs
            settings = data.get("settings", {})
            mapping = {
                "value": self.value_var,
                "rounds": self.rounds_var,
                "box_seconds": self.box_seconds_var,
                "wager_at": self.wager_at_var,
                "accept_after": self.accept_after_var,
                "done_after": self.done_after_var,
                "round_gap": self.round_gap_var,
                "wait_window": self.wait_window_var,
            }
            for key, variable in mapping.items():
                if key in settings:
                    variable.set(str(settings[key]))
            if "odds_weighting" in settings:
                self.odds_weighting_var.set(bool(settings["odds_weighting"]))
            if "mlb_algo" in settings:
                self.mlb_algo_var.set(bool(settings["mlb_algo"]))
            if "max_favorite_percent" in settings:
                self.max_favorite_var.set(str(settings["max_favorite_percent"]))
            if "across_line" in settings:
                self.across_line_var.set(bool(settings["across_line"]))
            elif "down_line" in settings:
                # v1.7 compatibility: old "Down the Line" becomes "Across the Line".
                self.across_line_var.set(bool(settings["down_line"]))
            if "random_lines" in settings:
                self.random_lines_var.set(bool(settings["random_lines"]))
            if self.random_lines_var.get():
                self.across_line_var.set(False)
            column_pairs_data = data.get("column_pairs", [])
            restored_columns: list[list[BoxPair]] = []
            for column in column_pairs_data:
                restored: list[BoxPair] = []
                if isinstance(column, list):
                    for item in column:
                        top = item.get("top") if isinstance(item, dict) else None
                        bottom = item.get("bottom") if isinstance(item, dict) else None
                        if isinstance(top, list) and isinstance(bottom, list) and len(top) == 2 and len(bottom) == 2:
                            restored.append(BoxPair(BoxTarget(int(top[0]), int(top[1])), BoxTarget(int(bottom[0]), int(bottom[1]))))
                restored_columns.append(restored)
            if len(restored_columns) == 3:
                self.column_pairs = restored_columns
                if self._three_column_mode_active() and restored_columns[0]:
                    self.pairs = restored_columns[0]
            if self.pairs:
                self.last_sequence = secure_sequence(len(self.pairs))
                self._show_sequence(self.last_sequence, "Saved preview")
        except Exception:
            pass

    def _save_config(self) -> None:
        try:
            data = {
                "version": APP_VERSION,
                "regions": {key: list(rect) if rect else None for key, rect in self.regions.items()},
                "pairs": [
                    {
                        "top": [pair.top.center_x, pair.top.center_y],
                        "bottom": [pair.bottom.center_x, pair.bottom.center_y],
                    }
                    for pair in self.pairs
                ],
                "column_pairs": [
                    [
                        {
                            "top": [pair.top.center_x, pair.top.center_y],
                            "bottom": [pair.bottom.center_x, pair.bottom.center_y],
                        }
                        for pair in column
                    ]
                    for column in self.column_pairs
                ],
                "settings": {
                    "value": self.value_var.get(),
                    "rounds": self.rounds_var.get(),
                    "box_seconds": self.box_seconds_var.get(),
                    "wager_at": self.wager_at_var.get(),
                    "accept_after": self.accept_after_var.get(),
                    "done_after": self.done_after_var.get(),
                    "round_gap": self.round_gap_var.get(),
                    "wait_window": self.wait_window_var.get(),
                    "odds_weighting": bool(self.odds_weighting_var.get()),
                    "mlb_algo": bool(self.mlb_algo_var.get()),
                    "max_favorite_percent": self.max_favorite_var.get(),
                    "across_line": bool(self.across_line_var.get()),
                    "random_lines": bool(self.random_lines_var.get()),
                },
            }
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _close(self) -> None:
        if self.running:
            self.stop_event.set()
            self.after(250, self.destroy)
        else:
            self._save_config()
            self.destroy()


def main() -> None:
    app = BoxFlipApp()
    app.mainloop()


if __name__ == "__main__":
    main()
