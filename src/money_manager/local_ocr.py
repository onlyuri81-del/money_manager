"""On-device OCR helpers. Image bytes are processed in memory and never persisted."""
import os
import re
from dataclasses import dataclass
from datetime import date
from io import BytesIO


@dataclass(frozen=True)
class OCRCandidate:
    line: str
    recognized_on: date | None
    amount_krw: int | None


DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})(?!\d)")
KOREAN_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?")
AMOUNT_PATTERN = re.compile(r"(?<![\d,])([+-]?\s*(?:\d{1,3}(?:,\d{3})+|\d{4,}))(?:\s*원)?(?!\d)")


def extract_local_text(image_bytes: bytes) -> str:
    """Run Korean/English Tesseract locally; report missing local prerequisites explicitly."""
    try:
        from PIL import Image, UnidentifiedImageError
        import pytesseract
        from pytesseract import TesseractError, TesseractNotFoundError
    except ImportError as exc:
        raise RuntimeError(
            "로컬 OCR 패키지가 없습니다. `python -m pip install -e \".[ui,ocr]\"`를 실행하세요."
        ) from exc

    executable = os.environ.get("TESSERACT_CMD")
    if executable:
        pytesseract.pytesseract.tesseract_cmd = executable

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            return pytesseract.image_to_string(image, lang="kor+eng")
    except UnidentifiedImageError as exc:
        raise ValueError("이미지 파일을 읽을 수 없습니다. PNG 또는 JPG 파일을 선택하세요.") from exc
    except OSError as exc:
        raise ValueError("이미지 파일을 읽을 수 없습니다. 정상적인 PNG 또는 JPG 파일인지 확인하세요.") from exc
    except TesseractNotFoundError as exc:
        raise RuntimeError(
            "Tesseract 실행 파일을 찾을 수 없습니다. Tesseract와 한국어(kor) 언어 데이터를 설치하세요."
        ) from exc
    except TesseractError as exc:
        raise RuntimeError(
            "Tesseract OCR을 실행하지 못했습니다. 한국어(kor) 언어 데이터 설치를 확인하세요."
        ) from exc


def parse_candidates(text: str) -> tuple[OCRCandidate, ...]:
    """Extract only unambiguous full dates and single amount candidates from OCR lines."""
    candidates = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        date_pattern = DATE_PATTERN if DATE_PATTERN.search(normalized) else KOREAN_DATE_PATTERN
        date_match = date_pattern.search(normalized)
        parsed_date = None
        if date_match:
            try:
                parsed_date = date(*(int(part) for part in date_match.groups()))
            except ValueError:
                parsed_date = None
        without_date = date_pattern.sub(" ", normalized)
        amounts = []
        for match in AMOUNT_PATTERN.finditer(without_date):
            raw = re.sub(r"\s+", "", match.group(1)).replace(",", "")
            try:
                amounts.append(int(raw))
            except ValueError:
                continue
        # A line containing both a transaction amount and a balance is ambiguous.
        parsed_amount = amounts[0] if len(amounts) == 1 else None
        if parsed_date is not None or amounts:
            candidates.append(OCRCandidate(normalized, parsed_date, parsed_amount))
    return tuple(candidates)
