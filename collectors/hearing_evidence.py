"""Bounded official hearing evidence acquisition; no OCR and no ingestion transport."""
from __future__ import annotations

import hashlib
import io
import logging
import re
import warnings
from dataclasses import dataclass
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HEARING_TITLE = "HEARING_TITLE"
HEARING_DESCRIPTION = "HEARING_DESCRIPTION"
DRAFT_NEW_PROVISIONS = "DRAFT_NEW_PROVISIONS"
ATTACHED_CURRENT_LAW = "ATTACHED_CURRENT_LAW"
OTHER_OFFICIAL_ATTACHMENT = "OTHER_OFFICIAL_ATTACHMENT"
EVIDENCE_KINDS = frozenset((HEARING_TITLE, HEARING_DESCRIPTION, DRAFT_NEW_PROVISIONS,
                            ATTACHED_CURRENT_LAW, OTHER_OFFICIAL_ATTACHMENT))
MAX_DOCUMENTS = 2
MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 200
MAX_TEXT_CHARS = 500_000
OFFICIAL_HOSTS = frozenset(("hoeringsportalen.dk", "www.hoeringsportalen.dk",
                            "prodstoragehoeringspo.blob.core.windows.net"))


@dataclass(frozen=True)
class EvidenceFragment:
    kind: str
    text: str
    source_url: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return dict(kind=self.kind, text=self.text, source_url=self.source_url, sha256=self.sha256)


def _official(url: str) -> bool:
    try:
        u = urlsplit(url)
        return (u.scheme == "https" and u.hostname in OFFICIAL_HOSTS
                and not u.username and not u.password and u.port in (None, 443))
    except ValueError:
        return False


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        if not _official(new_url):
            raise ValueError("evidence_redirect_rejected")
        return super().redirect_request(request, file, code, message, headers, new_url)


def fetch_official(url: str) -> tuple[bytes, str]:
    if not _official(url):
        raise ValueError("evidence_url_rejected")
    request = Request(url, headers={"User-Agent": "DenmarkTgBot-HearingEvidence/1.0",
                                    "Accept": "text/html,application/pdf"})
    with build_opener(_OfficialRedirect()).open(request, timeout=15) as response:
        if response.getcode() != 200 or not _official(response.geturl()):
            raise ValueError("evidence_response_rejected")
        data = response.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            raise ValueError("evidence_size_invalid")
        return data, response.headers.get_content_type()


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str | None]] = []
        self.parts: list[str] = []
        self.href: str | None = None
        self.label: list[str] = []
        self.hidden = 0
        self.tables = 0
        self.group: str | None = None
        self.group_row = False
        self.group_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables += 1
        if tag == "tr" and "documentGroup" in dict(attrs).get("class", "").split():
            self.group_row = True
            self.group_parts = []
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden:
            if tag == "a":
                self.href = dict(attrs).get("href")
                self.label = []
            if tag in ("p", "div", "br", "h1", "h2", "h3", "tr", "li"):
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        if tag == "a" and self.href:
            if self.tables:
                self.links.append((self.href, " ".join(self.label), self.group))
            self.href = None
        if tag == "tr" and self.group_row:
            self.group = " ".join(" ".join(self.group_parts).split()).casefold()
            self.group_row = False
        if tag == "table" and self.tables:
            self.tables -= 1
            self.group = None
        if tag in ("p", "div", "h1", "h2", "h3", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.group_row:
                self.group_parts.append(data)
            if self.href:
                self.label.append(data)


def discover_drafts(html: bytes, hearing_url: str) -> list[str]:
    """Select draft links only, never zip bundles, responses, mailing lists or arbitrary hosts."""
    page = _Page()
    page.feed(html.decode("utf-8-sig", errors="strict"))
    result = []
    for href, title, group in page.links:
        if group not in ("høringsdokument", "høringsdokumenter"):
            continue
        url = urljoin(hearing_url, href)
        if (not _official(url) or re.search(r"høringssvar|høringsliste|høringsbrev|oversigt|\.zip(?:\?|$)", title + " " + url, re.I)
                or not re.search(r"forslag|udkast", title, re.I)):
            continue
        if url != hearing_url and url not in result:
            result.append(url)
    if len(result) > MAX_DOCUMENTS:
        raise ValueError("evidence_document_limit")
    return result


_APPENDIX = re.compile(r"^\s*bilag\s+\d+\s*$|lovforslaget\s+sammenholdt\s+med\s+gældende", re.I)
_CURRENT_HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?gældende\s+(?:ret|lov(?:givning)?|formulering)\s*$", re.I)
_PROPOSED_HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:den foreslåede ordning|ministeriets overvejelser|udlændinge.+overvejelser|forslag til|bemærkninger til lovforslaget)\b", re.I)


def _provenance_sections(text: str, url: str, digest: str) -> list[EvidenceFragment]:
    """Heading-based conservative split, not keyword-based cherry-picking."""
    if not re.search(r"forslag|udkast", text[:2000], re.I):
        kind = ATTACHED_CURRENT_LAW if any(_CURRENT_HEADING.fullmatch(line) for line in text.splitlines()[:10]) else OTHER_OFFICIAL_ATTACHMENT
        return [EvidenceFragment(kind, text, url, digest)]
    result = []
    kind = DRAFT_NEW_PROVISIONS
    appendix = False
    lines: list[str] = []
    for line in text.splitlines():
        next_kind = kind
        if _APPENDIX.search(line):
            appendix = True
        if appendix or _CURRENT_HEADING.fullmatch(line):
            next_kind = ATTACHED_CURRENT_LAW
        elif _PROPOSED_HEADING.search(line):
            next_kind = DRAFT_NEW_PROVISIONS
        if next_kind != kind and lines:
            result.append(EvidenceFragment(kind, "\n".join(lines), url, digest))
            lines = []
        kind = next_kind
        lines.append(line)
    if lines:
        result.append(EvidenceFragment(kind, "\n".join(lines), url, digest))
    return result


@contextmanager
def _quiet_pdf_parser():
    # Enrichment is serial. Suppress parser-generated text locally and restore
    # logger settings even on a malformed document; report fixed codes instead.
    parent = logging.getLogger("pypdf")
    loggers = [parent] + [value for name, value in logging.Logger.manager.loggerDict.items()
                          if name.startswith("pypdf.") and isinstance(value, logging.Logger)]
    levels = [(logger, logger.level) for logger in loggers]
    try:
        for logger, _ in levels:
            logger.setLevel(logging.CRITICAL + 1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        for logger, level in levels:
            logger.setLevel(level)


def extract_fragments(data: bytes, content_type: str, url: str) -> list[EvidenceFragment]:
    """Extract actual text; uncertain/non-text drafts never become invented evidence."""
    if len(data) > MAX_BYTES or not _official(url):
        raise ValueError("evidence_response_rejected")
    if content_type == "application/pdf" or data.startswith(b"%PDF-"):
        from pypdf import PdfReader
        # Errors/warnings from a parser must not echo source data into collector logs.
        with _quiet_pdf_parser():
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or not 0 < len(reader.pages) <= MAX_PAGES:
                raise ValueError("evidence_pdf_unavailable")
            pages = [page.extract_text() or "" for page in reader.pages]
        # Image-only pages mean the document cannot be assessed completely without OCR.
        if any(len(re.sub(r"\s", "", text)) < 20 for text in pages):
            raise ValueError("evidence_pdf_nontext")
        text = "\n".join(pages)
    elif content_type in ("text/html", "application/xhtml+xml"):
        page = _Page()
        page.feed(data.decode("utf-8-sig", errors="strict"))
        text = "".join(page.parts)
    else:
        raise ValueError("evidence_nontext")
    if len(text) > MAX_TEXT_CHARS or len(re.sub(r"\s", "", text)) < 40:
        raise ValueError("evidence_text_unavailable")
    digest = hashlib.sha256(data).hexdigest()
    return _provenance_sections(text, url, digest)


def enrich_hearing(record: dict[str, object], *, fetcher=fetch_official) -> dict[str, object]:
    """Per-hearing partial failure; no exception text, source content or identifiers logged."""
    result = dict(record)
    fragments = []
    for kind, key in ((HEARING_TITLE, "title"), (HEARING_DESCRIPTION, "content_text")):
        if isinstance(record.get(key), str):
            fragments.append(EvidenceFragment(kind, record[key], str(record.get("url") or ""), "").to_dict())
    scope = str(record.get("title") or "") + " " + str(record.get("content_text") or "")
    if not re.search(r"ukrain|udlænding|ophold|flygtning|familie|beskyttelse|integration|arbejdstillad", scope, re.I):
        result.update(evidence_fragments=fragments, evidence_status="NOT_NEEDED", evidence_diagnostics=[])
        return result
    status = "UNAVAILABLE"
    codes = []
    url = record.get("url")
    try:
        if (not isinstance(url, str) or not _official(url) or urlsplit(url).hostname not in ("hoeringsportalen.dk", "www.hoeringsportalen.dk")
                or not re.fullmatch(r"/Hearing/Details/\d+/?", urlsplit(url).path)):
            raise ValueError("invalid_hearing_url")
        html, content_type = fetcher(url)
        if content_type not in ("text/html", "application/xhtml+xml") or len(html) > MAX_BYTES:
            raise ValueError("invalid_hearing_response")
        drafts = discover_drafts(html, url)
        if not drafts:
            codes.append("draft_evidence_unavailable")
        successes = 0
        for draft_url in drafts:
            try:
                data, kind = fetcher(draft_url)
                extracted = extract_fragments(data, kind, draft_url)
                fragments.extend(fragment.to_dict() for fragment in extracted)
                if any(f.kind == DRAFT_NEW_PROVISIONS for f in extracted):
                    successes += 1
                else:
                    codes.append("draft_evidence_unavailable")
            except Exception:
                codes.append("draft_evidence_unavailable")
        if successes:
            status = "COMPLETE" if successes == len(drafts) else "PARTIAL"
    except Exception:
        codes.append("hearing_evidence_unavailable")
    result.update(evidence_fragments=fragments, evidence_status=status,
                  evidence_diagnostics=sorted(set(codes)))
    return result
