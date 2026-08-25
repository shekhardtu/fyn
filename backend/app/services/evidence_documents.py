"""Canonical human and machine-readable exports for shared agreements."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Settings
from ..event_time import now_utc
from ..models import DocumentRevision
from .document_assets import read_asset_bytes, revision_assets


def _money(minor: int, currency: str) -> str:
    return f"{currency} {minor / 100:,.2f}"


def _instant(value: Any, timezone_name: str) -> str:
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        localized = instant.astimezone(ZoneInfo(timezone_name)) if instant.tzinfo else instant
        rendered = localized.strftime("%d %b %Y, %I:%M %p").replace(" 0", " ")
        return f"{rendered} · {timezone_name}"
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return str(value)


def agreement_pdf(payload: dict[str, Any]) -> bytes:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.enums import TA_CENTER  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # type: ignore[import-untyped]

    output = io.BytesIO()
    styles = getSampleStyleSheet()
    ink = colors.HexColor("#17221B")
    muted = colors.HexColor("#5D6B61")
    green = colors.HexColor("#176B45")
    line = colors.HexColor("#DCE3DE")
    soft = colors.HexColor("#F3F7F4")
    title = ParagraphStyle("FynTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=21, textColor=ink, alignment=TA_CENTER, spaceAfter=1.5 * mm)
    eyebrow = ParagraphStyle("Eyebrow", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.2, leading=9, textColor=green, alignment=TA_CENTER, spaceAfter=1 * mm)
    heading = ParagraphStyle("Heading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=ink, spaceBefore=2.6 * mm, spaceAfter=1.4 * mm)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.2, leading=11, textColor=ink)
    note = ParagraphStyle("Note", parent=body, fontSize=7.2, leading=9.5, textColor=muted)
    label = ParagraphStyle("Label", parent=note, fontName="Helvetica-Bold", fontSize=6.5, leading=8, textColor=muted)
    tiny = ParagraphStyle("Tiny", parent=note, fontName="Courier", fontSize=5.8, leading=7.2)
    revision = payload["documentRevision"]
    terms = revision["content"].get("terms") or {}
    parties = revision["content"].get("parties") or {}
    currency = str(terms.get("currency") or payload["currency"])

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.line(13 * mm, 12 * mm, A4[0] - 13 * mm, 12 * mm)
        canvas.setFont("Helvetica", 6.8)
        canvas.setFillColor(muted)
        canvas.drawString(13 * mm, 8 * mm, f"Fyn shared record {str(payload['id'])[:8]} | Revision {revision['revisionNumber']}")
        canvas.drawRightString(A4[0] - 13 * mm, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=13 * mm, leftMargin=13 * mm, topMargin=12 * mm, bottomMargin=17 * mm, title="Fyn shared repayment agreement", author="Fyn")
    story: list[Any] = [
        Paragraph("AUTHENTICATED ELECTRONIC ACKNOWLEDGEMENT", eyebrow),
        Paragraph("Shared Repayment Agreement", title),
        Paragraph(f"Agreement {escape(str(payload['id']))} &nbsp; | &nbsp; Revision {revision['revisionNumber']} &nbsp; | &nbsp; {escape(str(revision['state']).replace('_', ' ').title())}", note),
        Spacer(1, 2.5 * mm),
    ]
    legacy_annual_rate = terms.get("annualRateBps")
    rate = int(terms.get("interestRateBps", legacy_annual_rate or 0)) / 100
    period = str(terms.get("interestPeriod") or "yearly")
    mode = str(terms.get("interestMode") or "simple")
    basis = str(terms.get("calculationBasis") or ("actual_365" if legacy_annual_rate is not None else "not_applicable"))
    interest_label = "Interest-free"
    if rate:
        basis_label = "30-day basis" if basis == "fixed_30_day_month" else "actual/365"
        interest_label = f"{rate:g}% {period} {mode} · {basis_label}"
    summary_values = [
        ("LENDER", str(parties.get("lender", ""))),
        ("BORROWER", str(parties.get("borrower", ""))),
        ("PRINCIPAL", _money(int(terms.get("principalMinor", 0)), currency)),
        ("TOTAL REPAYABLE", _money(int(terms.get("totalRepayableMinor", 0)), currency)),
        ("MONEY DATE", str(terms.get("moneyDate", ""))),
        ("RETURN BY", str(terms.get("dueDate", ""))),
        ("INTEREST", interest_label),
        ("INTEREST AMOUNT", _money(int(terms.get("totalInterestMinor", 0)), currency)),
    ]
    summary = Table([
        [Paragraph(escape(item[0]), label) for item in summary_values[:4]],
        [Paragraph(f"<b>{escape(item[1])}</b>", body) for item in summary_values[:4]],
        [Paragraph(escape(item[0]), label) for item in summary_values[4:]],
        [Paragraph(f"<b>{escape(item[1])}</b>", body) for item in summary_values[4:]],
    ], colWidths=[43.5 * mm] * 4)
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), soft), ("BACKGROUND", (0, 2), (-1, 2), soft),
        ("GRID", (0, 0), (-1, -1), 0.45, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.6 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6 * mm),
    ]))
    story.extend([
        summary,
        Paragraph("1. Shared understanding", heading),
        Paragraph(escape(str(revision["content"].get("plainLanguage") or "")), body),
        Paragraph(f"<b>Context:</b> {escape(str(terms.get('note') or 'Not specified'))}", note),
    ])

    assurance_items = revision["content"].get("assuranceItems") or []
    assurance_lines: list[str] = []
    if assurance_items:
        for item in assurance_items:
            assurance_lines.append(f"<b>{escape(str(item.get('kind', '')).replace('_', ' ').title())}</b><br/>{escape(str(item.get('description', '')))}{('<br/>' + escape(str(item.get('maskedIdentifier')))) if item.get('maskedIdentifier') else ''}")
    else:
        assurance_lines.append("No assurance item is recorded in this revision.")

    assets = revision.get("assets") or []
    asset_lines: list[str] = []
    if assets:
        for asset in assets:
            asset_lines.append(f"<b>{escape(asset['originalFilename'])}</b> · {escape(asset['classification'].replace('_', ' ').title())}<br/><font name='Courier' size='5.8'>{escape(asset['sha256'])}</font>")
    else:
        asset_lines.append("No supporting documents are attached to this revision.")
    evidence_columns = Table([
        [Paragraph("2. Assurance items", heading), Paragraph("3. Supporting documents", heading)],
        [Paragraph("<br/>".join(assurance_lines), note), Paragraph("<br/><br/>".join(asset_lines), note)],
    ], colWidths=[87 * mm, 87 * mm])
    evidence_columns.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.45, line), ("INNERGRID", (0, 0), (-1, -1), 0.45, line),
        ("BACKGROUND", (0, 0), (-1, 0), soft), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 * mm),
    ]))
    story.append(evidence_columns)

    acceptances = {item["participantId"]: item for item in revision.get("acceptances") or []}
    signature_cells: list[Paragraph] = []
    for participant in payload.get("participants") or []:
        acceptance = acceptances.get(str(participant["id"])) or acceptances.get(participant["id"])
        if acceptance:
            identity = acceptance.get("actorIdentifierMasked") or "Authenticated Fyn account"
            network = acceptance.get("requestIpHash")
            signature_cells.append(Paragraph(
                f"<b>Electronically acknowledged by {escape(participant['displayName'])}</b><br/>"
                "<font color='#176B45'><b>AUTHENTICATED ELECTRONIC ACKNOWLEDGEMENT</b></font><br/>"
                f"{escape(_instant(acceptance['acceptedAt'], acceptance['actorTimezone']))}<br/>"
                f"{escape(identity)}"
                f"{('<br/>Network fingerprint ' + escape(str(network)[:12]) + '…') if network else ''}",
                note,
            ))
        else:
            signature_cells.append(Paragraph(
                f"<b>{escape(participant['displayName'])}</b><br/>"
                "<font color='#5D6B61'>AWAITING ACKNOWLEDGEMENT</font><br/>"
                f"Revision {revision['revisionNumber']} has not been accepted by this person.",
                note,
            ))
    while len(signature_cells) < 2:
        signature_cells.append(Paragraph("Awaiting participant", note))
    signature_table = Table([signature_cells[:2]], colWidths=[87 * mm, 87 * mm])
    signature_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.65, green), ("INNERGRID", (0, 0), (-1, -1), 0.45, line),
        ("BACKGROUND", (0, 0), (-1, -1), soft), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm),
    ]))
    statement = next((item.get("statementText") for item in revision.get("acceptances") or [] if item.get("statementText")), "I reviewed this exact revision and its supporting documents, and I acknowledge this shared record.")
    acceptance_section: list[Any] = [
        Paragraph("4. Signing through authenticated acknowledgement", heading),
        Paragraph("Each person signs this Fyn record by submitting the following statement against the exact revision and attachment manifest:", note),
        Table([[Paragraph(f"“{escape(statement)}”", body)]], colWidths=[174 * mm], style=TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.65, green), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EDF7F1")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
        ])),
        Spacer(1, 1.5 * mm),
        signature_table,
    ]
    story.append(KeepTogether(acceptance_section))

    story.extend([
        Paragraph("5. Evidence fingerprints", heading),
        Table([
            [Paragraph("CONTENT", label), Paragraph(escape(revision["contentHash"]), tiny)],
            [Paragraph("ATTACHMENT MANIFEST", label), Paragraph(escape(revision["manifestHash"]), tiny)],
            [Paragraph("COMBINED EVIDENCE", label), Paragraph(escape(revision["evidenceHash"]), tiny)],
        ], colWidths=[35 * mm, 139 * mm], style=TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, line), ("BACKGROUND", (0, 0), (0, -1), soft),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm), ("TOPPADDING", (0, 0), (-1, -1), 1.1 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ])),
        Spacer(1, 1.8 * mm),
        Table([[Paragraph(
            "<b>Signature meaning.</b> No handwritten signature or uploaded signature image is required by this workflow. "
            "An accepted card above is an authenticated electronic acknowledgement bound to this exact revision and its file fingerprints. "
            "It is not represented as a certificate-based digital signature or regulated eSign. Fyn does not verify government identity, move money, hold collateral, or decide disputes.",
            note,
        )]], colWidths=[174 * mm], style=TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.45, line), ("BACKGROUND", (0, 0), (-1, -1), soft),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 1.8 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 * mm),
        ])),
    ])
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def evidence_bundle(
    *,
    payload: dict[str, Any],
    revision: DocumentRevision,
    db,
    settings: Settings,
) -> bytes:
    pdf = agreement_pdf(payload)
    assets = revision_assets(db, revision.id)
    exported_at = now_utc().isoformat()
    manifest = {
        "schemaVersion": 1,
        "exportedAt": exported_at,
        "agreement": payload,
        "files": [{"id": str(asset.id), "path": f"attachments/{asset.id}-{asset.original_filename}", "sha256": asset.sha256, "byteSize": asset.byte_size} for asset in assets],
        "agreementPdfSha256": hashlib.sha256(pdf).hexdigest(),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"agreement-{payload['id']}.pdf", pdf)
        archive.writestr("evidence.json", json.dumps(manifest, sort_keys=True, indent=2, default=str, ensure_ascii=True))
        archive.writestr("VERIFY.txt", "Verify each SHA-256 value against the corresponding file. The combined evidence fingerprint shown in the agreement binds the structured terms to the attachment manifest.\n")
        for asset in assets:
            archive.writestr(f"attachments/{asset.id}-{asset.original_filename}", read_asset_bytes(asset, settings))
    return output.getvalue()
