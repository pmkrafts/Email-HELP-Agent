"""Email parsing: `.eml` (stdlib MIME) and `.txt` (docs-07 format, both separator conventions).

Hardened against everything the sample batches contain:
  - mbox `From ` prefix lines (SpamAssassin-style corpora)
  - raw 8-bit headers (ISO-8859-1 subjects without RFC 2047 encoding)
  - html-only bodies, empty bodies, empty subjects
  - CRLF line endings in `.txt` files
  - future-dated headers (flagged, never trusted for scheduling)
"""
from __future__ import annotations

import email
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from .models import ParsedEmail

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MOJIBAKE = "\ufffd"
# Headers more than this many days ahead are treated as clock skew/anomaly rather than normal
# scheduled-send test data (the labelled batches intentionally date emails a few days ahead).
FUTURE_DATE_TOLERANCE_DAYS = 30


@dataclass
class ParseStats:
    parsed: int = 0
    failed: int = 0


def _decode_raw_header(raw: bytes, charset: str | None) -> str:
    for enc in filter(None, [charset, "utf-8", "cp1252", "latin-1"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fix_raw_headers(subject: str, raw: bytes, charset: str | None) -> tuple[str, bool]:
    """Repair unencoded 8-bit header values that stdlib decoded to U+FFFD."""
    if _MOJIBAKE not in subject:
        return subject, False
    flat = raw.replace(b"\r\n", b"\n")
    for line in flat.split(b"\n")[:40]:
        if line.lower().startswith(b"subject:"):
            decoded = _decode_raw_header(line[len(b"subject:") :].strip(), charset)
            return decoded, True
    return subject, False


def parse_file(path: str | Path, now: datetime | None = None) -> ParsedEmail:
    p = Path(path)
    raw = p.read_bytes()
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []

    if p.suffix.lower() == ".txt":
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        head, sep, body = text.partition("\n---\n")
        if not sep:
            head, sep, body = text.partition("\n\n")
        headers: dict[str, str] = {}
        for line in head.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        if not headers:
            warnings.append("unparsable_txt_headers")
        subject = headers.get("subject", "")
        body_text = body.strip()
        date = None
        if headers.get("date"):
            try:
                date = datetime.fromisoformat(headers["date"]).replace(tzinfo=timezone.utc)
            except ValueError:
                warnings.append("bad_date")
        from_address = headers.get("from", "")
        from_name = None
        message_id = f"txt:{p.name}"
        if not body_text:
            warnings.append("empty_body")
        if not subject.strip():
            warnings.append("empty_subject")
    else:
        if raw.startswith(b"From ") and b":" in raw.split(b"\n", 1)[-1][:200]:
            warnings.append("mbox_prefix_stripped")
            raw = raw.split(b"\n", 1)[1] if b"\n" in raw else raw
        msg = email.message_from_bytes(raw, policy=policy.default)
        charset = msg.get_content_charset()
        subject = str(msg.get("subject") or "")
        subject, fixed = _fix_raw_headers(subject, raw, charset)
        if fixed:
            warnings.append("mojibake_header_fixed")
        from_name_h, from_addr_h = parseaddr(str(msg.get("from") or ""))
        if _MOJIBAKE in from_name_h:
            from_name_h = _decode_raw_header(from_name_h.encode("latin-1", "replace"), charset)
        from_name = from_name_h or None
        from_address = from_addr_h
        to_addresses = [addr for _, addr in getaddresses([str(msg.get("to") or "")]) if addr]
        message_id = str(msg.get("message-id") or f"eml:{p.name}").strip()
        date = None
        if msg.get("date"):
            try:
                date = parsedate_to_datetime(str(msg["date"]))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                warnings.append("bad_date")
        body_part = msg.get_body(preferencelist=("plain",))
        if body_part is not None:
            body_text = body_part.get_content()
        else:
            html_part = msg.get_body(preferencelist=("html",))
            if html_part is not None:
                body_text = _strip_html(html_part.get_content())
                warnings.append("html_only_body")
            else:
                body_text = ""
        body_text = body_text.strip()
        if not body_text:
            warnings.append("empty_body")
        if not subject.strip():
            warnings.append("empty_subject")
        if not from_address:
            warnings.append("missing_from")
        if date is not None and date > now + timedelta(days=FUTURE_DATE_TOLERANCE_DAYS):
            warnings.append("future_date")

    return ParsedEmail(
        message_id=message_id,
        from_address=from_address,
        from_name=from_name,
        to_addresses=to_addresses if p.suffix.lower() != ".txt" else [],
        subject=subject,
        body_text=body_text,
        date=date,
        raw_path=str(p),
        source_format=p.suffix.lower().lstrip("."),
        warnings=warnings,
    )


def parse_dir(directory: str | Path) -> tuple[list[ParsedEmail], list[tuple[str, str]]]:
    """Parse a flat directory or a batch directory containing `emails/`.

    Returns (parsed_emails, failures) where failures are (path, error) pairs.
    """
    d = Path(directory)
    root = d / "emails" if (d / "emails").is_dir() else d
    parsed: list[ParsedEmail] = []
    failures: list[tuple[str, str]] = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in {".eml", ".txt"} or not path.is_file():
            continue
        try:
            parsed.append(parse_file(path))
        except Exception as exc:  # noqa: BLE001 - per-file isolation is the point
            failures.append((str(path), f"{type(exc).__name__}: {exc}"))
    return parsed, failures
