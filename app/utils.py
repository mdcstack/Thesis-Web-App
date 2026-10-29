from datetime import datetime
import io
import os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from pypdf import PdfReader, PdfWriter
from .database import SessionLocal
from . import models

def get_active_template_config(db=None, department="CS", slot=1):
    """Retrieves the booklet template layout config for a department (CS or IT) and slot (1, 2, or 3)."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        dept = (department or "CS").upper()
        try:
            valid_progs = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
            if valid_progs and dept not in valid_progs:
                dept = valid_progs[0]
        except Exception:
            pass
        try:
            slot_num = int(slot)
            if slot_num not in [1, 2, 3]:
                slot_num = 1
        except Exception:
            slot_num = 1
            
        config = db.query(models.BookletTemplateConfig).filter(
            models.BookletTemplateConfig.department == dept,
            models.BookletTemplateConfig.slot_number == slot_num
        ).first()
        if not config:
            dept_pdf = f"active_template_{dept}_slot{slot_num}.pdf"
            has_pdf = os.path.exists(f"static/templates/{dept_pdf}")
            # Also check legacy unslotted file for slot 1
            if not has_pdf and slot_num == 1 and os.path.exists(f"static/templates/active_template_{dept}.pdf"):
                dept_pdf = f"active_template_{dept}.pdf"
                has_pdf = True
                
            config = models.BookletTemplateConfig(
                department=dept,
                slot_number=slot_num,
                is_configured=has_pdf,
                template_name=f"Template {slot_num}",
                pdf_filename=dept_pdf if has_pdf else None,
                control_x=500.0,
                control_y=400.0,
                control_font_size=18,
                control_enabled=True,
                srn_x=500.0,
                srn_y=375.0,
                srn_font_size=11,
                srn_enabled=True,
                qr_x=680.0,
                qr_y=360.0,
                qr_size=65.0,
                qr_enabled=True
            )
            db.add(config)
            db.commit()
            db.refresh(config)
        return config
    finally:
        if close_db:
            db.close()

def get_raw_template_pdf_path(department="CS", slot=1, config=None):
    """
    Finds the physical base template PDF for the given department and slot on the local server filesystem,
    regardless of whether it has been officially saved/configured (e.g. for draft workspace canvas and proofing).
    Returns None if no PDF file exists on disk.
    """
    dept = (getattr(config, 'department', None) or department or "CS").upper()
        
    slot_num = getattr(config, 'slot_number', None) or slot or 1
    try:
        slot_num = int(slot_num)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    # Check slot specific path first
    slot_path = f"static/templates/active_template_{dept}_slot{slot_num}.pdf"
    if os.path.exists(slot_path):
        return slot_path

    # Check config.pdf_filename
    if config and config.pdf_filename:
        named_path = f"static/templates/{config.pdf_filename}"
        if os.path.exists(named_path):
            return named_path
            
    # For slot 1 backwards-compatibility, check unslotted path
    if slot_num == 1:
        legacy_path = f"static/templates/active_template_{dept}.pdf"
        if os.path.exists(legacy_path):
            return legacy_path

    return None

def get_template_pdf_path(config=None, department="CS", slot=1):
    """
    Finds the base template PDF for the given department and slot for OFFICIAL booklet batch printing.
    Returns None if the template is not officially saved/configured (is_configured == False) or no PDF exists.
    """
    # If config explicitly indicates unconfigured, do not provide any template for official printing
    if config is not None and not getattr(config, 'is_configured', False):
        return None

    return get_raw_template_pdf_path(department=department, slot=slot, config=config)

def render_template_preview_image(pdf_path=None, output_png_path=None, department="CS", slot=1):
    """
    Renders Page 1 of the booklet cover PDF into a high-resolution PNG image
    to be displayed as the visual background of the interactive calibration canvas and generator preview.
    Returns None if no valid template PDF exists.
    """
    dept = (department or "CS").upper()
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    if output_png_path is None:
        output_png_path = f"static/templates/active_template_{dept}_slot{slot_num}_preview.png"

    os.makedirs(os.path.dirname(output_png_path), exist_ok=True)
    if pdf_path is None or not os.path.exists(pdf_path):
        pdf_path = get_raw_template_pdf_path(department=dept, slot=slot_num)
        
    if not pdf_path or not os.path.exists(pdf_path):
        # Remove stale preview image if any exists
        if os.path.exists(output_png_path):
            try:
                os.remove(output_png_path)
            except Exception:
                pass
        return None

    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import QSize
        from PySide6.QtPdf import QPdfDocument

        app = QGuiApplication.instance()
        if app is None:
            app = QGuiApplication([])

        doc = QPdfDocument()
        doc.load(os.path.abspath(pdf_path))
        if doc.pageCount() > 0:
            size = doc.pagePointSize(0)
            target_width = 1684  # 2x A4 landscape (842 pt)
            target_height = int(target_width * size.height() / size.width()) if size.width() > 0 else 1192
            img = doc.render(0, QSize(target_width, target_height))
            img.save(output_png_path)
            return output_png_path
    except Exception as e:
        print(f"Warning: Failed to render PDF preview with PySide6 for {dept} slot {slot_num}: {e}")
        
    return None

def draw_qr_code(canvas_obj, x, y, size, data):
    """Renders a vector QR code directly onto the ReportLab canvas."""
    widget = qr.QrCodeWidget(data)
    bounds = widget.getBounds()
    w = bounds[2] - bounds[0]
    h = bounds[3] - bounds[1]
    d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
    d.add(widget)
    renderPDF.draw(d, canvas_obj, x, y)

def create_batch_cover_page(batch_id, department, academic_period, total_yield, start_ctrl, end_ctrl, start_srn, end_srn, target_needed=0, created_at=None, program_name=None, ream_count=1):
    """
    Renders Page 1: Official Master Batch Cover Sheet.
    Includes large bold Batch ID, metadata specifications table, and operator directive.
    """
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    dt_str = (created_at or datetime.now()).strftime("%B %d, %Y • %I:%M %p")
    prog_label = program_name or ("Computer Science" if department == "CS" else ("Information Technology" if department == "IT" else department))
    
    # Outer Framing Border
    can.setStrokeColorRGB(0.12, 0.23, 0.38) # Deep navy #1e3a5f
    can.setLineWidth(2.5)
    can.rect(28, 28, width - 56, height - 56, fill=0, stroke=1)
    
    can.setStrokeColorRGB(0.85, 0.90, 0.95)
    can.setLineWidth(1)
    can.rect(33, 33, width - 66, height - 66, fill=0, stroke=1)

    # 1. Header Band
    can.setFillColorRGB(0.94, 0.97, 1.0)
    can.setStrokeColorRGB(0.75, 0.85, 0.95)
    can.rect(36, height - 90, width - 72, 54, fill=1, stroke=1)
    
    can.setFillColorRGB(0.09, 0.18, 0.32)
    can.setFont("Helvetica-Bold", 13)
    can.drawCentredString(width / 2.0, height - 54, "CAVITE STATE UNIVERSITY – IMUS CAMPUS")
    can.setFont("Helvetica", 9)
    can.setFillColorRGB(0.28, 0.38, 0.50)
    can.drawCentredString(width / 2.0, height - 68, "Department of Computer Studies • Examination Logistics & Distribution System")
    can.setFont("Helvetica-Bold", 9.5)
    can.setFillColorRGB(0.15, 0.35, 0.65)
    can.drawCentredString(width / 2.0, height - 82, "MASTER BATCH IDENTIFICATION & PRODUCTION COVER SHEET")

    # 2. Hero Section: Batch ID & Status Indicator (Full-width clean presentation)
    hero_y = height - 215
    can.setFillColorRGB(0.98, 0.99, 1.0)
    can.setStrokeColorRGB(0.80, 0.88, 0.96)
    can.setLineWidth(1)
    can.rect(36, hero_y, width - 72, 115, fill=1, stroke=1)

    # Top Label & Status Pill
    can.setFont("Helvetica-Bold", 10)
    can.setFillColorRGB(0.39, 0.45, 0.55)
    can.drawString(54, hero_y + 90, "OFFICIAL MASTER BATCH IDENTIFIER")

    # Status Pill on the Right
    badge_text = "STAGE IN QR REGISTRATION"
    can.setFont("Helvetica-Bold", 9)
    badge_w = can.stringWidth(badge_text, "Helvetica-Bold", 9) + 20
    badge_x = width - 54 - badge_w
    badge_y = hero_y + 82
    can.setFillColorRGB(0.93, 0.96, 1.0)
    can.setStrokeColorRGB(0.70, 0.82, 0.95)
    can.roundRect(badge_x, badge_y, badge_w, 22, 4, fill=1, stroke=1)
    can.setFillColorRGB(0.12, 0.35, 0.70)
    can.drawCentredString(badge_x + (badge_w / 2.0), badge_y + 6, badge_text)

    # Big Bold Batch ID
    can.setFont("Helvetica-Bold", 34)
    can.setFillColorRGB(0.06, 0.09, 0.16)
    can.drawString(54, hero_y + 48, batch_id)

    can.setFont("Helvetica", 9.5)
    can.setFillColorRGB(0.35, 0.42, 0.52)
    can.drawString(54, hero_y + 24, f"Generated on {dt_str} | Academic Period: {academic_period or 'Active Period'}")

    # 3. Specification Grid (2 Columns)
    grid_y = height - 420
    grid_h = 195
    col_w = (width - 72 - 16) / 2.0
    
    # Left Card: Academic & Batch Scope
    can.setFillColorRGB(1, 1, 1)
    can.setStrokeColorRGB(0.88, 0.91, 0.94)
    can.rect(36, grid_y, col_w, grid_h, fill=1, stroke=1)
    
    can.setFillColorRGB(0.96, 0.97, 0.99)
    can.rect(36, grid_y + grid_h - 26, col_w, 26, fill=1, stroke=0)
    can.setFont("Helvetica-Bold", 10)
    can.setFillColorRGB(0.12, 0.23, 0.38)
    can.drawString(48, grid_y + grid_h - 18, "📋 BATCH PRODUCTION SCOPE")

    rows_left = [
        ("Academic Period:", academic_period or "Current Active Period"),
        ("Program / Course:", f"{prog_label} ({department})"),
        ("Raw Paper Converted:", f"{ream_count} Whole Ream(s) (500 sheets/ream)"),
        ("Total Booklet Yield:", f"{total_yield} Booklets (Sequential Batch)"),
        ("Active Student Quota:", f"{target_needed} Booklets needed for period" if target_needed > 0 else "Full Batch Production"),
    ]
    cur_y = grid_y + grid_h - 52
    for label, val in rows_left:
        can.setFont("Helvetica-Bold", 9)
        can.setFillColorRGB(0.35, 0.42, 0.52)
        can.drawString(48, cur_y, label)
        can.setFont("Helvetica", 9)
        can.setFillColorRGB(0.08, 0.12, 0.20)
        can.drawString(185, cur_y, str(val))
        cur_y -= 28

    # Right Card: Reference Sequences & Quota Layout
    right_x = 36 + col_w + 16
    can.setFillColorRGB(1, 1, 1)
    can.setStrokeColorRGB(0.88, 0.91, 0.94)
    can.rect(right_x, grid_y, col_w, grid_h, fill=1, stroke=1)

    can.setFillColorRGB(0.96, 0.97, 0.99)
    can.rect(right_x, grid_y + grid_h - 26, col_w, 26, fill=1, stroke=0)
    can.setFont("Helvetica-Bold", 10)
    can.setFillColorRGB(0.12, 0.23, 0.38)
    can.drawString(right_x + 12, grid_y + grid_h - 18, "🔢 SERIAL NUMBERS & QUOTA BREAKDOWN")

    has_excess = bool(0 < target_needed < total_yield)
    excess_qty = max(0, total_yield - target_needed)
    
    rows_right = [
        ("Control No Range:", f"{start_ctrl}  ➔  {end_ctrl}"),
        ("Sort Record No (SRN):", f"{start_srn}  ➔  {end_srn}"),
        ("Target Quota Pages:", f"Pages 2 to {target_needed + 1} ({target_needed} Booklets)" if target_needed > 0 else f"Pages 2 to {total_yield + 1}"),
        ("Quota Divider Page:", f"Page {target_needed + 2} (🛑 Warning Divider Sheet)" if has_excess else "None (100% of batch required)"),
        ("Surplus Vault Pages:", f"Pages {target_needed + 3} to {total_yield + 2} ({excess_qty} Booklets)" if has_excess else "0 Booklets (Zero Surplus)"),
    ]
    cur_y = grid_y + grid_h - 52
    for label, val in rows_right:
        can.setFont("Helvetica-Bold", 9)
        can.setFillColorRGB(0.35, 0.42, 0.52)
        can.drawString(right_x + 12, cur_y, label)
        can.setFont("Helvetica", 9)
        if "Warning" in str(val) or "Surplus" in label:
            can.setFillColorRGB(0.70, 0.15, 0.15) if has_excess else can.setFillColorRGB(0.08, 0.12, 0.20)
        else:
            can.setFillColorRGB(0.08, 0.12, 0.20)
        can.drawString(right_x + 155, cur_y, str(val))
        cur_y -= 28

    # 4. Operator Instructions Footer Card
    foot_y = 36
    foot_h = 70
    can.setFillColorRGB(0.99, 0.98, 0.93)
    can.setStrokeColorRGB(0.92, 0.85, 0.65)
    can.rect(36, foot_y, width - 72, foot_h, fill=1, stroke=1)

    can.setFont("Helvetica-Bold", 9.5)
    can.setFillColorRGB(0.55, 0.35, 0.05)
    can.drawString(48, foot_y + foot_h - 18, "⚠️ OPERATOR DIRECTIVE (DO NOT BIND AS A BOOKLET COVER):")

    can.setFont("Helvetica", 8.5)
    can.setFillColorRGB(0.20, 0.25, 0.32)
    can.drawString(48, foot_y + foot_h - 34, "• This sheet is the official batch identification cover. Keep this sheet on top of the physical ream bundle for vault tracking.")
    can.drawString(48, foot_y + foot_h - 48, f"• If printing ONLY the active period quota: Set your printer page range to Pages 2 - {target_needed + 1}." if has_excess else f"• Print full batch (Pages 2 - {total_yield + 1}) to fulfill active semester requirements.")
    can.drawString(48, foot_y + foot_h - 62, "• To activate physical supply packs: Open 'QR Registration' and click '+ Stage' on this Batch ID or enter it in the bundle builder.")

    can.save()
    packet.seek(0)
    return PdfReader(packet).pages[0]

def create_quota_warning_page(batch_id, department, academic_period, total_yield, target_needed, excess_count, fulfilled_start_ctrl, fulfilled_end_ctrl, excess_start_ctrl, excess_end_ctrl, buffer_pct=15.0, program_name=None):
    """
    Renders an unmistakable Warning Divider Sheet inserted after booklet # target_needed.
    Alerts the operator that continuing to print creates surplus stock pre-dated for this exam period.
    """
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    prog_label = program_name or ("Computer Science" if department == "CS" else ("Information Technology" if department == "IT" else department))

    # Outer Warning Border (Crimson Hazard Style)
    can.setStrokeColorRGB(0.86, 0.15, 0.15) # Red #dc2626
    can.setLineWidth(3.5)
    can.rect(28, 28, width - 56, height - 56, fill=0, stroke=1)

    can.setStrokeColorRGB(0.95, 0.70, 0.70)
    can.setLineWidth(1)
    can.rect(33, 33, width - 66, height - 66, fill=0, stroke=1)

    # 1. Top Hazard Banner
    banner_y = height - 120
    banner_h = 84
    can.setFillColorRGB(0.86, 0.15, 0.15) # Crimson red
    can.rect(36, banner_y, width - 72, banner_h, fill=1, stroke=0)

    can.setFillColorRGB(1, 1, 1)
    can.setFont("Helvetica-Bold", 22)
    can.drawCentredString(width / 2.0, banner_y + 50, "🛑  TARGET DEMAND FULFILLED — STOP PRINTING HERE")

    can.setFont("Helvetica-Bold", 11)
    can.setFillColorRGB(1.0, 0.90, 0.90)
    can.drawCentredString(width / 2.0, banner_y + 26, f"ACTIVE PERIOD QUOTA COMPLETE: {target_needed} OF {total_yield} BOOKLETS PRINTED FOR {prog_label.upper()} ({department})")
    can.setFont("Helvetica", 9.5)
    can.drawCentredString(width / 2.0, banner_y + 10, f"Batch Reference: {batch_id} • Academic Period: {academic_period or 'Active Period'}")

    # 2. Middle Explanation & Impact Box
    mid_y = height - 315
    mid_h = 185
    can.setFillColorRGB(0.99, 0.95, 0.95) # Soft pinkish white
    can.setStrokeColorRGB(0.94, 0.75, 0.75)
    can.rect(36, mid_y, width - 72, mid_h, fill=1, stroke=1)

    can.setFont("Helvetica-Bold", 12)
    can.setFillColorRGB(0.60, 0.10, 0.10) # Dark crimson
    can.drawString(54, mid_y + mid_h - 26, "CRITICAL NOTICE TO PRINT OPERATOR & PRODUCTION CLERK:")

    body_lines = [
        f"1. Active Quota Satisfied: The preceding {target_needed} booklet covers fulfill 100% of the enrolled student demand for {prog_label} ({department}).",
        f"2. Safety Margin Included: This target already incorporates the approved {buffer_pct:.0f}% institutional safety buffer for {academic_period}.",
        f"3. Excess Surplus Stock Warning: The remaining {excess_count} booklet cover(s) in this PDF (Booklets #{target_needed + 1} to #{total_yield}) are SURPLUS RESERVE.",
        f"4. Risk of Material Waste: Printing and binding physical booklets beyond this sheet will generate excess stock pre-dated for {academic_period}",
        f"   with allocated Control Numbers and QR codes that might not be distributed, leading to unnecessary paper sheets and staples expenditure.",
        f"5. Recommended Action: STOP PRINTING NOW unless additional vault reserve booklets have been explicitly authorized by the Department Chairperson."
    ]
    cur_y = mid_y + mid_h - 48
    for line in body_lines:
        can.setFont("Helvetica", 9.5)
        can.setFillColorRGB(0.18, 0.20, 0.25)
        can.drawString(54, cur_y, line)
        cur_y -= 22

    # 3. Two Comparison Cards (Fulfilled vs Excess)
    cards_y = height - 440
    cards_h = 115
    col_w = (width - 72 - 16) / 2.0

    # Left: Green Fulfilled Card
    can.setFillColorRGB(0.95, 0.99, 0.96)
    can.setStrokeColorRGB(0.55, 0.85, 0.65)
    can.rect(36, cards_y, col_w, cards_h, fill=1, stroke=1)

    can.setFillColorRGB(0.08, 0.45, 0.22)
    can.setFont("Helvetica-Bold", 10.5)
    can.drawString(50, cards_y + cards_h - 22, f"✅ FULFILLED QUOTA: {target_needed} BOOKLETS (COMPLETED)")
    
    can.setFont("Helvetica-Bold", 9)
    can.setFillColorRGB(0.20, 0.35, 0.25)
    can.drawString(50, cards_y + cards_h - 44, "Control Numbers:")
    can.setFont("Helvetica", 9)
    can.drawString(155, cards_y + cards_h - 44, f"{fulfilled_start_ctrl}  ➔  {fulfilled_end_ctrl}")

    can.setFont("Helvetica-Bold", 9)
    can.drawString(50, cards_y + cards_h - 66, "PDF Page Range:")
    can.setFont("Helvetica", 9)
    can.drawString(155, cards_y + cards_h - 66, f"Pages 2 to {target_needed + 1} ({target_needed} Booklet Covers)")

    can.setFont("Helvetica-Bold", 8.5)
    can.setFillColorRGB(0.10, 0.50, 0.25)
    can.drawString(50, cards_y + 16, "STATUS: Ready for binding, QR activation, and faculty distribution.")

    # Right: Amber Warning Excess Card
    right_x = 36 + col_w + 16
    can.setFillColorRGB(1.0, 0.98, 0.92)
    can.setStrokeColorRGB(0.92, 0.75, 0.45)
    can.rect(right_x, cards_y, col_w, cards_h, fill=1, stroke=1)

    can.setFillColorRGB(0.65, 0.35, 0.05)
    can.setFont("Helvetica-Bold", 10.5)
    can.drawString(right_x + 14, cards_y + cards_h - 22, f"⚠️ EXCESS RESERVE: {excess_count} BOOKLETS (SURPLUS)")

    can.setFont("Helvetica-Bold", 9)
    can.setFillColorRGB(0.45, 0.35, 0.20)
    can.drawString(right_x + 14, cards_y + cards_h - 44, "Control Numbers:")
    can.setFont("Helvetica", 9)
    can.drawString(right_x + 120, cards_y + cards_h - 44, f"{excess_start_ctrl}  ➔  {excess_end_ctrl}")

    can.setFont("Helvetica-Bold", 9)
    can.drawString(right_x + 14, cards_y + cards_h - 66, "PDF Page Range:")
    can.setFont("Helvetica", 9)
    can.drawString(right_x + 120, cards_y + cards_h - 66, f"Pages {target_needed + 3} to {total_yield + 2} ({excess_count} Booklet Covers)")

    can.setFont("Helvetica-Bold", 8.5)
    can.setFillColorRGB(0.70, 0.20, 0.10)
    can.drawString(right_x + 14, cards_y + 16, "STATUS: Unassigned surplus. Avoid printing to prevent paper waste.")

    # 4. Directive Box
    dir_y = 36
    dir_h = 75
    can.setFillColorRGB(0.12, 0.16, 0.22)
    can.rect(36, dir_y, width - 72, dir_h, fill=1, stroke=0)

    can.setFillColorRGB(1, 1, 1)
    can.setFont("Helvetica-Bold", 10)
    can.drawString(50, dir_y + dir_h - 22, "OPERATOR PRINT DIRECTIVE:")

    can.setFont("Helvetica", 9)
    can.setFillColorRGB(0.90, 0.92, 0.95)
    can.drawString(50, dir_y + dir_h - 40, "• To avoid paper waste: STOP PRINTING AT THIS PAGE. Remove this warning sheet from the stack.")
    can.drawString(50, dir_y + dir_h - 58, "• If all pages were printed: Store the excess booklets (following this page) in reserve vault inventory; do NOT issue unless needed.")

    can.save()
    packet.seek(0)
    return PdfReader(packet).pages[0]

def generate_batch_pdf(start_num, total_yield, batch_id, prefix, start_srn_num=1, department="CS", slot=1, db=None, target_needed=0, academic_period=None, buffer_pct=15.0, created_at=None, ream_count=1):
    """
    Generates a master batch PDF where:
    1. Page 1 is an official Master Batch Cover Sheet with Batch ID in bold text & production specifications.
    2. Pages 2 to (total_yield + 1) are Booklet Covers fulfilling the active semester demand.
    """
    os.makedirs("static/front_page_pdfs", exist_ok=True)
    dept = (department or "CS").upper()
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1
        
    config = get_active_template_config(db, department=dept, slot=slot_num)
    template_path = get_template_pdf_path(config, department=dept, slot=slot_num)
    output_path = f"static/front_page_pdfs/{batch_id}.pdf"
    
    writer = PdfWriter()

    # Per-course Control Number prefix: {prefix}-{dept}
    if prefix:
        if prefix.endswith(f"-{dept}") or prefix.endswith(f"_{dept}"):
            course_ctrl_prefix = prefix
        else:
            course_ctrl_prefix = f"{prefix}-{dept}"
    else:
        course_ctrl_prefix = dept

    start_ctrl_str = f"{course_ctrl_prefix}-{start_num:04d}"
    end_ctrl_str = f"{course_ctrl_prefix}-{(start_num + total_yield - 1):04d}"
    start_srn_str = f"SRN-{dept}-{start_srn_num:06d}"
    end_srn_str = f"SRN-{dept}-{(start_srn_num + total_yield - 1):06d}"

    # Resolve program name if db is available
    prog_name = None
    if db:
        try:
            prog_obj = db.query(models.AcademicProgram).filter_by(code=dept).first()
            if prog_obj:
                prog_name = prog_obj.name
        except Exception:
            pass

    # 1. Add Page 1: Master Batch Identification Cover Sheet
    cover_page = create_batch_cover_page(
        batch_id=batch_id,
        department=dept,
        academic_period=academic_period,
        total_yield=total_yield,
        start_ctrl=start_ctrl_str,
        end_ctrl=end_ctrl_str,
        start_srn=start_srn_str,
        end_srn=end_srn_str,
        target_needed=target_needed,
        created_at=created_at or datetime.now(),
        program_name=prog_name,
        ream_count=ream_count
    )
    writer.add_page(cover_page)

    # Pre-generate quota warning divider page if demand does not consume the full batch
    has_warning_page = bool(0 < target_needed < total_yield)
    warning_page = None
    if has_warning_page:
        fulfilled_end_ctrl = f"{course_ctrl_prefix}-{(start_num + target_needed - 1):04d}"
        excess_start_ctrl = f"{course_ctrl_prefix}-{(start_num + target_needed):04d}"
        excess_count = total_yield - target_needed
        warning_page = create_quota_warning_page(
            batch_id=batch_id,
            department=dept,
            academic_period=academic_period,
            total_yield=total_yield,
            target_needed=target_needed,
            excess_count=excess_count,
            fulfilled_start_ctrl=start_ctrl_str,
            fulfilled_end_ctrl=fulfilled_end_ctrl,
            excess_start_ctrl=excess_start_ctrl,
            excess_end_ctrl=end_ctrl_str,
            buffer_pct=buffer_pct,
            program_name=prog_name
        )

    for i in range(total_yield):
        # Insert warning divider sheet right after booklet # target_needed
        if has_warning_page and i == target_needed and warning_page is not None:
            writer.add_page(warning_page)

        ref_num = f"{course_ctrl_prefix}-{(start_num + i):04d}"
        srn_str = f"SRN-{dept}-{(start_srn_num + i):06d}"
        
        packet = io.BytesIO()
        can = canvas.Canvas(packet, pagesize=landscape(A4))
        
        # 1. Control Number
        if config.control_enabled:
            can.setFont("Helvetica-Bold", config.control_font_size)
            can.setFillColorRGB(0, 0, 0)
            can.drawCentredString(config.control_x, config.control_y, ref_num)
            
        # 2. Sort Record Number (SRN)
        if config.srn_enabled:
            can.setFont("Helvetica-Bold", config.srn_font_size)
            can.setFillColorRGB(0.1, 0.1, 0.1)
            can.drawCentredString(config.srn_x, config.srn_y, f"SRN: {srn_str}")
            
        # 3. Individual Booklet QR Code (stamped on front cover)
        if config.qr_enabled:
            draw_qr_code(can, config.qr_x, config.qr_y, config.qr_size, ref_num)
            
        can.save()
        packet.seek(0)
        
        overlay_pdf = PdfReader(packet)
        overlay_page = overlay_pdf.pages[0]
        
        template_reader = PdfReader(template_path)
        fresh_page = template_reader.pages[0]
        
        fresh_page.merge_page(overlay_page, over=True)
        writer.add_page(fresh_page)

    with open(output_path, "wb") as f:
        writer.write(f)
        
    return output_path

def generate_sample_preview_pdf(sample_control_num="1ST26-0001", sample_srn="SRN-000001", department="CS", slot=1, config=None, db=None):
    """
    Generates a 1-page sample preview PDF using active or custom coordinates
    so administrators can test-print a single sheet to verify physical alignment.
    """
    os.makedirs("static/front_page_pdfs", exist_ok=True)
    dept = (getattr(config, 'department', None) or department or "CS").upper()
    slot_num = getattr(config, 'slot_number', None) or slot or 1
    try:
        slot_num = int(slot_num)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1
        
    prefix = "1ST26"
    if db:
        try:
            from . import models
            settings = db.query(models.SystemSetting).first()
            if settings and settings.current_semester_name:
                prefix = settings.current_semester_name
        except Exception:
            pass

    if sample_control_num in ["1ST26-0001", None] or sample_control_num == f"{prefix}-0001":
        sample_control_num = f"{prefix}-{dept}-0001"
    if sample_srn in ["SRN-000001", None]:
        sample_srn = f"SRN-{dept}-000001"
        
    if config is None:
        config = get_active_template_config(db, department=dept, slot=slot_num)
        
    template_path = get_raw_template_pdf_path(department=dept, slot=slot_num, config=config)
    output_path = f"static/front_page_pdfs/template_preview_{dept}_slot{slot_num}.pdf"
    
    writer = PdfWriter()
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=landscape(A4))
    
    # Sample Stamping
    if getattr(config, 'control_enabled', True):
        can.setFont("Helvetica-Bold", getattr(config, 'control_font_size', 18))
        can.setFillColorRGB(0, 0, 0)
        can.drawCentredString(getattr(config, 'control_x', 500.0), getattr(config, 'control_y', 400.0), sample_control_num)
        
    if getattr(config, 'srn_enabled', True):
        can.setFont("Helvetica-Bold", getattr(config, 'srn_font_size', 11))
        can.setFillColorRGB(0.1, 0.1, 0.1)
        can.drawCentredString(getattr(config, 'srn_x', 500.0), getattr(config, 'srn_y', 375.0), f"SRN: {sample_srn}")
        
    if getattr(config, 'qr_enabled', True):
        draw_qr_code(can, getattr(config, 'qr_x', 680.0), getattr(config, 'qr_y', 360.0), getattr(config, 'qr_size', 65.0), sample_control_num)
        
    can.save()
    packet.seek(0)
    
    overlay_pdf = PdfReader(packet)
    overlay_page = overlay_pdf.pages[0]
    
    if template_path and os.path.exists(template_path):
        template_reader = PdfReader(template_path)
        fresh_page = template_reader.pages[0]
        fresh_page.merge_page(overlay_page, over=True)
        writer.add_page(fresh_page)
    else:
        # If no custom background template exists yet, output the stamped overlay on a clean page
        writer.add_page(overlay_page)

    with open(output_path, "wb") as f:
        writer.write(f)
        
    proof_png_path = f"static/front_page_pdfs/template_preview_{dept}_slot{slot_num}_proof.png"
    try:
        render_template_preview_image(pdf_path=output_path, output_png_path=proof_png_path, department=dept, slot=slot_num)
    except Exception as e:
        print(f"Notice: Failed to render preview proof image: {e}")

    return output_path