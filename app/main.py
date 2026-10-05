import math
import re
from fastapi import FastAPI, Depends
import bcrypt
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
import datetime
from fastapi.responses import RedirectResponse, HTMLResponse
# Import local modules
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from . import models, schemas
from .database import SessionLocal, engine
from .utils import generate_batch_pdf
import qrcode
import base64
from io import BytesIO
from fastapi import HTTPException
from typing import List
from pydantic import BaseModel
import datetime
from typing import Optional

class LoginRequest(BaseModel):
    username: str
    password: str
class EnrollmentCreate(BaseModel):
    teacher_name: str
    section: str
    subject: str
    student_headcount: int
class DistributionRequest(BaseModel):
    enrollment_id: int
    scanned_pack_qr: str
    bridging_pack_qr: Optional[str] = None  
    clerk_name: str = "System Clerk"
    selected_loose_refs: List[str] = []
# 1. Initialize Database Tables
models.Base.metadata.create_all(bind=engine)


# 2. Initialize the FastAPI App
app = FastAPI(title="BOOKLETLOG System")
# Mount static files (PDFs, CSS) and HTML templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
# 3. Database Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()




@app.get("/api/mrp-status")
def get_mrp_status(db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    
    total_students = sum(e.student_headcount for e in enrollments)
    required_booklets = total_students
    required_sheets = total_students * 6

    required_reams = math.ceil(required_booklets / 83) if required_booklets > 0 else 0
    
    return {
        "total_enrolled_students": total_students,
        "exact_booklets_needed": required_booklets,
        "exact_a4_sheets_needed": required_sheets,
        "minimum_reams_required": required_reams
    }

@app.post("/api/enrollments", response_model=schemas.Enrollment)
def create_enrollment(enrollment: schemas.EnrollmentCreate, db: Session = Depends(get_db)):
    db_enrollment = models.Enrollment(**enrollment.model_dump())
    db.add(db_enrollment)
    db.commit()
    db.refresh(db_enrollment)
    return db_enrollment

# Schema to catch the JSON payload from the frontend
class ReamRequest(BaseModel):
    ream_count: int

import os
import base64

@app.post("/api/save-qr")
def save_qr_code(req: schemas.SaveQRRequest):
    os.makedirs("static/qrcodes", exist_ok=True)
    img_data = req.base64_image.split(",")[1] if "," in req.base64_image else req.base64_image
    file_path = f"static/qrcodes/{req.pack_id}_LABEL.png"
    with open(file_path, "wb") as fh:
        fh.write(base64.b64decode(img_data))
    return {"status": "success", "file_path": f"/{file_path}"}

@app.post("/api/register-ream")
def register_ream(request_data: ReamRequest, db: Session = Depends(get_db)):
    ream_count = request_data.ream_count
    total_yield = ream_count * 83  # Deterministic yield logic
    
    # 1. Find the last generated reference number
    last_booklet = db.query(models.Booklet).order_by(models.Booklet.id.desc()).first()
    start_num = 1
    if last_booklet:
        start_num = int(last_booklet.reference_num.split('-')[1]) + 1
        
    # 2. Generate Master Batch ID
    batch_id = f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    current_period = get_active_period(db)
    
    new_batch = models.MasterBatch(
        qr_code_id=batch_id,
        ream_count=ream_count,
        total_yield=total_yield, 
        start_reference=f"REF-{start_num:04d}",
        end_reference=f"REF-{(start_num + total_yield - 1):04d}",
        registered_period=current_period
    )
    db.add(new_batch)
    db.commit()
    db.refresh(new_batch)
    
    # 3. Log ALL physical booklets dynamically (Marked as pending)
    for i in range(total_yield):
        ref_num = f"REF-{(start_num + i):04d}"
        new_booklet = models.Booklet(
            reference_num=ref_num,
            master_batch_id=new_batch.id,
            status="pending" # Changed from 'available'
        )
        db.add(new_booklet)
    db.commit()
    
    # 4. Trigger PDF Stamping for the total yield
    pdf_path = generate_batch_pdf(start_num, total_yield, batch_id)
    
    return {
        "message": f"{ream_count} Reams successfully converted.",
        "master_batch_id": batch_id,
        "booklets_generated": total_yield,
        "pdf_download_link": f"/{pdf_path}"
    }


@app.get("/dashboard", response_class=HTMLResponse)
def read_dashboard(request: Request, db: Session = Depends(get_db)):
    # 1. Total Enrolled Students (This is the Absolute Demand)
    total_students = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    
    # 2. Total Distributed
    distributed_booklets = db.query(models.Booklet).filter(models.Booklet.status == "distributed").count()
    
    # 3. Available Stock
    available_stock = db.query(models.Booklet).filter(models.Booklet.status == "available").count()
    
    pending_demand = total_students - distributed_booklets
    fulfillment_rate = round((distributed_booklets / total_students * 100)) if total_students > 0 else 0

    analytics = {
        "total_revenue": distributed_booklets * 10,
        "distributed_booklets": distributed_booklets,
        "pending_demand": pending_demand,
        "available_stock": available_stock,
        "total_students": total_students,
        "fulfillment_rate": fulfillment_rate
    }
    
    return templates.TemplateResponse("dashboard.html", {"request": request, "analytics": analytics})


@app.get("/history", response_class=HTMLResponse)
def read_history(request: Request, db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    history_data = []
    
    for enr in enrollments:
        booklets = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == enr.id,
            models.Booklet.status == "distributed"
        ).order_by(models.Booklet.id).all()
        
        if booklets:
            qty = len(booklets)
            
            # FIXED: Changed reference_number to reference_num
            refs = f"{booklets[0].reference_num} ➔ {booklets[-1].reference_num}" if qty > 1 else booklets[0].reference_num
            
            history_data.append({
                "enrollment_id": enr.id,
                "teacher": enr.teacher_name,
                "section": enr.section,
                "subject": enr.subject,
                "qty": qty,
                "amount": qty * 10,
                "refs": refs
            })
    
    return templates.TemplateResponse("history.html", {"request": request, "history_data": history_data})

@app.get("/generator")
def view_generator(request: Request):
    # This page doesn't need database math to load, it just needs the HTML template
    return templates.TemplateResponse("pdf_generator.html", {"request": request})

from fastapi import HTTPException

@app.get("/qr-registration")
def view_qr_registration(request: Request):
    return templates.TemplateResponse("qr_registration.html", {"request": request})

# Updated Schema accepts an array/list of batch IDs
class ActivateRequest(BaseModel):
    batch_ids: List[str]

@app.post("/api/activate-batch")
def activate_batch(request_data: ActivateRequest, db: Session = Depends(get_db)):
    batch_ids = request_data.batch_ids
    if not batch_ids:
        raise HTTPException(status_code=400, detail="No batches provided.")
        
    # 1. Fetch all requested batches
    batches = db.query(models.MasterBatch).filter(models.MasterBatch.qr_code_id.in_(batch_ids)).all()
    
    if len(batches) != len(batch_ids):
        raise HTTPException(status_code=404, detail="One or more Batch IDs were not found in the system.")
        
    for b in batches:
        if b.status == "active":
            raise HTTPException(status_code=400, detail=f"Batch {b.qr_code_id} is already active.")
            
    # 2. Sort batches mathematically by their starting reference number
    def extract_num(ref_str):
        # Converts "REF-0042" into the integer 42
        return int(ref_str.split('-')[1])
        
    batches.sort(key=lambda x: extract_num(x.start_reference))
    
    # 3. Validate Contiguity (Ensure there are no gaps in the reference numbers)
    for i in range(len(batches) - 1):
        current_end = extract_num(batches[i].end_reference)
        next_start = extract_num(batches[i+1].start_reference)
        if next_start != current_end + 1:
            raise HTTPException(
                status_code=400, 
                detail=f"Gap detected! {batches[i].end_reference} does not seamlessly connect to {batches[i+1].start_reference}."
            )
            
    # 4. Generate the unified Supply Pack ID
    pack_id = f"PACK-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    total_booklets = 0
    
    # 5. Update Statuses and assign the Pack ID
    for batch in batches:
        batch.status = "active"
        batch.pack_qr_id = pack_id
        
        booklets = db.query(models.Booklet).filter(models.Booklet.master_batch_id == batch.id).all()
        for booklet in booklets:
            booklet.status = "available"
        total_booklets += len(booklets)
        
    db.commit()
    
    # 6. Generate ONE Master QR Code for the entire Pack
    import qrcode
    import base64
    from io import BytesIO
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(pack_id) # The QR now holds the PACK_ID, not the individual batch ID
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    qr_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    return {
        "message": f"Success! Bundled {len(batches)} batches into active supply.",
        "pack_id": pack_id,
        "total_booklets": total_booklets,
        "qr_code_base64": f"data:image/png;base64,{qr_base64}"
    }

from sqlalchemy import func

# ---------------------------------------------------------
# DISTRIBUTION WORKFLOW ROUTES
# ---------------------------------------------------------

@app.get("/distribution")
def distribution_page(request: Request):
    """Renders the QR scanner and distribution UI"""
    return templates.TemplateResponse("distribution.html", {"request": request})


@app.get("/api/loose-stock-list")
def get_loose_stock_list(db: Session = Depends(get_db)):
    loose = db.query(models.Booklet).filter(models.Booklet.status == "loose_stock").order_by(models.Booklet.reference_num).all()
    return {"status": "success", "data": [{"id": b.id, "reference_num": b.reference_num} for b in loose]}

@app.get("/api/pending-distributions")
def get_pending_distributions(db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    active_period = get_active_period(db) # Get the current period!
    data = []
    
    for e in enrollments:
        # MAGIC: Only count booklets given during THIS specific period
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == e.id,
            models.Booklet.exam_period == active_period
        ).count()
        
        remaining = e.student_headcount - fulfilled_count
        
        if remaining > 0:
            data.append({
                "enrollment_id": e.id,
                "teacher_name": e.teacher_name,
                "section": e.section,   # FIXED: Changed from section_name to section
                "subject": e.subject,   # FIXED: Changed from subject_name to subject
                "headcount": e.student_headcount,
                "fulfilled": fulfilled_count,
                "remaining": remaining
            })
            
    return {"status": "success", "data": data, "active_period": active_period}
    enrollments = db.query(models.Enrollment).all()
    active_period = get_active_period(db) # Get the current period!
    data = []
    
    for e in enrollments:
        # MAGIC: Only count booklets given during THIS specific period
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == e.id,
            models.Booklet.exam_period == active_period
        ).count()
        
        remaining = e.student_headcount - fulfilled_count
        
        if remaining > 0:
            data.append({
                "enrollment_id": e.id,
                "teacher_name": e.teacher_name,
                "section": e.section_name,
                "subject": e.subject_name,
                "headcount": e.student_headcount,
                "fulfilled": fulfilled_count,
                "remaining": remaining
            })
            
    return {"status": "success", "data": data, "active_period": active_period}


@app.get("/api/pack-status/{qr_code}")
def get_pack_status(qr_code: str, db: Session = Depends(get_db)):
    """Checks the scanned QR code and returns how many booklets are available inside."""
    batches = db.query(models.MasterBatch).filter(
        (models.MasterBatch.qr_code_id == qr_code) |
        (models.MasterBatch.pack_qr_id == qr_code)
    ).all()

    if not batches:
        return {"status": "error", "message": "Invalid QR Code. Pack not found."}
    
    batch_ids = [b.id for b in batches]
    available_count = db.query(models.Booklet).filter(
        models.Booklet.master_batch_id.in_(batch_ids),
        models.Booklet.status == "available"
    ).count()

    return {"status": "success", "qr_code": qr_code, "available": available_count}


    
@app.get("/", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
def read_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

import uuid

@app.post("/api/login")
def login_user(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()

    if not user or not bcrypt.checkpw(req.password.encode('utf-8'), user.password.encode('utf-8')):
        return {"status": "error", "message": "Invalid username or password."}
    
    new_token = str(uuid.uuid4())
    user.session_token = new_token
    db.commit()
    
    # Return user details so the browser knows what role they are
    return {
        "status": "success", 
        "user": {
            "username": user.username,
            "role": user.role,
            "department": user.department,
            "session_token": new_token
        }
    }

@app.post("/api/verify-session")
def verify_session(req: schemas.VerifySessionRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()
    if user and user.session_token == req.session_token:
        return {"status": "valid"}
    return {"status": "invalid"}

@app.post("/api/logout")
def logout_session(req: schemas.LogoutRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()
    if user:
        user.session_token = None
        db.commit()
    return {"status": "success"}

@app.get("/api/dss-alert")
def check_dss_alert(db: Session = Depends(get_db)):
    # 1. Calculate pending demand (Students who haven't received booklets yet)
    total_students = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    distributed = db.query(models.Booklet).filter(models.Booklet.status == "distributed").count()
    pending_demand = max(0, total_students - distributed)
    
    # 2. Calculate available physical stock
    available_stock = db.query(models.Booklet).filter(models.Booklet.status == "available").count()
    
    # 3. Decision Support Logic (Is there a deficit?)
    if available_stock < pending_demand:
        deficit_booklets = pending_demand - available_stock
        reams_to_print = math.ceil(deficit_booklets / 83) # The strict 83-yield constraint
        
        return {
            "alert": True, 
            "message": f"You have an inventory deficit of {deficit_booklets} booklets.",
            "reams_needed": reams_to_print
        }
        
    return {"alert": False}    

@app.get("/enrollments", response_class=HTMLResponse)
def read_enrollments(request: Request, db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    return templates.TemplateResponse("enrollments.html", {"request": request, "enrollments": enrollments})

@app.post("/api/enrollments")
def create_enrollment(enr: EnrollmentCreate, db: Session = Depends(get_db)):
    new_enr = models.Enrollment(
        teacher_name=enr.teacher_name.strip(),
        section=enr.section.strip().upper(), # Forces formats like BSCS-1E to uppercase
        subject=enr.subject.strip().upper(),
        student_headcount=enr.student_headcount
    )
    db.add(new_enr)
    db.commit()
    return {"status": "success", "message": "Class record added successfully!"}

@app.delete("/api/enrollments/{enr_id}")
def delete_enrollment(enr_id: int, db: Session = Depends(get_db)):
    enr = db.query(models.Enrollment).filter(models.Enrollment.id == enr_id).first()
    if enr:
        db.delete(enr)
        db.commit()
        return {"status": "success"}
    return {"status": "error", "message": "Record not found"}

@app.put("/api/enrollments/{enr_id}")
def update_enrollment(enr_id: int, enr: EnrollmentCreate, db: Session = Depends(get_db)):
    # 1. Find the existing record
    db_enr = db.query(models.Enrollment).filter(models.Enrollment.id == enr_id).first()
    
    if db_enr:
        # 2. Overwrite the old data with the new data from the form
        db_enr.teacher_name = enr.teacher_name.strip()
        db_enr.section = enr.section.strip().upper()
        db_enr.subject = enr.subject.strip().upper()
        db_enr.student_headcount = enr.student_headcount
        
        db.commit()
        return {"status": "success", "message": "Class updated successfully!"}
        
    return {"status": "error", "message": "Record not found"}


from typing import Optional
from pydantic import BaseModel
from datetime import datetime

@app.get("/api/public-receipts")
def get_public_receipts(db: Session = Depends(get_db)):
    try:
        enrollments = db.query(models.Enrollment).all()
        receipts_data = []
        
        for enr in enrollments:
            # Group by period just like we do in the History route!
            periods = db.query(models.Booklet.exam_period).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).distinct().all()

            for (period,) in periods:
                booklets = db.query(models.Booklet).filter(
                    models.Booklet.enrollment_id == enr.id,
                    models.Booklet.status == "distributed",
                    models.Booklet.exam_period == period
                ).order_by(models.Booklet.id).all()
                
                if booklets:
                    qty = len(booklets)
                    staff_name = booklets[0].distributed_by if booklets[0].distributed_by else "System Admin"
                    
                    dist_date = getattr(booklets[0], 'date_distributed', None)
                    if dist_date:
                        date_str = dist_date.strftime("%d-%b-%Y %I:%M %p")
                    else:
                        date_str = datetime.now().strftime("%d-%b-%Y %I:%M %p")

                    # Generate the detailed log string for the receipt
                    blocks = []
                    current_block = [booklets[0]]
                    for i in range(1, len(booklets)):
                        prev = booklets[i-1]
                        curr = booklets[i]
                        if curr.id == prev.id + 1 and getattr(curr, 'master_batch_id', None) == getattr(prev, 'master_batch_id', None):
                            current_block.append(curr)
                        else:
                            blocks.append(current_block)
                            current_block = [curr]
                    blocks.append(current_block)

                    log_strings = []
                    for blk in blocks:
                        f_ref = getattr(blk[0], 'reference_num', getattr(blk[0], 'reference_number', ''))
                        l_ref = getattr(blk[-1], 'reference_num', getattr(blk[-1], 'reference_number', ''))
                        batch_id = getattr(blk[0], 'master_batch_id', None)
                        
                        trace_str = ""
                        if batch_id:
                            batch = db.query(models.MasterBatch).filter(models.MasterBatch.id == batch_id).first()
                            if batch:
                                pack_str = f" | Pack: {batch.pack_qr_id}" if batch.pack_qr_id else " | (Loose/Unpacked)"
                                trace_str = f"<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#7f8c8d; font-size:0.85em;'>[Batch: {batch.qr_code_id}{pack_str}]</span>"
                        
                        if len(blk) > 1: 
                            log_strings.append(f"Ref: #{f_ref} to #{l_ref} ({len(blk)} pcs){trace_str}")
                        else: 
                            log_strings.append(f"Ref: #{f_ref} (1 pc){trace_str}")

                    receipts_data.append({
                        "enrollment_id": enr.id,
                        "date": date_str,
                        "teacher": enr.teacher_name,
                        "section": enr.section,
                        "subject": enr.subject,
                        "qty": qty,
                        "amount": qty * 10,
                        "staff": staff_name,
                        "log": log_strings,
                        "exam_period": period if period else "Legacy (No Period)" # NEW: Added Period
                    })
        
        return {"status": "success", "data": receipts_data}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}
    

    # --- PUBLIC RECEIPTS PORTAL ROUTE ---
@app.get("/receipts", response_class=HTMLResponse)
def public_receipts_portal(request: Request):
    return templates.TemplateResponse("receipts.html", {"request": request})

# --- LOOSE STOCK / RETURN ROUTES ---

@app.get("/api/distributed-booklets/{enrollment_id}")
def get_distributed_booklets(enrollment_id: int, period: str = None, db: Session = Depends(get_db)):
    """Fetches booklets for a specific class AND specific exam period."""
    try:
        # Start the query by finding the right class and distributed booklets
        query = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == enrollment_id,
            models.Booklet.status == "distributed"
        )
        
        # MAGIC FIX: Filter down to the EXACT exam period clicked
        if period:
            if period == "Legacy (No Period)":
                # Handle old booklets that didn't have a stamped period
                query = query.filter((models.Booklet.exam_period == None) | (models.Booklet.exam_period == ""))
            else:
                query = query.filter(models.Booklet.exam_period == period)
                
        booklets = query.order_by(models.Booklet.id).all()
        
        # Safely extract reference numbers
        data = []
        for b in booklets:
            ref = getattr(b, 'reference_num', getattr(b, 'reference_number', ''))
            data.append({"id": b.id, "ref": ref})
            
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}

@app.post("/api/process-return")
def process_return(req: schemas.ReturnRequest, db: Session = Depends(get_db)):
    """Handles returns differently based on whether the booklet is usable or defective."""
    try:
        booklets = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == req.enrollment_id,
            models.Booklet.reference_num.in_(req.reference_numbers)
        ).all()
        
        if not booklets:
            return {"status": "error", "message": "No matching booklets found to return."}
            
        return_qty = len(booklets)
        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == req.enrollment_id).first()
        is_defective = req.reason == "Misprint / Defect"

        if enrollment and not is_defective:
            enrollment.student_headcount = max(0, enrollment.student_headcount - return_qty)

        for b in booklets:
            b.return_reason = req.reason
            if is_defective:
                b.status = "voided"
                # Keep enrollment_id intact for department defect analytics
            else:
                b.status = "loose_stock"
                b.enrollment_id = None 
                b.date_distributed = None
                b.distributed_by = None
            
        db.commit()
        
        if is_defective:
            return {"status": "success", "message": f"Voided {return_qty} defective booklet(s). This section now requires a replacement."}
        else:
            return {"status": "success", "message": f"Successfully moved {return_qty} booklet(s) to Loose Stock."}
        
    except Exception as e:
        print(f"Error in /api/process-return: {str(e)}")
        return {"status": "error", "message": str(e)}
    
    
# --- 1. PREVIEW ROUTE (DRY RUN) ---
@app.post("/api/preview-distribution")
def preview_distribution(req: schemas.DistributionRequest, db: Session = Depends(get_db)):
    try:
        # 1. Fetch the active period
        active_period = get_active_period(db)
        
        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == req.enrollment_id).first()
        if not enrollment: return {"status": "error", "message": "Section not found."}

        # 2. FILTER DEMAND BY ACTIVE PERIOD ONLY
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == req.enrollment_id,
            models.Booklet.exam_period == active_period
        ).count()
        
        remaining_demand = enrollment.student_headcount - fulfilled_count
        if remaining_demand <= 0: return {"status": "error", "message": f"Section already fulfilled for {active_period}."}

        # Verify Primary Pack
        primary_batches = db.query(models.MasterBatch).filter(
            (models.MasterBatch.qr_code_id == req.scanned_pack_qr) |
            (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)
        ).all()
        if not primary_batches: return {"status": "error", "message": "Invalid Primary Pack QR."}
        primary_batch_ids = [b.id for b in primary_batches]

        # Verify Bridging Pack (If provided)
        bridging_batch_ids = []
        if req.bridging_pack_qr:
            bridging_batches = db.query(models.MasterBatch).filter(
                (models.MasterBatch.qr_code_id == req.bridging_pack_qr) |
                (models.MasterBatch.pack_qr_id == req.bridging_pack_qr)
            ).all()
            if not bridging_batches: return {"status": "error", "message": "Invalid Bridging Pack QR."}
            bridging_batch_ids = [b.id for b in bridging_batches]

        # --- THE TRIPLE-POOL ALGORITHM (Preview) ---
        booklets_to_distribute = []
        still_needed = remaining_demand

        # Pool 1: Loose Stock
        if req.selected_loose_refs:
            loose = db.query(models.Booklet).filter(
                models.Booklet.reference_num.in_(req.selected_loose_refs),
                models.Booklet.status == "loose_stock"
            ).all()
            if loose:
                booklets_to_distribute.extend(loose)
                still_needed -= len(loose)

        # Pool 2: Primary Pack
        if still_needed > 0:
            primary = db.query(models.Booklet).filter(
                models.Booklet.master_batch_id.in_(primary_batch_ids), 
                models.Booklet.status == "available"
            ).order_by(models.Booklet.reference_num).limit(still_needed).all()
            if primary:
                booklets_to_distribute.extend(primary)
                still_needed -= len(primary)

        # Pool 3: Bridging Pack
        if still_needed > 0 and bridging_batch_ids:
            bridging = db.query(models.Booklet).filter(
                models.Booklet.master_batch_id.in_(bridging_batch_ids), 
                models.Booklet.status == "available"
            ).order_by(models.Booklet.reference_num).limit(still_needed).all()
            if bridging:
                booklets_to_distribute.extend(bridging)
                still_needed -= len(bridging)

        if still_needed > 0:
            return {"status": "error", "message": f"Insufficient stock even with bridging. Short by {still_needed} pcs."}

        if not booklets_to_distribute:
            return {"status": "error", "message": "No booklets could be allocated."}

        # Generate Chunked Audit Log (with formatting)
        blocks = []
        current_block = [booklets_to_distribute[0]]
        for i in range(1, len(booklets_to_distribute)):
            prev = booklets_to_distribute[i-1]
            curr = booklets_to_distribute[i]
            if curr.id == prev.id + 1 and getattr(curr, 'master_batch_id', None) == getattr(prev, 'master_batch_id', None):
                current_block.append(curr)
            else:
                blocks.append(current_block)
                current_block = [curr]
        blocks.append(current_block)

        log_strings = []
        for blk in blocks:
            f_ref = getattr(blk[0], 'reference_num', getattr(blk[0], 'reference_number', ''))
            l_ref = getattr(blk[-1], 'reference_num', getattr(blk[-1], 'reference_number', ''))
            batch_id = getattr(blk[0], 'master_batch_id', None)
            
            trace_str = ""
            if batch_id:
                batch = db.query(models.MasterBatch).filter(models.MasterBatch.id == batch_id).first()
                if batch:
                    pack_str = f" | Pack: {batch.pack_qr_id}" if batch.pack_qr_id else " | (Loose/Unpacked)"
                    trace_str = f"<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#7f8c8d; font-size:0.85em;'>[Batch: {batch.qr_code_id}{pack_str}]</span>"
            
            if len(blk) > 1: 
                log_strings.append(f"Ref: #{f_ref} to #{l_ref} ({len(blk)} pcs){trace_str}")
            else: 
                log_strings.append(f"Ref: #{f_ref} (1 pc){trace_str}")
                
        chunk_log_str = "<br>&nbsp;&nbsp;&nbsp;&nbsp;".join(log_strings)
        return {"status": "success", "preview_log": chunk_log_str}
        
    except Exception as e:
        print(f"CRITICAL ERROR IN PREVIEW: {str(e)}")
        return {"status": "error", "message": str(e)}
    
    
# --- 2. EXECUTION ROUTE (SINGLE-SAVE) ---
@app.post("/api/execute-distribution")
def execute_distribution(req: schemas.DistributionRequest, db: Session = Depends(get_db)):
    try:
        # 1. Fetch the active period
        active_period = get_active_period(db)

        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == req.enrollment_id).first()
        if not enrollment:
            return {"status": "error", "message": "Section not found."}

        # 2. FILTER DEMAND BY ACTIVE PERIOD ONLY
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == req.enrollment_id,
            models.Booklet.exam_period == active_period
        ).count()
        
        remaining_demand = enrollment.student_headcount - fulfilled_count
        if remaining_demand <= 0:
            return {"status": "error", "message": f"Section already fulfilled for {active_period}."}

        primary_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.scanned_pack_qr) | (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)).all()
        primary_batch_ids = [b.id for b in primary_batches]

        bridging_batch_ids = []
        if req.bridging_pack_qr:
            bridging_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.bridging_pack_qr) | (models.MasterBatch.pack_qr_id == req.bridging_pack_qr)).all()
            bridging_batch_ids = [b.id for b in bridging_batches]

        # --- THE TRIPLE-POOL ALGORITHM (Execution) ---
        booklets_to_distribute = []
        still_needed = remaining_demand

        if req.selected_loose_refs:
            loose = db.query(models.Booklet).filter(
                models.Booklet.reference_num.in_(req.selected_loose_refs),
                models.Booklet.status == "loose_stock"
            ).all()
            booklets_to_distribute.extend(loose)
            still_needed -= len(loose)

        if still_needed > 0:
            primary = db.query(models.Booklet).filter(models.Booklet.master_batch_id.in_(primary_batch_ids), models.Booklet.status == "available").order_by(models.Booklet.reference_num).limit(still_needed).all()
            booklets_to_distribute.extend(primary)
            still_needed -= len(primary)

        if still_needed > 0 and bridging_batch_ids:
            bridging = db.query(models.Booklet).filter(models.Booklet.master_batch_id.in_(bridging_batch_ids), models.Booklet.status == "available").order_by(models.Booklet.reference_num).limit(still_needed).all()
            booklets_to_distribute.extend(bridging)

        # 4. Bind the booklets to the teacher permanently and stamp the period
        for b in booklets_to_distribute:
            b.status = "distributed"
            b.enrollment_id = req.enrollment_id
            b.date_distributed = datetime.now()
            b.distributed_by = req.clerk_name
            b.exam_period = active_period

        db.commit() # The Single-Save!
        
        return {"status": "success", "message": "Transaction perfectly recorded."}
    except Exception as e:
        print(f"CRITICAL ERROR IN EXECUTION: {str(e)}")
        return {"status": "error", "message": str(e)}

# Update this specific function in main.py
@app.get("/api/history-data")
def get_history_data(db: Session = Depends(get_db)):
    try:
        enrollments = db.query(models.Enrollment).all()
        active_period = get_active_period(db)
        history_data = []
        
        for enr in enrollments:
            periods = db.query(models.Booklet.exam_period).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).distinct().all()

            for (period,) in periods:
                booklets = db.query(models.Booklet).filter(
                    models.Booklet.enrollment_id == enr.id,
                    models.Booklet.status == "distributed",
                    models.Booklet.exam_period == period
                ).order_by(models.Booklet.id).all()
                
                if booklets:
                    qty = len(booklets)
                    refs = f"{booklets[0].reference_num} ➔ {booklets[-1].reference_num}" if qty > 1 else booklets[0].reference_num
                    
                    dist_date = getattr(booklets[0], 'date_distributed', None)
                    if dist_date:
                        date_str = dist_date.strftime("%d-%b-%Y %I:%M %p")
                        # SECURE PYTHON MATH: Calculate exact days old
                        days_old = (datetime.now() - dist_date).days
                    else:
                        date_str = datetime.now().strftime("%d-%b-%Y %I:%M %p")
                        days_old = 0

                    history_data.append({
                        "enrollment_id": enr.id,
                        "date": date_str,
                        "teacher": enr.teacher_name,
                        "section": enr.section,
                        "subject": enr.subject,
                        "qty": qty,
                        "amount": qty * 10,
                        "refs": refs,
                        "exam_period": period if period else "Legacy (No Period)",
                        "days_old": days_old  # <--- THIS WAS MISSING
                    })
        
        return {"status": "success", "data": history_data, "active_period": active_period}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}

@app.get("/history", response_class=HTMLResponse)
def read_history_page(request: Request):
    return templates.TemplateResponse("history.html", {"request": request})

@app.get("/api/search-transaction")
def search_transaction(ref: str, db: Session = Depends(get_db)):
    """Finds exactly which transaction (Class + Exam Period) owns a specific booklet."""
    try:
        booklets = db.query(models.Booklet).filter(
            models.Booklet.reference_num.like(f"%{ref.strip()}%"),
            models.Booklet.enrollment_id.isnot(None)
        ).all()
        
        # MAGIC FIX: Combine enrollment_id AND exam_period to create a 100% unique key
        matched_keys = list(set([
            f"{b.enrollment_id}|{b.exam_period if b.exam_period else 'Legacy (No Period)'}" 
            for b in booklets
        ]))
        
        return {"status": "success", "matched_keys": matched_keys}
    except Exception as e:
        print(f"Search Error: {str(e)}")
        return {"status": "error", "message": str(e)}
    
def get_active_period(db: Session) -> str:
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings(active_period="UNSET")
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings.active_period

# Add an endpoint so the frontend can read/change it
@app.get("/api/active-period")
def api_get_active_period(db: Session = Depends(get_db)):
    return {"status": "success", "active_period": get_active_period(db)}

@app.post("/api/active-period")
def api_set_active_period(period: dict, db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings(active_period=period.get("active_period", "1st Sem - Midterms"))
        db.add(settings)
    else:
        settings.active_period = period.get("active_period")
    db.commit()
    return {"status": "success", "message": f"Period changed to {settings.active_period}"}




# --- DASHBOARD ANALYTICS ROUTE ---
@app.get("/api/dashboard-stats")
def get_dashboard_stats(period: str = None, db: Session = Depends(get_db)):
    """Calculates KPIs, financial revenue, charts, LIVE INVENTORY, and Year Level Data."""
    try:
        active_period = period if period else get_active_period(db)
        
        # Fetch issued booklets for the period
        issued_booklets = db.query(models.Booklet).filter(
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "distributed"
        ).all()
        
        total_issued = len(issued_booklets)
        
        # 1. Math for Raw Reams Converted (Period Isolated)
        period_batches = db.query(models.MasterBatch).filter(models.MasterBatch.registered_period == active_period).all()
        total_generated = sum([b.total_yield for b in period_batches])
        total_generated_sheets = total_generated * 6

        reams_converted = math.ceil(total_generated_sheets / 500) if total_generated_sheets > 0 else 0
        
        # 2. Math for Reams Distributed
        total_distributed_sheets = total_issued * 6
        reams_distributed = total_distributed_sheets // 500
        loose_distributed = total_distributed_sheets % 500 
        
        defective_count = db.query(models.Booklet).filter(
            models.Booklet.exam_period == active_period,
            models.Booklet.return_reason == "Misprint / Defect"
        ).count()
        
        # --- 1. LIVE INVENTORY CALCULATION ---
        enrollments = db.query(models.Enrollment).all()
        total_headcount = sum([e.student_headcount for e in enrollments])
        remaining_demand = max(0, total_headcount - total_issued)
        
        available_stock = db.query(models.Booklet).filter(
            models.Booklet.status.in_(["available", "loose_stock"])
        ).count()

        # --- 2. TREND CHART ---
        daily_counts = {}
        for b in issued_booklets:
            if b.date_distributed:
                date_str = b.date_distributed.strftime("%b %d, %Y")
                daily_counts[date_str] = daily_counts.get(date_str, 0) + 1
                
        sorted_dates = sorted(daily_counts.keys(), key=lambda d: datetime.strptime(d, "%b %d, %Y"))
        trend_labels = sorted_dates
        trend_data = [daily_counts[d] for d in sorted_dates]

        # --- 3. YEAR LEVEL REVENUE & TRANSPARENT COMPUTATIONS ENGINE ---
        cs_count = 0
        it_count = 0
        
        year_level_sales = {"CS": {"1": 0, "2": 0, "3": 0, "4": 0}, "IT": {"1": 0, "2": 0, "3": 0, "4": 0}}
        comp_records = {"CS": {"1": [], "2": [], "3": [], "4": []}, "IT": {"1": [], "2": [], "3": [], "4": []}}
        comp_summary = {"CS": {"1": 0, "2": 0, "3": 0, "4": 0, "total": 0}, "IT": {"1": 0, "2": 0, "3": 0, "4": 0, "total": 0}}

        for enr in enrollments:
            b_count = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.exam_period == active_period,
                models.Booklet.status == "distributed"
            ).count()

            if b_count > 0:
                amount = b_count * 10
                dept = "CS" if "BSCS" in enr.section.upper() else ("IT" if "BSIT" in enr.section.upper() else None)
                
                if dept == "CS": cs_count += b_count
                if dept == "IT": it_count += b_count

                if dept:
                    y = next((char for char in enr.section if char.isdigit()), "1")
                    if y in ["1", "2", "3", "4"]:
                        year_level_sales[dept][y] += amount
                        comp_summary[dept][y] += amount
                        comp_summary[dept]["total"] += amount
                        comp_records[dept][y].append({
                            "teacher": enr.teacher_name,
                            "subject": enr.subject,
                            "section": enr.section,
                            "headcount": b_count,
                            "amount": amount
                        })

        return {
            "status": "success",
            "active_period": active_period,
            "kpi": {
                "reams_converted": reams_converted,
                "total_generated": total_generated,
                "reams_distributed": reams_distributed,
                "loose_distributed": loose_distributed,
                "issued": total_issued,
                "defective": defective_count
            },
            "inventory": {
                "available": available_stock,
                "remaining_demand": remaining_demand
            },
            "charts": {
                "departments": {"CS": cs_count, "IT": it_count, "Other": max(0, total_issued - (cs_count + it_count))},
                "trend": {"labels": trend_labels, "data": trend_data}
            },
            "year_level_sales": year_level_sales,
            "computations_data": {
                "records": comp_records,
                "summary": comp_summary
            }
        }
    except Exception as e:
        print(f"Dashboard Error: {str(e)}")
        return {"status": "error", "message": str(e)}

# --- REORDER DECISION EXPERT SYSTEM (Inference Engine) ---
EXPERT_RULES = [
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Finals' in facts['period'],
        "advice": lambda facts: (
            f"⚠️ SEMESTER ENDING: Demand is short by {facts['pending_demand'] - facts['available_stock']} booklets. "
            f"Action: Open exactly {__import__('math').ceil((facts['pending_demand'] - facts['available_stock']) * 6 / 500)} Ream(s). "
            f"This yields {(__import__('math').ceil((facts['pending_demand'] - facts['available_stock']) * 6 / 500) * 500) // 6} booklets. Do not print extra buffer stock because the semester is ending."
        ),
        "color": "#e74c3c"
    },
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Midterms' in facts['period'],
        "advice": lambda facts: (
            f"🔥 CRITICAL PEAK (MIDTERMS): Demand is short by {facts['pending_demand'] - facts['available_stock']} booklets. "
            f"Action: Open exactly {__import__('math').ceil((int((facts['pending_demand'] - facts['available_stock']) * 1.15)) * 6 / 500)} Ream(s). "
            f"This yields {(__import__('math').ceil((int((facts['pending_demand'] - facts['available_stock']) * 1.15)) * 6 / 500) * 500) // 6} booklets, covering the deficit PLUS a 15% buffer for late enrollees and replacements."
        ),
        "color": "#c0392b"
    },
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Prelims' in facts['period'],
        "advice": lambda facts: (
            f"📉 NORMAL DEFICIT (PRELIMS): Demand is short by {facts['pending_demand'] - facts['available_stock']} booklets. "
            f"Action: Open exactly {__import__('math').ceil((int((facts['pending_demand'] - facts['available_stock']) * 1.05)) * 6 / 500)} Ream(s). "
            f"This yields {(__import__('math').ceil((int((facts['pending_demand'] - facts['available_stock']) * 1.05)) * 6 / 500) * 500) // 6} booklets, covering the deficit plus a standard 5% buffer."
        ),
        "color": "#e67e22"
    },
    {
        "conditions": lambda facts: facts['available_stock'] >= facts['pending_demand'] and (facts['available_stock'] - facts['pending_demand']) < 20,
        "advice": lambda facts: "💡 SAFE BUFFER WARNING: Demand is met, but you have dipped below the safe buffer of 20 booklets. Consider a small reorder batch soon.",
        "color": "#3498db"
    },
    {
        "conditions": lambda facts: facts['available_stock'] >= facts['pending_demand'] and (facts['available_stock'] - facts['pending_demand']) >= 20,
        "advice": lambda facts: "✅ INVENTORY HEALTHY: Available stock fully meets pending demand with a safe buffer. No reordering necessary.",
        "color": "#27ae60"
    }
]

@app.get("/api/reorder-decision-expert")
def reorder_decision_expert(db: Session = Depends(get_db)):
    # 1. Gather facts
    total_students = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    distributed = db.query(models.Booklet).filter(models.Booklet.status == "distributed").count()
    pending_demand = max(0, total_students - distributed)
    available_stock = db.query(models.Booklet).filter(models.Booklet.status == "available").count()
    
    # Get active period
    period_name = get_active_period(db)
    
    facts = {
        'available_stock': available_stock,
        'pending_demand': pending_demand,
        'period': period_name
    }
    
    # 2. Run Inference Engine
    expert_advice = []
    advice_color = "#34495e"
    
    for rule in EXPERT_RULES:
        if rule["conditions"](facts):
            expert_advice.append(rule["advice"](facts) if callable(rule["advice"]) else rule["advice"])
            advice_color = rule["color"]
            break # Fire only highest priority rule
            
    if not expert_advice:
        expert_advice.append("System analyzing...")
        
    return {
        "facts": facts,
        "advice": expert_advice[0],
        "color": advice_color
    }


@app.get("/manage_users")
def manage_users_page(request: Request):
    return templates.TemplateResponse("manage_user.html", {"request": request})

@app.post("/api/users/create")
def create_user(req: schemas.CreateUserRequest, db: Session = Depends(get_db)):
    # Check if username exists
    existing_user = db.query(models.User).filter(models.User.username == req.username).first()
    if existing_user:
        return {"status": "error", "message": "Username already exists."}
    
    # Hash password
    hashed_password = bcrypt.hashpw(req.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    new_user = models.User(
        username=req.username,
        password=hashed_password,
        role=req.role
    )
    db.add(new_user)
    db.commit()
    
    return {"status": "success", "message": "Account created successfully!"}




