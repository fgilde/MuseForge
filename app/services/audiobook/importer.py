"""Text import → Chapters/Blocks (PLAN §3.3).

Supported: ``.txt``, ``.md`` (stdlib only), ``.docx``, ``.pdf``, ``.epub``.
The last three need libraries MuseForge does not ship yet, so their imports are
**lazy with an actionable message** — the same shape
``postprocessing/voice_clone.py`` uses for the missing SeedVC component.  A
missing library never breaks the module, only the one format that needs it.

    requirements.txt additions needed for the optional formats:
        python-docx==1.1.2   # .docx
        pypdf==5.1.0         # .pdf
        EbookLib==0.18       # .epub   (HTML is stripped with stdlib html.parser,
                             #          so no BeautifulSoup/lxml dependency)

Chapter auto-split recognises Markdown headings (``#``…``######``), setext
headings, and numbered headings in German and English (``Kapitel 3``,
``Chapter IV``, ``Teil 2``, ``Part One``, ``Prolog``/``Epilogue``…).

Self-check: ``python -m services.audiobook.importer`` from ``app/``.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Optional

from services.audiobook.model import Block, Chapter, Run, new_id

SUPPORTED_EXTENSIONS = (".txt", ".md", ".markdown", ".docx", ".pdf", ".epub")

# What each optional format needs, and how to get it.  Kept in one place so the
# error message and the requirements note can never drift apart.
_OPTIONAL_DEPS = {
    ".docx": ("docx", "python-docx", "python-docx==1.1.2"),
    ".pdf": ("pypdf", "pypdf", "pypdf==5.1.0"),
    ".epub": ("ebooklib", "EbookLib", "EbookLib==0.18"),
}


class ImportError_(RuntimeError):
    """Import failed for a reason the user can act on."""


def _missing_dependency(ext: str, exc: Exception) -> ImportError_:
    module, package, pin = _OPTIONAL_DEPS[ext]
    return ImportError_(
        f"Cannot read {ext} files: the '{module}' library is not installed "
        f"({exc}). Add '{pin}' to app/requirements.txt and rebuild the image, "
        f"or install it with 'pip install {package}'. Meanwhile you can import "
        f"the same text as .txt or .md."
    )


# ── Raw text extraction ────────────────────────────────────────────────────


def _read_txt(path: str) -> str:
    # Books arrive as UTF-8, UTF-8-BOM or legacy cp1252; try in that order
    # rather than crashing on a single stray byte.
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _read_docx(path: str) -> str:
    try:
        import docx  # type: ignore
    except Exception as exc:
        raise _missing_dependency(".docx", exc) from exc
    document = docx.Document(path)
    lines = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        style = (getattr(paragraph.style, "name", "") or "").lower()
        # Word headings carry the chapter structure; re-emit them as Markdown
        # so the shared splitter below sees them without a second code path.
        if text and style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            lines.append(f"{'#' * min(6, int(level))} {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def _read_pdf(path: str) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:
        raise _missing_dependency(".pdf", exc) from exc
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return _dehyphenate("\n\n".join(pages))


class _HtmlText(HTMLParser):
    """Minimal HTML → text, enough for EPUB chapter documents.

    Block-level tags become paragraph breaks; headings are re-emitted as
    Markdown so the chapter splitter sees them.  A dedicated HTML library would
    buy nothing here — EPUB content is XHTML and we only want the text.
    """

    _BLOCK = {
        "p", "div", "br", "li", "tr", "section", "article",
        "blockquote", "figure", "figcaption",
    }
    _HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0
        self._heading_level = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._HEADING:
            self.parts.append("\n\n")
            self._heading_level = int(tag[1])
            self.parts.append("#" * self._heading_level + " ")
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._HEADING:
            self._heading_level = 0
            self.parts.append("\n\n")
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self._skipping:
            return
        if self._heading_level:
            # A heading must stay on one line or it stops being a heading.
            data = re.sub(r"\s+", " ", data)
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return parser.text()


def _read_epub(path: str) -> str:
    try:
        import ebooklib  # type: ignore
        from ebooklib import epub  # type: ignore
    except Exception as exc:
        raise _missing_dependency(".epub", exc) from exc
    book = epub.read_epub(path)
    documents = []
    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        try:
            raw = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        text = html_to_text(raw).strip()
        if text:
            documents.append(text)
    # EPUB spine order already separates chapters into documents; a page break
    # between them is a strong chapter hint the splitter can use.
    return "\n\n\n".join(documents)


def extract_text(path: str) -> str:
    """Read any supported document into plain text with blank-line paragraphs."""
    ext = os.path.splitext(path)[1].lower()
    if not os.path.isfile(path):
        raise ImportError_(f"File not found: {path}")
    if ext in (".txt", ".md", ".markdown"):
        return _read_txt(path)
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".pdf":
        return _read_pdf(path)
    if ext == ".epub":
        return _read_epub(path)
    raise ImportError_(
        f"Unsupported file type '{ext or path}'. Supported: "
        + ", ".join(SUPPORTED_EXTENSIONS)
    )


def _dehyphenate(text: str) -> str:
    """Rejoin words a PDF split across a line break ("Kapi-\\ntel" → "Kapitel")."""
    return re.sub(r"(\w)[-‐]\n(\w)", r"\1\2", text)


# ── Paragraph detection ────────────────────────────────────────────────────

_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")


def split_paragraphs(text: str) -> list[str]:
    """Split into paragraphs, preferring blank lines over single newlines.

    Hard-wrapped sources (PDF, plain .txt from Gutenberg) have no blank lines
    inside a paragraph but do have them between paragraphs — so blank lines win
    when present.  When there are none at all, every line is its own paragraph,
    which is the only safe reading.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if _BLANK_LINE_RE.search(normalized):
        chunks = _BLANK_LINE_RE.split(normalized)
        result = []
        for chunk in chunks:
            # Unwrap hard line breaks *inside* a paragraph, but keep Markdown
            # headings and list items on their own lines.
            lines = [line.strip() for line in chunk.split("\n") if line.strip()]
            if not lines:
                continue
            if any(_HEADING_RE.match(line) or _MARKDOWN_HEADING_RE.match(line) for line in lines):
                result.extend(lines)
            else:
                result.append(" ".join(lines))
        return result
    return [line.strip() for line in normalized.split("\n") if line.strip()]


# ── Chapter detection ──────────────────────────────────────────────────────

_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*#*$")

# German + English numbered headings, roman or arabic or spelled-out, with an
# optional subtitle after a separator.  Anchored and length-capped so a
# sentence that merely *starts* with "Chapter" is not mistaken for a heading.
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?P<kind>kapitel|chapter|teil|part|buch|book|abschnitt|section)"
    r"\s+(?P<number>[0-9]{1,3}|[IVXLC]{1,7}|[a-zäöüß]+)"
    r"|(?P<special>prolog|prologue|epilog|epilogue|vorwort|nachwort|"
    r"preface|foreword|afterword|einleitung|introduction|widmung|dedication)"
    r")\s*(?:[:.–—-]\s*(?P<title>.+))?\s*$",
    re.IGNORECASE,
)

# A heading line is short by nature; this rejects prose that happens to match.
_MAX_HEADING_CHARS = 90


def heading_of(line: str) -> Optional[str]:
    """Return the chapter title if ``line`` is a heading, else ``None``.

    The title is the heading text as written — including the number — because
    "Kapitel 7 — Der Fall" is what the user wants to see in the sidebar.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return None
    markdown = _MARKDOWN_HEADING_RE.match(stripped)
    if markdown:
        return markdown.group("title").strip()
    if _HEADING_RE.match(stripped):
        # Reject a "heading" that ends in sentence punctuation — that's prose.
        if stripped.endswith((".", "!", "?")) and not re.search(r"\d\.$", stripped):
            return None
        return stripped
    return None


def split_chapters(paragraphs: list[str]) -> list[tuple[str, list[str]]]:
    """Group paragraphs into ``(title, paragraphs)`` chapters at headings.

    A heading with no body below it (a part title immediately followed by the
    next chapter heading) is folded into the following chapter's title instead
    of producing an empty chapter.
    """
    chapters: list[tuple[str, list[str]]] = []
    current_title = ""
    current: list[str] = []

    for paragraph in paragraphs:
        title = heading_of(paragraph)
        if title is None:
            current.append(paragraph)
            continue
        if current:
            chapters.append((current_title, current))
            current = []
            current_title = title
        else:
            # No body since the last heading → merge the two titles.
            current_title = f"{current_title} — {title}" if current_title else title
    if current:
        chapters.append((current_title, current))
    if not chapters and paragraphs:
        chapters = [("", paragraphs)]
    return chapters


# ── Language heuristic ─────────────────────────────────────────────────────

# Stopword frequency is enough to pick a TTS language code, and it costs no
# dependency.  ponytail: naive word-set scoring; swap in langdetect only if a
# real misdetection shows up (it needs a new pip dependency).
_LANGUAGE_MARKERS = {
    "de": {
        "der", "die", "das", "und", "nicht", "ich", "sie", "ein", "eine", "mit",
        "auf", "war", "sich", "dass", "aber", "als", "auch", "noch", "wie",
        "für", "von", "dem", "den", "hatte", "ist", "es", "zu", "im",
    },
    "en": {
        "the", "and", "was", "that", "with", "his", "her", "for", "not", "you",
        "but", "had", "have", "this", "from", "they", "she", "he", "it", "is",
        "were", "there", "their", "would", "which",
    },
    "fr": {
        "les", "des", "une", "que", "pas", "dans", "pour", "sur", "avec", "est",
        "elle", "qui", "mais", "plus", "tout", "son", "ses", "nous", "vous",
        "était", "avait", "être",
    },
    "es": {
        "los", "las", "que", "con", "por", "para", "una", "como", "pero", "más",
        "sus", "era", "cuando", "muy", "todo", "ella", "está", "esta", "del",
    },
    "it": {
        "che", "non", "per", "con", "una", "sono", "come", "questo", "alla",
        "delle", "degli", "essere", "aveva", "erano", "molto", "anche", "dei",
    },
}

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str, default: str = "en") -> str:
    """Best-guess ISO-639-1 code from stopword hits.  Falls back to ``default``."""
    tokens = [one.lower() for one in _TOKEN_RE.findall(text[:20000])]
    if len(tokens) < 8:
        return default
    scores = {
        code: sum(1 for token in tokens if token in markers)
        for code, markers in _LANGUAGE_MARKERS.items()
    }
    best = max(scores, key=lambda code: scores[code])
    # Require a real signal: 1.5% of tokens matching, and a clear winner.
    if scores[best] < max(3, len(tokens) * 0.015):
        return default
    runner_up = max((s for c, s in scores.items() if c != best), default=0)
    if scores[best] <= runner_up:
        return default
    return best


# ── Assembly ───────────────────────────────────────────────────────────────


def paragraphs_to_blocks(
    paragraphs: list[str], profile_id: Optional[str] = None,
) -> list[Block]:
    """One paragraph = one Block with exactly one Run (the whole paragraph).

    Voice splitting happens later, in the editor or via AI Magic — an importer
    that guessed at runs would only create work to undo.
    """
    blocks = [
        Block(id=new_id(), runs=[Run(id=new_id(), text=text, profile_id=profile_id)])
        for text in paragraphs
        if text.strip()
    ]
    return blocks or [Block(id=new_id(), runs=[Run(id=new_id(), text="")])]


def text_to_chapters(
    text: str,
    *,
    profile_id: Optional[str] = None,
    auto_split: bool = True,
    default_title: str = "Chapter",
) -> list[Chapter]:
    """Full text → Chapters with Blocks/Runs, ready for the data model."""
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return [Chapter(id=new_id(), title=f"{default_title} 1", blocks=paragraphs_to_blocks([]))]
    groups = (
        split_chapters(paragraphs) if auto_split else [("", paragraphs)]
    )
    chapters: list[Chapter] = []
    for index, (title, body) in enumerate(groups, start=1):
        chapters.append(
            Chapter(
                id=new_id(),
                title=title or f"{default_title} {index}",
                blocks=paragraphs_to_blocks(body, profile_id),
                language=detect_language("\n".join(body[:40])),
            )
        )
    return chapters


def import_document(
    path: str,
    *,
    profile_id: Optional[str] = None,
    auto_split: bool = True,
) -> dict:
    """Read a file and return ``{chapters, language, title, word_count}``.

    The endpoint layer turns this into a project via ``store.create_project``;
    keeping the two apart is what lets "Generate a story instead" (PLAN §4.2)
    reuse the extracted text without creating a project.
    """
    text = extract_text(path)
    chapters = text_to_chapters(text, profile_id=profile_id, auto_split=auto_split)
    return {
        "title": os.path.splitext(os.path.basename(path))[0],
        "language": detect_language(text),
        "chapters": chapters,
        "word_count": len(text.split()),
        "source_path": path,
    }


if __name__ == "__main__":
    # Self-check: paragraph detection, heading recognition (both languages),
    # auto-split, language heuristic, HTML stripping, missing-dep message.
    # `python -m services.audiobook.importer` from app/.

    # 1. Blank-line paragraphs win, and hard wraps inside one are unwrapped.
    text = "Erste Zeile\nnoch dieselbe.\n\nZweiter Absatz.\n\n\nDritter."
    assert split_paragraphs(text) == [
        "Erste Zeile noch dieselbe.", "Zweiter Absatz.", "Dritter.",
    ], split_paragraphs(text)

    # 2. Without blank lines every line is a paragraph (never merge blindly).
    assert split_paragraphs("a\nb\nc") == ["a", "b", "c"]

    # 3. Heading recognition.
    assert heading_of("# Der Anfang") == "Der Anfang"
    assert heading_of("### Kapitel 3") == "Kapitel 3"
    assert heading_of("Kapitel 12") == "Kapitel 12"
    assert heading_of("Chapter IV: The Fall") == "Chapter IV: The Fall"
    assert heading_of("Teil 2 — Die Reise") == "Teil 2 — Die Reise"
    assert heading_of("Part One") == "Part One"
    assert heading_of("Prolog") == "Prolog"
    assert heading_of("Epilogue") == "Epilogue"
    # Prose must NOT be mistaken for a heading.
    assert heading_of("Chapter after chapter he read on and on until dawn.") is None
    assert heading_of("Kapitel 3 war das beste, sagte sie.") is None
    assert heading_of("") is None
    assert heading_of("Der Mann ging in das Haus.") is None

    # 4. Auto-split into chapters, keeping heading text as the title.
    book = (
        "# Kapitel 1\n\nEs war einmal ein Haus.\n\nDarin wohnte niemand.\n\n"
        "# Kapitel 2 — Der Gast\n\nDann klopfte es.\n"
    )
    chapters = text_to_chapters(book)
    assert [one.title for one in chapters] == ["Kapitel 1", "Kapitel 2 — Der Gast"], \
        [one.title for one in chapters]
    assert len(chapters[0].blocks) == 2
    assert chapters[0].blocks[0].runs[0].text == "Es war einmal ein Haus."
    assert len(chapters[1].blocks) == 1

    # 5. A part heading directly above a chapter heading folds into the title
    #    instead of producing an empty chapter.
    folded = text_to_chapters("# Teil 1\n\n# Kapitel 1\n\nText hier.\n")
    assert len(folded) == 1, [one.title for one in folded]
    assert folded[0].title == "Teil 1 — Kapitel 1", folded[0].title

    # 6. No headings at all → one chapter with a generated title.
    plain = text_to_chapters("Nur Text.\n\nUnd mehr Text.")
    assert len(plain) == 1 and plain[0].title == "Chapter 1"
    assert len(plain[0].blocks) == 2

    # 7. auto_split=False keeps everything in one chapter.
    assert len(text_to_chapters(book, auto_split=False)) == 1

    # 8. Language heuristic.
    german = (
        "Der Mann ging in das Haus und sah sich um. Es war nicht das erste Mal, "
        "dass er hier war, aber diesmal hatte sich etwas verändert. Die Tür war "
        "offen und im Flur lag ein Brief mit seinem Namen darauf."
    )
    english = (
        "The man walked into the house and looked around. It was not the first "
        "time that he had been there, but this time something had changed. The "
        "door was open and there was a letter with his name on it."
    )
    assert detect_language(german) == "de", detect_language(german)
    assert detect_language(english) == "en", detect_language(english)
    assert detect_language("xyz") == "en"                     # too short → default
    assert detect_language("xyz", default="de") == "de"

    # 9. EPUB/HTML stripping turns headings into Markdown and keeps paragraphs.
    html = (
        "<html><head><title>ignored</title><style>p{}</style></head><body>"
        "<h2>Kapitel 1</h2><p>Erster Absatz.</p><p>Zweiter&nbsp;Absatz.</p>"
        "</body></html>"
    )
    paragraphs = split_paragraphs(html_to_text(html))
    assert paragraphs[0] == "## Kapitel 1", paragraphs
    assert heading_of(paragraphs[0]) == "Kapitel 1"
    assert "Erster Absatz." in paragraphs
    assert not any("ignored" in one for one in paragraphs), paragraphs

    # 10. PDF de-hyphenation.
    assert _dehyphenate("Kapi-\ntel") == "Kapitel"

    # 11. Missing-dependency message names the package and the pin.
    message = str(_missing_dependency(".docx", RuntimeError("no module")))
    assert "python-docx" in message and "requirements.txt" in message, message

    # 12. Unsupported extension is a clear error, not a traceback.
    try:
        extract_text("book.rtf")
    except ImportError_ as exc:
        assert "Unsupported file type" in str(exc) or "not found" in str(exc), exc
    else:
        raise AssertionError("expected ImportError_")

    print("audiobook.importer self-check OK")
