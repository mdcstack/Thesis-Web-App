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
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings()
        db.add(settings)
        db.commit()

    ream_count = request_data.ream_count
    # Deterministic yield logic based on Admin settings
    yield_per_ream = 500 // settings.sheets_per_booklet
    total_yield = ream_count * yield_per_ream
    
    # 1. Find the last generated reference number
    last_booklet = db.query(models.Booklet).order_by(models.Booklet.id.desc()).first()
    start_num = 1
    if last_booklet:
        start_num = int(last_booklet.control_num.split("-")[-1]) + 1
        
    # 2. Generate Master Batch ID
    batch_id = f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    current_period = get_active_period(db)
    
    prefix = settings.current_semester_name or "1ST26"
    
    new_batch = models.MasterBatch(
        qr_code_id=batch_id,
        ream_count=ream_count,
        total_yield=total_yield, 
        start_control_num=f"{prefix}-{start_num:04d}",
        end_control_num=f"{prefix}-{(start_num + total_yield - 1):04d}",
        registered_period=current_period,
        ream_price=settings.ream_price,
        sheets_per_booklet=settings.sheets_per_booklet
    )
    db.add(new_batch)
    db.commit()
    db.refresh(new_batch)
    
    # 3. Log ALL physical booklets dynamically (Marked as pending)
    for i in range(total_yield):
        ref_num = f"{prefix}-{(start_num + i):04d}"
        new_booklet = models.Booklet(
            control_num=ref_num,
            master_batch_id=new_batch.id,
            status="pending" # Changed from 'available'
        )
        db.add(new_booklet)
    db.commit()
    
    # 4. Trigger PDF Stamping for the total yield
    pdf_path = generate_batch_pdf(start_num, total_yield, batch_id, prefix)
    
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
            refs = f"{booklets[0].control_num} ➔ {booklets[-1].control_num}" if qty > 1 else booklets[0].control_num
            
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
        
    batches.sort(key=lambda x: extract_num(x.start_control_num))
    
    # 3. Validate Contiguity (Ensure there are no gaps in the reference numbers)
    for i in range(len(batches) - 1):
        current_end = extract_num(batches[i].end_control_num)
        next_start = extract_num(batches[i+1].start_control_num)
        if next_start != current_end + 1:
            raise HTTPException(
                status_code=400, 
                detail=f"Gap detected! {batches[i].end_control_num} does not seamlessly connect to {batches[i+1].start_control_num}."
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
    loose = db.query(models.Booklet).filter(models.Booklet.status == "loose_stock").order_by(models.Booklet.control_num).all()
    return {"status": "success", "data": [{"id": b.id, "control_num": b.control_num} for b in loose]}

@app.get("/api/pending-distributions")
def get_pending_distributions(db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    active_period = get_active_period(db) # Get the current period!
    
    settings = db.query(models.SystemSettings).first()
    booklet_price = settings.booklet_price if settings else 2.50
    
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
            
    return {
        "status": "success", 
        "data": data, 
        "active_period": active_period,
        "booklet_price": booklet_price
    }


@app.get("/api/pack-status/{qr_code}")
def get_pack_status(qr_code: str, username: Optional[str] = None, db: Session = Depends(get_db)):
    """Checks the scanned QR code and returns how many booklets are available inside.
    Enforces exclusive pack locking per user session."""
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

    if available_count == 0:
        return {
            "status": "error",
            "code": "PACK_DEPLETED",
            "message": "This Master Pack has already been fully depleted (0 pcs remaining)."
        }

    # Anti-Overlap Collision Protection: Check if held by another active user
    primary_batch = batches[0]
    if primary_batch.claimed_by and username and primary_batch.claimed_by != username:
        return {
            "status": "error",
            "code": "PACK_LOCKED",
            "claimed_by": primary_batch.claimed_by,
            "message": f"This Master Pack is currently opened and held by '{primary_batch.claimed_by}'. To maintain an organized workflow, two accounts cannot distribute from the same box. Please scan a different pack."
        }

    # Exclusively lock/claim this pack for the current user
    if username:
        for b in batches:
            b.claimed_by = username
        db.commit()

    return {"status": "success", "qr_code": qr_code, "available": available_count, "claimed_by": username}


class ReleasePackRequest(BaseModel):
    username: str
    qr_code: Optional[str] = None

@app.post("/api/release-pack")
def release_pack(req: ReleasePackRequest, db: Session = Depends(get_db)):
    """Releases the exclusive claim on a Master Pack so another user can scan it."""
    query = db.query(models.MasterBatch).filter(models.MasterBatch.claimed_by == req.username)
    if req.qr_code:
        query = query.filter(
            (models.MasterBatch.qr_code_id == req.qr_code) |
            (models.MasterBatch.pack_qr_id == req.qr_code)
        )
    batches = query.all()
    for b in batches:
        b.claimed_by = None
    db.commit()
    return {"status": "success", "message": "Pack released successfully."}

import json

@app.post("/api/release-pack-beacon")
async def release_pack_beacon(request: Request, db: Session = Depends(get_db)):
    """Handles browser exit / tab closure beacon to release the user's active pack immediately."""
    try:
        body = await request.body()
        if body:
            data = json.loads(body.decode("utf-8"))
            username = data.get("username")
            if username:
                db.query(models.MasterBatch).filter(models.MasterBatch.claimed_by == username).update({models.MasterBatch.claimed_by: None})
                db.commit()
    except Exception as e:
        print("Beacon release error:", str(e))
    return {"status": "ok"}


    
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

@app.post("/api/authorize-admin")
def authorize_admin(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()

    if not user or not bcrypt.checkpw(req.password.encode('utf-8'), user.password.encode('utf-8')):
        return {"status": "error", "message": "Invalid username or password."}
    
    if user.role.lower() != 'admin':
        return {"status": "error", "message": "User does not have Admin privileges."}
    
    return {"status": "success", "message": "Authorized"}

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
        # Release any active Master Packs held by this user so others can use them
        db.query(models.MasterBatch).filter(models.MasterBatch.claimed_by == user.username).update({models.MasterBatch.claimed_by: None})
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
    section_upper = enr.section.strip().upper()
    inferred_dept = "CS"
    if "BSIT" in section_upper:
        inferred_dept = "IT"

    new_enr = models.Enrollment(
        teacher_name=enr.teacher_name.strip(),
        section=section_upper,
        subject=enr.subject.strip().upper(),
        student_headcount=enr.student_headcount,
        department=inferred_dept
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
        
        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50
        
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
                        f_ref = getattr(blk[0], 'control_num', getattr(blk[0], 'reference_number', ''))
                        l_ref = getattr(blk[-1], 'control_num', getattr(blk[-1], 'reference_number', ''))
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

                    # Read unit price based on Payment Status Grandfathering Rule
                    is_fully_paid = (enr.payment_status == "Fully Paid" or (enr.amount_due > 0 and enr.amount_paid >= enr.amount_due))
                    b_prices = [getattr(b, 'unit_price', None) for b in booklets if getattr(b, 'unit_price', None) is not None]

                    if is_fully_paid:
                        # Rule 1: Grandfathered at the historical rate
                        if b_prices:
                            effective_unit_price = float(b_prices[0])
                        elif qty > 0 and enr.amount_due > 0:
                            effective_unit_price = round(float(enr.amount_due) / qty, 2)
                        else:
                            effective_unit_price = float(booklet_price)
                        receipt_amount = round(qty * effective_unit_price, 2)
                    else:
                        # Rule 2 & 3: Unpaid & Partially Paid accounts reflect current updated price
                        if b_prices:
                            effective_unit_price = float(b_prices[0])
                        else:
                            effective_unit_price = float(booklet_price)
                        # Amount reflects total order due for whole batch
                        receipt_amount = round(qty * effective_unit_price, 2)

                    receipts_data.append({
                        "enrollment_id": enr.id,
                        "date": date_str,
                        "teacher": enr.teacher_name,
                        "section": enr.section,
                        "subject": enr.subject,
                        "qty": qty,
                        "unit_price": effective_unit_price,
                        "amount": receipt_amount,
                        "payment_status": enr.payment_status or "Unpaid",
                        "amount_due": round(enr.amount_due, 2),
                        "amount_paid": round(enr.amount_paid or 0.0, 2),
                        "staff": staff_name,
                        "log": log_strings,
                        "exam_period": period if period else "Legacy (No Period)" # NEW: Added Period
                    })
        
        return {"status": "success", "booklet_price": booklet_price, "data": receipts_data}
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
            ref = getattr(b, 'control_num', getattr(b, 'reference_number', ''))
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
            models.Booklet.control_num.in_(req.control_numbers)
        ).all()
        
        if not booklets:
            return {"status": "error", "message": "No matching booklets found to return."}
            
        return_qty = len(booklets)
        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == req.enrollment_id).first()
        is_defective = req.reason == "Misprint / Defect"

        if enrollment and not is_defective:
            enrollment.student_headcount = max(0, enrollment.student_headcount - return_qty)

        # Phase 3: Payment Tracking deduction on return
        if enrollment:
            settings = db.query(models.SystemSettings).first()
            price = settings.booklet_price if settings else 2.50
            enrollment.amount_due = max(0.0, enrollment.amount_due - (return_qty * price))
            
            if enrollment.amount_due == 0:
                enrollment.payment_status = "Unpaid" if enrollment.amount_paid == 0 else "Fully Paid"
            elif enrollment.amount_paid >= enrollment.amount_due:
                enrollment.payment_status = "Fully Paid"
            elif enrollment.amount_paid > 0:
                enrollment.payment_status = "Partially Paid"
            else:
                enrollment.payment_status = "Unpaid"

        for b in booklets:
            b.return_reason = req.reason
            if is_defective:
                b.status = "voided"
                # Keep enrollment_id intact for department defect analytics
            else:
                b.status = "loose_stock"
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
        if remaining_demand <= 0: 
            return {"status": "error", "code": "ALREADY_FULFILLED", "message": f"Section already fulfilled for {active_period}."}

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
        if req.selected_loose_controls:
            loose = db.query(models.Booklet).filter(
                models.Booklet.control_num.in_(req.selected_loose_controls),
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
            ).order_by(models.Booklet.control_num).limit(still_needed).all()
            if primary:
                booklets_to_distribute.extend(primary)
                still_needed -= len(primary)

        # Pool 3: Bridging Pack
        if still_needed > 0 and bridging_batch_ids:
            bridging = db.query(models.Booklet).filter(
                models.Booklet.master_batch_id.in_(bridging_batch_ids), 
                models.Booklet.status == "available"
            ).order_by(models.Booklet.control_num).limit(still_needed).all()
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
            f_ref = getattr(blk[0], 'control_num', getattr(blk[0], 'reference_number', ''))
            l_ref = getattr(blk[-1], 'control_num', getattr(blk[-1], 'reference_number', ''))
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
        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50
        total_amount = round(remaining_demand * booklet_price, 2)
        return {
            "status": "success", 
            "preview_log": chunk_log_str,
            "booklet_price": booklet_price,
            "total_amount": total_amount
        }
        
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
            return {"status": "error", "code": "ALREADY_FULFILLED", "message": f"Section already fulfilled for {active_period}."}

        primary_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.scanned_pack_qr) | (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)).all()
        primary_batch_ids = [b.id for b in primary_batches]

        bridging_batch_ids = []
        if req.bridging_pack_qr:
            bridging_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.bridging_pack_qr) | (models.MasterBatch.pack_qr_id == req.bridging_pack_qr)).all()
            bridging_batch_ids = [b.id for b in bridging_batches]

        # --- THE TRIPLE-POOL ALGORITHM (Execution) ---
        booklets_to_distribute = []
        still_needed = remaining_demand

        if req.selected_loose_controls:
            loose = db.query(models.Booklet).filter(
                models.Booklet.control_num.in_(req.selected_loose_controls),
                models.Booklet.status == "loose_stock"
            ).all()
            booklets_to_distribute.extend(loose)
            still_needed -= len(loose)

        if still_needed > 0:
            primary = db.query(models.Booklet).filter(models.Booklet.master_batch_id.in_(primary_batch_ids), models.Booklet.status == "available").order_by(models.Booklet.control_num).limit(still_needed).all()
            booklets_to_distribute.extend(primary)
            still_needed -= len(primary)

        if still_needed > 0 and bridging_batch_ids:
            bridging = db.query(models.Booklet).filter(models.Booklet.master_batch_id.in_(bridging_batch_ids), models.Booklet.status == "available").order_by(models.Booklet.control_num).limit(still_needed).all()
            booklets_to_distribute.extend(bridging)

        if len(booklets_to_distribute) < remaining_demand:
            return {
                "status": "error",
                "code": "STOCK_CONFLICT",
                "message": f"Stock conflict: Available booklets ({len(booklets_to_distribute)}) did not match required demand ({remaining_demand}). Another clerk may have just distributed from this stock."
            }

        # 4. Bind the booklets to the teacher permanently and stamp the period & unit price
        settings = db.query(models.SystemSettings).first()
        price = settings.booklet_price if settings else 2.50

        for b in booklets_to_distribute:
            b.status = "distributed"
            b.enrollment_id = req.enrollment_id
            b.date_distributed = datetime.now()
            b.distributed_by = req.clerk_name
            b.exam_period = active_period
            b.unit_price = price
            
        if "BSCS" in enrollment.section.upper():
            enrollment.department = "CS"
        elif "BSIT" in enrollment.section.upper():
            enrollment.department = "IT"

        # Phase 3: Payment Tracking
        enrollment.amount_due += (len(booklets_to_distribute) * price)
        
        if enrollment.amount_due > 0 and enrollment.amount_paid >= enrollment.amount_due:
            enrollment.payment_status = "Fully Paid"
        elif enrollment.amount_paid > 0:
            enrollment.payment_status = "Partially Paid"
        else:
            enrollment.payment_status = "Unpaid"

        db.commit() # The Single-Save!
        
        return {"status": "success", "message": "Transaction perfectly recorded."}
    except Exception as e:
        print(f"CRITICAL ERROR IN EXECUTION: {str(e)}")
        return {"status": "error", "message": str(e)}

# Update this specific function in main.py
@app.get("/api/history-data")
def get_history_data(department: str = "ALL", db: Session = Depends(get_db)):
    try:
        query = db.query(models.Enrollment)
        if department.upper() not in ["ALL", "ADMIN"]:
            query = query.filter(models.Enrollment.department == department)
        enrollments = query.all()
        active_period = get_active_period(db)
        
        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50
        
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
                    refs = f"{booklets[0].control_num} ➔ {booklets[-1].control_num}" if qty > 1 else booklets[0].control_num
                    
                    dist_date = getattr(booklets[0], 'date_distributed', None)
                    if dist_date:
                        date_str = dist_date.strftime("%d-%b-%Y %I:%M %p")
                        # SECURE PYTHON MATH: Calculate exact days old
                        days_old = (datetime.now() - dist_date).days
                    else:
                        date_str = datetime.now().strftime("%d-%b-%Y %I:%M %p")
                        days_old = 0

                    # Determine effective price based on Payment Status Grandfathering Rule
                    is_fully_paid = (enr.payment_status == "Fully Paid" or (enr.amount_due > 0 and enr.amount_paid >= enr.amount_due))
                    b_prices = [getattr(b, 'unit_price', None) for b in booklets if getattr(b, 'unit_price', None) is not None]

                    if is_fully_paid:
                        if b_prices:
                            effective_price = float(b_prices[0])
                        elif qty > 0 and enr.amount_due > 0:
                            effective_price = round(float(enr.amount_due) / qty, 2)
                        else:
                            effective_price = float(booklet_price)
                    else:
                        if b_prices:
                            effective_price = float(b_prices[0])
                        else:
                            effective_price = float(booklet_price)

                    transaction_total = round(qty * effective_price, 2)

                    history_data.append({
                        "enrollment_id": enr.id,
                        "date": date_str,
                        "teacher": enr.teacher_name,
                        "section": enr.section,
                        "subject": enr.subject,
                        "qty": qty,
                        "booklet_price": effective_price,
                        "total_bill": transaction_total,
                        "amount_due": enr.amount_due,
                        "amount_paid": enr.amount_paid,
                        "balance": round(max(0.0, enr.amount_due - enr.amount_paid), 2),
                        "payment_status": enr.payment_status or "Unpaid",
                        "amount": transaction_total,
                        "refs": refs,
                        "exam_period": period if period else "Legacy (No Period)",
                        "days_old": days_old
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
            models.Booklet.control_num.like(f"%{ref.strip()}%"),
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


# --- SYSTEM INITIALIZATION GATE & STATUS ---
@app.get("/api/system-status")
def get_system_status(db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings(active_period="UNSET", booklet_price=0.0)
        db.add(settings)
        db.commit()
        db.refresh(settings)

    price = float(settings.booklet_price) if settings.booklet_price is not None else 0.0
    period = settings.active_period or "UNSET"
    
    # Initialized if a valid positive booklet selling price is established and active_period is set
    is_initialized = bool(price > 0.0 and period != "UNSET")

    return {
        "status": "success",
        "is_initialized": is_initialized,
        "active_period": period,
        "booklet_price": price,
        "ream_price": float(settings.ream_price or 250.0),
        "sheets_per_booklet": int(settings.sheets_per_booklet or 6),
        "current_semester_name": settings.current_semester_name or "1ST26",
        "requires_admin_setup": not is_initialized
    }


class AdminResetRequest(BaseModel):
    username: str
    password: str

@app.post("/api/admin/reset-transactions")
def reset_all_transactions(req: AdminResetRequest, db: Session = Depends(get_db)):
    """Clean slate reset to Day 0: Returns all distributed/loose booklets back to available,
    clears enrollment dues and payments back to 0, and resets system settings to uninitialized."""
    # Verify Admin credentials
    user = db.query(models.User).filter(models.User.username == req.username).first()
    if not user or not bcrypt.checkpw(req.password.encode('utf-8'), user.password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid admin credentials.")
    if user.role.lower() != 'admin':
        raise HTTPException(status_code=403, detail="Only System Administrators can perform a clean slate reset.")

    # 1. Reset all booklets (keep master batches intact)
    booklets = db.query(models.Booklet).all()
    reset_booklets_count = 0
    for b in booklets:
        b.status = "available"
        b.enrollment_id = None
        b.date_distributed = None
        b.distributed_by = None
        b.return_reason = None
        b.exam_period = None
        b.unit_price = None
        reset_booklets_count += 1

    # 2. Reset all enrollments
    enrollments = db.query(models.Enrollment).all()
    reset_enrollments_count = 0
    for enr in enrollments:
        enr.amount_due = 0.0
        enr.amount_paid = 0.0
        enr.payment_status = "Unpaid"
        reset_enrollments_count += 1

    # 3. Reset system settings to uninitialized
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings()
        db.add(settings)
    settings.active_period = "UNSET"
    settings.booklet_price = 0.0

    db.commit()

    return {
        "status": "success",
        "message": f"Clean slate reset complete! {reset_booklets_count} booklets returned to available stock; {reset_enrollments_count} classes reset to zero dues. System price set to UNSET (Day 0).",
        "booklets_reset": reset_booklets_count,
        "enrollments_reset": reset_enrollments_count
    }




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
        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50
        ream_price = settings.ream_price if settings else 250.0

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
                amount = b_count * booklet_price
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

        # --- 4. EXPENDITURE & ROI REPORT (FINANCIALS) ---
        cs_gross = cs_count * booklet_price
        it_gross = it_count * booklet_price
        total_gross = cs_gross + it_gross
        
        cs_share = cs_gross * 0.01
        it_share = it_gross * 0.01
        
        combined_subtotal = total_gross - cs_share - it_share
        igp_share = combined_subtotal * 0.20
        
        production_cost = reams_converted * ream_price
        net_income = combined_subtotal - igp_share - production_cost
        
        accounts_receivable = sum(max(0, enr.amount_due - enr.amount_paid) for enr in enrollments)
        
        financials = {
            "beginning_balance": 0.00,
            "accounts_receivable": accounts_receivable,
            "accounts_payable": igp_share,
            "production_cost": production_cost,
            "gross_revenue": total_gross,
            "cs_share": cs_share,
            "it_share": it_share,
            "combined_subtotal": combined_subtotal,
            "igp_share": igp_share,
            "net_income": net_income,
            "booklet_price": booklet_price,
            "ream_price": ream_price
        }

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
            },
            "financials": financials
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
    
    final_department = req.department
    if req.role == "Admin":
        final_department = "All"
    
    new_user = models.User(
        username=req.username,
        password=hashed_password,
        role=req.role,
        department=final_department
    )
    db.add(new_user)
    db.commit()
    
    return {"status": "success", "message": "Account created successfully!"}






from typing import Optional
import re

class SettingsUpdate(BaseModel):
    current_semester_name: Optional[str] = None
    ream_price: float
    sheets_per_booklet: int
    booklet_price: float
    active_period: str

@app.get("/settings", response_class=HTMLResponse)
def get_settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})

@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return {
        "current_semester_name": settings.current_semester_name,
        "ream_price": settings.ream_price,
        "sheets_per_booklet": settings.sheets_per_booklet,
        "booklet_price": settings.booklet_price,
        "active_period": settings.active_period
    }

@app.post("/api/settings")
def update_settings(req: SettingsUpdate, db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings()
        db.add(settings)
    
    try:
        period_part, ay_part = req.active_period.split(" | ")
        # e.g. "AY 2026-2027"
        match = re.search(r"AY\s+(\d{4})-(\d{4})", ay_part)
        if match:
            start_year = match.group(1)[-2:]
            end_year = match.group(2)[-2:]
        else:
            start_year = "XX"
            end_year = "XX"
            
        if "1st Sem" in period_part:
            derived_semester_name = f"1ST{start_year}"
        else:
            derived_semester_name = f"2ND{end_year}"
    except Exception:
        derived_semester_name = req.current_semester_name or "1STXX"
    
    old_price = float(settings.booklet_price) if settings.booklet_price is not None else 2.50
    new_price = float(req.booklet_price)

    settings.current_semester_name = derived_semester_name
    settings.ream_price = req.ream_price
    settings.sheets_per_booklet = req.sheets_per_booklet
    settings.booklet_price = new_price
    settings.active_period = req.active_period

    preserved_count = 0
    adjusted_count = 0

    if old_price != new_price:
        enrollments = db.query(models.Enrollment).all()
        for enr in enrollments:
            # Rule 1: Fully Paid transactions are 100% IMMUTABLE / LOCKED
            if enr.payment_status == "Fully Paid" or (enr.amount_due > 0 and enr.amount_paid >= enr.amount_due):
                preserved_count += 1
                continue

            # Rule 2 & 3: Unpaid and Partially Paid accounts re-priced for the whole batch
            distributed_count = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).count()

            if distributed_count > 0:
                # Whole-batch re-pricing
                enr.amount_due = round(distributed_count * new_price, 2)

                # Synchronize unit_price column on the actual distributed booklets
                db.query(models.Booklet).filter(
                    models.Booklet.enrollment_id == enr.id,
                    models.Booklet.status == "distributed"
                ).update({models.Booklet.unit_price: new_price}, synchronize_session=False)

                # Status re-evaluation with payments credited as-is
                if enr.amount_paid >= enr.amount_due:
                    enr.payment_status = "Fully Paid"
                elif enr.amount_paid > 0:
                    enr.payment_status = "Partially Paid"
                else:
                    enr.payment_status = "Unpaid"

                adjusted_count += 1

    db.commit()
    
    msg = f"Settings updated successfully. (Grandfathered {preserved_count} fully paid account(s); re-priced {adjusted_count} pending account(s) at ₱{new_price:.2f}/pc)" if old_price != new_price else "Settings updated successfully."
    return {"status": "success", "message": msg}


# ==========================================
# PHASE 3: INSTRUCTOR PAYMENT TRACKING
# ==========================================

from pydantic import BaseModel
class PaymentRequest(BaseModel):
    amount: float

@app.get("/payment-tracking", response_class=HTMLResponse)
def payment_tracking_page(request: Request):
    return templates.TemplateResponse("payment_tracking.html", {"request": request})

@app.get("/department-breakdown", response_class=HTMLResponse)
def department_breakdown_page(request: Request):
    return templates.TemplateResponse("department_breakdown.html", {"request": request})

@app.get("/expenditure-report", response_class=HTMLResponse)
def expenditure_report_page(request: Request):
    return templates.TemplateResponse("expenditure_report.html", {"request": request})

@app.get("/api/payment-tracking")
def get_payment_tracking(department: str = "ALL", db: Session = Depends(get_db)):
    try:
        # Get global settings to determine booklet price
        settings = db.query(models.SystemSettings).first()
        current_price = settings.booklet_price if settings else 2.50

        # Get all enrollments that have at least some booklets distributed (or amount_due > 0)
        query = db.query(models.Enrollment).filter(models.Enrollment.amount_due > 0)
        if department.upper() not in ["ALL", "ADMIN"]:
            query = query.filter(models.Enrollment.department == department)
        enrollments = query.all()
        data = []
        for enr in enrollments:
            # Count distributed booklets for this enrollment
            distributed_count = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).count()
            
            # Count loose returned booklets
            loose_returned = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == 'loose_stock'
            ).count()

            is_fully_paid = (enr.payment_status == "Fully Paid" or (enr.amount_due > 0 and enr.amount_paid >= enr.amount_due))
            effective_price = round(enr.amount_due / distributed_count, 2) if (is_fully_paid and distributed_count > 0) else current_price

            data.append({
                "id": enr.id,
                "teacher_name": enr.teacher_name,
                "section": enr.section,
                "subject": enr.subject,
                "amount_due": enr.amount_due,
                "amount_paid": enr.amount_paid,
                "balance": round(max(0.0, enr.amount_due - enr.amount_paid), 2),
                "payment_status": enr.payment_status,
                "department": enr.department,
                "student_headcount": enr.student_headcount,
                "distributed_count": distributed_count,
                "loose_returned": loose_returned,
                "booklet_price": effective_price
            })
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/enrollments/{enrollment_id}/pay")
def receive_payment(enrollment_id: int, req: PaymentRequest, db: Session = Depends(get_db)):
    try:
        if req.amount <= 0:
            return {"status": "error", "message": "Payment amount must be greater than zero."}
            
        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == enrollment_id).first()
        if not enrollment:
            return {"status": "error", "message": "Enrollment/Transaction not found."}
            
        remaining_balance = round(max(0.0, enrollment.amount_due - enrollment.amount_paid), 2)
        if round(req.amount, 2) > remaining_balance:
            return {
                "status": "error",
                "message": f"Payment amount (₱{req.amount:,.2f}) exceeds the outstanding balance (₱{remaining_balance:,.2f}). Overpayment is not allowed."
            }
            
        enrollment.amount_paid += req.amount
        
        # Recalculate status
        if enrollment.amount_paid >= enrollment.amount_due:
            enrollment.payment_status = "Fully Paid"
        elif enrollment.amount_paid > 0:
            enrollment.payment_status = "Partially Paid"
        else:
            enrollment.payment_status = "Unpaid"
            
        db.commit()
        return {"status": "success", "message": "Payment recorded successfully."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
