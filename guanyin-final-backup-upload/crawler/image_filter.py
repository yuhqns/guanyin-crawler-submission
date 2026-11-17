"""Conservative image-quality filter for the Guanyin knowledge crawler.

The filter uses metadata, image geometry, optional OCR, and article context.
It intentionally rejects only high-confidence noise; low relevance alone is
not sufficient to discard an image.
"""
from __future__ import annotations

import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

NOISE_META = re.compile(
    r"(?:^|[\s/_?.=&-])(?:logo|wordmark|favicon|site[-_]?icon|avatar|"
    r"tracking|pixel|sprite|spinner|placeholder|lazyblank|decorative|decoration|"
    r"ornament|separator|divider|social[-_]?icon|menu[-_]?icon|search[-_]?icon)(?:$|[\s/_?.=&-])",
    re.I,
)
AD_META = re.compile(
    r"(?:advert|advertisement|ad[-_]?banner|line[-_]?ad|super[-_]?app|"
    r"sponsor(?:ed)?|newsletter|subscribe|subscription|電子報|免費訂閱|"
    r"立即訂閱|訂閱|廣告|贊助|優惠|促銷|立即購買|限時特惠)", re.I
)
# Match against compacted OCR as well as retaining the original OCR text in the report.
# Tesseract often inserts spaces between individual Traditional Chinese characters.
AD_OCR = re.compile(
    r"(?:免費訂閱|立即訂閱|訂閱電子報|電子報|立即購買|限時特惠|限時優惠|立即下載|"
    r"加入會員|點我訂閱|subscribe|signup|newsletter|shopnow|buynow|"
    r"limitedtime|specialoffer|advertisement)", re.I
)


def _normalize_ocr_for_match(text: str) -> str:
    """Remove OCR-inserted whitespace and punctuation for robust keyword matching."""
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE).casefold()


def _ocr_text(data: bytes) -> str:
    """Run OCR only when both pytesseract and the Tesseract executable exist."""
    if not shutil.which("tesseract"):
        return ""
    try:
        import pytesseract
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            # OCR is intended for banners with visible text, not every photograph.
            if im.width < 240 or im.height < 45 or im.width / max(im.height, 1) < 2.2:
                return ""
            im.thumbnail((1800, 900))
            return pytesseract.image_to_string(im, lang="chi_tra+eng", timeout=8)
    except Exception:
        return ""


def inspect_image(url: str, attrs: dict | None, data: bytes, content_type: str,
                  article_title: str = "", article_content: str = "") -> tuple[bool, str, str]:
    """Return (exclude, reason, OCR text). Reasons are stable for CSV reporting."""
    attrs = attrs or {}
    meta = " ".join([
        unquote(url or ""), str(attrs.get("alt") or ""),
        str(attrs.get("title") or ""), str(attrs.get("class") or ""),
        str(attrs.get("id") or ""), str(attrs.get("role") or ""),
    ])
    if NOISE_META.search(meta):
        return True, "metadata_indicates_logo_or_ui", ""
    if AD_META.search(meta):
        return True, "metadata_indicates_advertisement", ""

    width = height = 0
    is_svg = "svg" in (content_type or "").lower() or urlparse(url).path.lower().endswith(".svg")
    if is_svg:
        # SVG text/attributes can identify icon-like assets without rasterizing them.
        head = data[:200_000].decode("utf-8", errors="ignore")
        if NOISE_META.search(head):
            return True, "svg_markup_indicates_logo_or_decoration", ""
        dims = re.search(r"<svg\b[^>]*(?:width=[\"'](\d+)|height=[\"'](\d+))", head, re.I)
        if dims:
            nums = [int(x) for x in dims.groups() if x]
            if nums and max(nums) <= 64:
                return True, "svg_icon_dimensions", ""
        return False, "", ""

    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            width, height = im.size
    except Exception:
        return False, "", ""

    if width < 90 or height < 70:
        return True, f"image_too_small_{width}x{height}", ""

    ocr = ""
    # Wide, shallow assets are common ad banners; OCR confirms before rejection.
    if width / max(height, 1) >= 4.2 and height <= 320:
        ocr = _ocr_text(data)
        if AD_OCR.search(_normalize_ocr_for_match(ocr)):
            return True, "wide_banner_with_advertising_text", ocr.strip()
        # Metadata/shape alone is not enough to discard a panoramic local photo.

    # An image is not rejected merely because its alt text lacks article keywords.
    # This avoids losing genuine landscape, architecture, and documentary photos.
    return False, "", ocr.strip()
