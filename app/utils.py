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
        if dept not in ["CS", "IT"]:
            dept = "CS"
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
    if dept not in ["CS", "IT"]:
        dept = "CS"
        
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

def generate_batch_pdf(start_num, total_yield, batch_id, prefix, start_srn_num=1, department="CS", slot=1, db=None):
    """
    Generates a master batch PDF where each page has:
    1. Control Number stamped at (control_x, control_y)
    2. Sort Record Number (SRN) stamped at (srn_x, srn_y)
    3. Individual Booklet QR Code stamped at (qr_x, qr_y)
    All merged over the front-cover template PDF for the specified department and slot.
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

    for i in range(total_yield):
        ref_num = f"{prefix}-{(start_num + i):04d}"
        srn_str = f"SRN-{(start_srn_num + i):06d}"
        
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