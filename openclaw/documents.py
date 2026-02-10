from __future__ import annotations

from pathlib import Path


def render_text_pdf(path: Path, text: str) -> bool:
    """
    Render plain text into a simple, ATS-friendly PDF.

    Uses reportlab if available. Returns True on success.
    """
    try:
        from reportlab.lib.pagesizes import letter  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet  # type: ignore
        from reportlab.lib.units import inch  # type: ignore
        from reportlab.platypus import Preformatted, SimpleDocTemplate  # type: ignore
    except Exception:
        return False

    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    style = styles["Code"]
    style.fontName = "Courier"
    style.fontSize = 10
    style.leading = 12

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="OpenClaw Document",
        author="OpenClaw",
    )
    doc.build([Preformatted(text, style)])
    return True


def render_cover_letter_pdf(
    path: Path,
    text: str,
    *,
    applicant_name: str = "",
    company: str = "",
    role: str = "",
    email: str = "",
    phone: str = "",
    linkedin: str = "",
    website: str = "",
) -> bool:
    """
    Render a cover letter into an elegant, professionally typeset PDF.
    Uses fpdf2 (pure Python) as primary, reportlab as fallback.
    """
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)

    # Sanitize Unicode for PDF core fonts (replace smart chars with ASCII)
    _unicode_map = {
        "\u2014": ", ", "\u2013": "-",             # em dash -> comma, en dash -> hyphen
        "\u2018": "'", "\u2019": "'",              # smart single quotes
        "\u201c": '"', "\u201d": '"',              # smart double quotes
        "\u2026": "...",                            # ellipsis
        "\u00a0": " ",                              # non-breaking space
        "\u2022": "-",                              # bullet
        "\u2032": "'", "\u2033": '"',              # prime marks
    }
    for uchar, repl in _unicode_map.items():
        text = text.replace(uchar, repl)

    # Try fpdf2 first (pure Python, no system deps)
    try:
        from fpdf import FPDF  # type: ignore

        pdf = FPDF(format="letter")
        # Generous margins: 1 inch left/right, 0.85 inch top, 1 inch bottom
        pdf.set_margins(left=25.4, top=21.6, right=25.4)
        pdf.set_auto_page_break(auto=True, margin=25.4)
        pdf.add_page()

        # Document metadata
        title = f"Cover Letter - {applicant_name}" if applicant_name else "Cover Letter"
        pdf.set_title(title)
        pdf.set_author(applicant_name or "Applicant")

        # ── Header: Candidate name + contact info ──
        if applicant_name:
            pdf.set_font("Helvetica", style="B", size=16)
            pdf.cell(w=0, text=applicant_name, new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.ln(1)

            # Contact line: email | phone | linkedin | website
            contact_parts = []
            if email:
                contact_parts.append(email)
            if phone:
                contact_parts.append(phone)
            if linkedin:
                # Show just the handle, not full URL
                li = linkedin.rstrip("/")
                li_short = li.split("/")[-1] if "/" in li else li
                contact_parts.append(f"linkedin.com/in/{li_short}")
            if website:
                ws = website.replace("https://", "").replace("http://", "").rstrip("/")
                contact_parts.append(ws)

            if contact_parts:
                pdf.set_font("Helvetica", size=9)
                pdf.set_text_color(100, 100, 100)  # Medium gray
                contact_line = "  |  ".join(contact_parts)
                pdf.cell(w=0, text=contact_line, new_x="LMARGIN", new_y="NEXT", align="L")
                pdf.set_text_color(0, 0, 0)  # Reset to black

            # Thin separator line
            pdf.ln(4)
            y = pdf.get_y()
            pdf.set_draw_color(180, 180, 180)
            pdf.set_line_width(0.3)
            pdf.line(25.4, y, 25.4 + pdf.epw, y)
            pdf.ln(8)
        else:
            pdf.ln(6)

        # ── Letter body ──
        paragraphs = text.split("\n\n")

        for i, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue

            lines = para.split("\n")
            for j, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue

                is_greeting = line.lower().startswith("dear ")
                is_closing = (
                    line.lower().startswith("sincerely")
                    or line.lower().startswith("best regards")
                    or line.lower().startswith("respectfully")
                    or line.lower().startswith("warm regards")
                    or line.lower().startswith("thank you")
                )
                is_sign_name = (
                    i == len(paragraphs) - 1
                    and j == len(lines) - 1
                    and len(line.split()) <= 4
                    and not is_greeting
                    and not is_closing
                )

                if is_sign_name:
                    # Signature name: bold, slightly larger
                    pdf.set_font("Helvetica", style="B", size=11)
                    pdf.cell(text=line, new_x="LMARGIN", new_y="NEXT")
                elif is_greeting:
                    # Greeting: regular weight
                    pdf.set_font("Helvetica", size=10.5)
                    pdf.cell(text=line, new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(5)
                elif is_closing:
                    # Closing: add space before, then the line
                    pdf.ln(5)
                    pdf.set_font("Helvetica", size=10.5)
                    pdf.cell(text=line, new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(2)
                else:
                    # Body text: clean, readable
                    pdf.set_font("Helvetica", size=10.5)
                    pdf.multi_cell(w=0, h=5.8, text=line)

            # Paragraph spacing
            if i < len(paragraphs) - 1:
                pdf.ln(3.5)

        pdf.output(str(path))
        return path.exists()
    except Exception as _fpdf_err:
        import logging as _log
        _log.getLogger(__name__).debug("fpdf2 cover letter rendering failed: %s", _fpdf_err, exc_info=True)

    # Fallback: reportlab
    try:
        from reportlab.lib.pagesizes import letter  # type: ignore
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore
        from reportlab.lib.units import inch  # type: ignore
        from reportlab.lib.enums import TA_LEFT  # type: ignore
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer  # type: ignore
        import html as _html
    except Exception:
        return render_text_pdf(path, text)

    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)

    # Define professional styles
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "CoverBody",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=11,
        leading=15,
        alignment=TA_LEFT,
        spaceAfter=10,
    )
    greeting_style = ParagraphStyle(
        "CoverGreeting",
        parent=body_style,
        spaceAfter=14,
    )
    closing_style = ParagraphStyle(
        "CoverClosing",
        parent=body_style,
        spaceBefore=14,
        spaceAfter=4,
    )
    name_style = ParagraphStyle(
        "CoverName",
        parent=body_style,
        fontName="Times-Bold",
        spaceBefore=0,
    )

    title = f"Cover Letter – {applicant_name}" if applicant_name else "Cover Letter"

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=1.0 * inch,
        rightMargin=1.0 * inch,
        topMargin=1.0 * inch,
        bottomMargin=1.0 * inch,
        title=title,
        author=applicant_name or "Applicant",
    )

    # Parse paragraphs from text
    flowables = []
    paragraphs = text.split("\n\n")

    for i, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue

        # Handle multi-line within a paragraph (e.g. "Sincerely,\nName")
        lines = para.split("\n")

        for j, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            # Escape HTML entities for reportlab Paragraph
            safe_line = _html.escape(line)

            # Detect greeting line
            if line.lower().startswith("dear "):
                flowables.append(Paragraph(safe_line, greeting_style))
            # Detect closing line
            elif line.lower().startswith("sincerely") or line.lower().startswith("best regards") or line.lower().startswith("respectfully"):
                flowables.append(Paragraph(safe_line, closing_style))
            # Detect name after closing (bold)
            elif i == len(paragraphs) - 1 and j == len(lines) - 1 and len(line.split()) <= 4:
                flowables.append(Paragraph(safe_line, name_style))
            else:
                flowables.append(Paragraph(safe_line, body_style))

        if i < len(paragraphs) - 1:
            flowables.append(Spacer(1, 4))

    if not flowables:
        return False

    try:
        doc.build(flowables)
        return True
    except Exception:
        # Fallback to simple text PDF
        return render_text_pdf(path, text)

