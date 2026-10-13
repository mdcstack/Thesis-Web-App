import io
import os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from pypdf import PdfReader, PdfWriter

def generate_batch_pdf(start_num, total_yield, batch_id, prefix):
    os.makedirs("static/front_page_pdfs", exist_ok=True)
    template_path = "base_template.pdf" 
    output_path = f"static/front_page_pdfs/{batch_id}.pdf"
    
    writer = PdfWriter()
    POS_X = 500
    POS_Y = 400

    for i in range(total_yield):
        ref_num = f"{prefix}-{(start_num + i):04d}"
        
        packet = io.BytesIO()
        can = canvas.Canvas(packet, pagesize=landscape(A4))
        can.setFont("Helvetica-Bold", 18)
        can.setFillColorRGB(0, 0, 0) 
        can.drawCentredString(POS_X, POS_Y, ref_num)
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