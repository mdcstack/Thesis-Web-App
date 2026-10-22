import math
import os
import re
import asyncio
import json
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, UploadFile, File
import bcrypt
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
import datetime
from fastapi.responses import RedirectResponse, HTMLResponse, Response
# Import local modules
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from . import models, schemas
from .database import SessionLocal, engine
from .utils import generate_batch_pdf, get_active_template_config, generate_sample_preview_pdf, render_template_preview_image, get_template_pdf_path, get_raw_template_pdf_path
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

def ensure_schema_migrations():
    try:
        from sqlalchemy import inspect, text
        import shutil
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        
        # 1. system_settings
        if 'system_settings' in tables:
            columns = [c['name'] for c in inspector.get_columns('system_settings')]
            with engine.connect() as conn:
                if 'department_ream_budget' not in columns:
                    conn.execute(text("ALTER TABLE system_settings ADD COLUMN department_ream_budget FLOAT DEFAULT 0.0"))
                    conn.commit()
                if 'reorder_buffer_percent' not in columns:
                    conn.execute(text("ALTER TABLE system_settings ADD COLUMN reorder_buffer_percent FLOAT DEFAULT 15.0"))
                    conn.commit()

        # 2. booklet_template_configs
        if 'booklet_template_configs' in tables:
            columns = [c['name'] for c in inspector.get_columns('booklet_template_configs')]
            with engine.connect() as conn:
                if 'department' not in columns:
                    conn.execute(text("ALTER TABLE booklet_template_configs ADD COLUMN department VARCHAR(20) DEFAULT 'CS'"))
                    conn.commit()
                if 'slot_number' not in columns:
                    conn.execute(text("ALTER TABLE booklet_template_configs ADD COLUMN slot_number INT DEFAULT 1"))
                    conn.commit()
                if 'is_configured' not in columns:
                    conn.execute(text("ALTER TABLE booklet_template_configs ADD COLUMN is_configured BOOLEAN DEFAULT FALSE"))
                    conn.commit()

        # 3. master_batches
        if 'master_batches' in tables:
            columns = [c['name'] for c in inspector.get_columns('master_batches')]
            with engine.connect() as conn:
                if 'department' not in columns:
                    conn.execute(text("ALTER TABLE master_batches ADD COLUMN department VARCHAR(50) DEFAULT 'CS'"))
                    conn.commit()
                if 'template_slot' not in columns:
                    conn.execute(text("ALTER TABLE master_batches ADD COLUMN template_slot INT DEFAULT 1"))
                    conn.commit()

        os.makedirs("static/templates", exist_ok=True)
        
        # 4. Seamless migration of template files and 3 slots per department
        db_mig = SessionLocal()
        try:
            for dept in ["CS", "IT"]:
                # Ensure legacy file is copied to slot 1 if present
                legacy_pdf = f"static/templates/active_template_{dept}.pdf"
                slot1_pdf = f"static/templates/active_template_{dept}_slot1.pdf"
                if os.path.exists(legacy_pdf) and not os.path.exists(slot1_pdf):
                    shutil.copyfile(legacy_pdf, slot1_pdf)
                legacy_png = f"static/templates/active_template_{dept}_preview.png"
                slot1_png = f"static/templates/active_template_{dept}_slot1_preview.png"
                if os.path.exists(legacy_png) and not os.path.exists(slot1_png):
                    shutil.copyfile(legacy_png, slot1_png)

                for slot_idx in [1, 2, 3]:
                    all_cfgs = db_mig.query(models.BookletTemplateConfig).filter(
                        models.BookletTemplateConfig.department == dept,
                        models.BookletTemplateConfig.slot_number == slot_idx
                    ).order_by(models.BookletTemplateConfig.is_configured.desc(), models.BookletTemplateConfig.id.asc()).all()
                    if len(all_cfgs) > 1:
                        for extra in all_cfgs[1:]:
                            db_mig.delete(extra)
                        db_mig.commit()
                        cfg = all_cfgs[0]
                    elif all_cfgs:
                        cfg = all_cfgs[0]
                    else:
                        cfg = None

                    if not cfg:
                        target_pdf = f"active_template_{dept}_slot{slot_idx}.pdf"
                        has_file = os.path.exists(f"static/templates/{target_pdf}")
                        new_cfg = models.BookletTemplateConfig(
                            department=dept,
                            slot_number=slot_idx,
                            is_configured=has_file,
                            template_name=f"Template {slot_idx}",
                            pdf_filename=target_pdf if has_file else None,
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
                        db_mig.add(new_cfg)
                        db_mig.commit()
        finally:
            db_mig.close()
    except Exception as e:
        print(f"Notice: schema migration check: {e}")

ensure_schema_migrations()


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

# ==========================================
# REAL-TIME OFFLINE LAN WEBSOCKET MANAGER
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead_connections = []
        payload_text = json.dumps(message)
        for connection in list(self.active_connections):
            try:
                await connection.send_text(payload_text)
            except Exception:
                dead_connections.append(connection)
        for dead in dead_connections:
            self.disconnect(dead)

manager = ConnectionManager()
main_event_loop = None

@app.on_event("startup")
async def startup_event():
    global main_event_loop
    try:
        main_event_loop = asyncio.get_running_loop()
    except Exception as e:
        print(f"Startup loop initialization note: {e}")

    # Self-healing audit for existing enrollment records
    try:
        init_db = SessionLocal()
        all_enrs = init_db.query(models.Enrollment).all()
        repaired = 0
        for enr in all_enrs:
            sec = (enr.section or "").upper()
            target_dept = "IT" if ("BSIT" in sec or "IT-" in sec) else ("CS" if ("BSCS" in sec or "CS-" in sec) else (enr.department or "CS").upper())
            if enr.department != target_dept:
                enr.department = target_dept
                repaired += 1
        if repaired > 0:
            init_db.commit()
            print(f"[Self-Healing] Repaired {repaired} enrollment department mismatch(es) on server startup.")
        init_db.close()
    except Exception as e:
        print(f"[Self-Healing] Startup audit note: {e}")

def notify_lan_mutation(event_type: str, details: dict = None, db: Session = None):
    """Increments the database data_version counter and broadcasts a real-time event to all LAN terminals."""
    new_version = 1
    try:
        should_close = False
        if db is None:
            db = SessionLocal()
            should_close = True
        
        settings = db.query(models.SystemSettings).first()
        if not settings:
            settings = models.SystemSettings(data_version=1)
            db.add(settings)
        else:
            settings.data_version = (settings.data_version or 1) + 1
        db.commit()
        new_version = settings.data_version
        if should_close:
            db.close()
    except Exception as e:
        print(f"Error updating data_version: {e}")

    try:
        ts = datetime.now().isoformat()
    except Exception:
        ts = ""

    payload = {
        "event": event_type,
        "version": new_version,
        "details": details or {},
        "timestamp": ts
    }

    global main_event_loop
    if main_event_loop and main_event_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(manager.broadcast(payload), main_event_loop)
        except Exception as e:
            print(f"Broadcast threadsafe error: {e}")
    else:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(manager.broadcast(payload))
            else:
                loop.run_until_complete(manager.broadcast(payload))
        except Exception:
            pass

@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current version on initial connection
        db = SessionLocal()
        settings = db.query(models.SystemSettings).first()
        current_version = settings.data_version if settings and settings.data_version else 1
        db.close()
        await websocket.send_text(json.dumps({
            "event": "CONNECTED",
            "version": current_version,
            "message": "Connected to Offline LAN Real-Time Sync"
        }))

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

@app.get("/api/sync-version")
def get_sync_version(db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    version = settings.data_version if settings and settings.data_version else 1
    return {"status": "success", "version": version}




@app.get("/api/mrp-status")
def get_mrp_status(db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    sheets_per_b = settings.sheets_per_booklet if settings and settings.sheets_per_booklet else 6
    yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83

    enrollments = db.query(models.Enrollment).all()
    
    total_students = sum(e.student_headcount for e in enrollments)
    required_booklets = total_students
    required_sheets = total_students * sheets_per_b

    full_reams = required_sheets // 500
    loose_sheets = required_sheets % 500
    whole_reams_to_buy = math.ceil(required_booklets / yield_per_r) if required_booklets > 0 else 0
    
    return {
        "total_enrolled_students": total_students,
        "exact_booklets_needed": required_booklets,
        "sheets_per_booklet": sheets_per_b,
        "yield_per_ream": yield_per_r,
        "exact_a4_sheets_needed": required_sheets,
        "full_reams": full_reams,
        "loose_sheets": loose_sheets,
        "usage_display": f"{full_reams} Ream(s) and {loose_sheets} paper sheets",
        "minimum_reams_required": whole_reams_to_buy
    }

# Schema to catch the JSON payload from the frontend
class ReamRequest(BaseModel):
    ream_count: int
    department: Optional[str] = "CS"
    template_slot: Optional[int] = 1

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

    # 0. Strict Academic Examination Period Guard
    current_period = get_active_period(db)
    if not current_period or current_period == "UNSET" or "UNSET" in current_period:
        raise HTTPException(
            status_code=400,
            detail="Academic examination period has not been established yet. Please configure the official examination period in System Settings before generating booklet batches."
        )

    department = (request_data.department or "CS").upper()
    if department not in ["CS", "IT"]:
        department = "CS"

    template_slot = getattr(request_data, 'template_slot', 1) or 1
    try:
        template_slot = int(template_slot)
        if template_slot not in [1, 2, 3]:
            template_slot = 1
    except Exception:
        template_slot = 1

    # Strict PDF Generator Guard: Check if chosen template slot is configured for requested department
    template_config = get_active_template_config(db, department=department, slot=template_slot)
    template_pdf = get_template_pdf_path(template_config, department=department, slot=template_slot)
    has_valid_file = bool(template_pdf and os.path.exists(template_pdf) and not template_pdf.endswith("blank_template.pdf") and not template_pdf.endswith(f"blank_template_{department}.pdf"))
    
    if not template_config or not template_config.is_configured or not has_valid_file:
        slot_label = template_config.template_name if (template_config and template_config.template_name) else f"Template {template_slot}"
        raise HTTPException(
            status_code=400,
            detail=f"Booklet cover {slot_label} for {department} is not configured yet. Please configure the template layout and upload the cover PDF under Booklet Template before generating booklet batches."
        )

    # Strict Class Enrollment Guard: Prevent batch generation when no enrollments exist in Manage Classes
    all_enrollments = db.query(models.Enrollment).all()
    if not all_enrollments:
        raise HTTPException(
            status_code=400,
            detail="Cannot generate booklet batch: No class sections have been enrolled in Manage Classes yet. Please enroll class sections before initiating batch production."
        )

    dept_enrollments = [e for e in all_enrollments if resolve_enrollment_department(e, db=db) == department]
    if not dept_enrollments:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot generate booklet batch for {department}: No class sections are enrolled for {department} in Manage Classes. Please add {department} enrollments first."
        )

    ream_count = request_data.ream_count
    # Deterministic yield logic based on Admin settings
    yield_per_ream = 500 // settings.sheets_per_booklet
    total_yield = ream_count * yield_per_ream
    
    # 1. Find the last generated reference number
    last_booklet = db.query(models.Booklet).order_by(models.Booklet.id.desc()).first()
    start_num = 1
    if last_booklet:
        start_num = int(last_booklet.control_num.split("-")[-1]) + 1
        
    # 2. Generate Master Batch ID (Daily Sequential: BATCH-YYYYMMDD######)
    today_str = datetime.now().strftime("%Y%m%d")
    batch_prefix = f"BATCH-{today_str}"
    existing_today_batches = db.query(models.MasterBatch).filter(
        models.MasterBatch.qr_code_id.like(f"{batch_prefix}%")
    ).all()
    max_batch_seq = 0
    for b in existing_today_batches:
        suffix = b.qr_code_id.replace(batch_prefix, "")
        if len(suffix) == 6 and suffix.isdigit() and int(suffix) < 10000:
            max_batch_seq = max(max_batch_seq, int(suffix))
    next_batch_num = max_batch_seq + 1
    batch_id = f"{batch_prefix}{next_batch_num:06d}"
    
    current_period = get_active_period(db)
    prefix = settings.current_semester_name or "1ST26"

    # 3. Calculate Global Sort Record Number (SRN) Range
    last_srn_booklet = db.query(models.Booklet).filter(
        models.Booklet.sort_record_num.isnot(None)
    ).order_by(models.Booklet.id.desc()).first()

    start_srn_num = 1
    if last_srn_booklet and last_srn_booklet.sort_record_num:
        try:
            start_srn_num = int(last_srn_booklet.sort_record_num.split("-")[-1]) + 1
        except (ValueError, IndexError):
            start_srn_num = db.query(models.Booklet).count() + 1

    start_srn_str = f"SRN-{start_srn_num:06d}"
    end_srn_str = f"SRN-{(start_srn_num + total_yield - 1):06d}"
    
    new_batch = models.MasterBatch(
        qr_code_id=batch_id,
        department=department,
        ream_count=ream_count,
        total_yield=total_yield, 
        start_control_num=f"{prefix}-{start_num:04d}",
        end_control_num=f"{prefix}-{(start_num + total_yield - 1):04d}",
        start_srn=start_srn_str,
        end_srn=end_srn_str,
        registered_period=current_period,
        ream_price=settings.ream_price,
        sheets_per_booklet=settings.sheets_per_booklet,
        template_slot=template_slot
    )
    db.add(new_batch)
    db.commit()
    db.refresh(new_batch)
    
    # 4. Log ALL physical booklets dynamically (Marked as pending with sequential SRN)
    for i in range(total_yield):
        ref_num = f"{prefix}-{(start_num + i):04d}"
        srn_num = f"SRN-{(start_srn_num + i):06d}"
        new_booklet = models.Booklet(
            control_num=ref_num,
            sort_record_num=srn_num,
            master_batch_id=new_batch.id,
            status="pending"
        )
        db.add(new_booklet)
    db.commit()
    notify_lan_mutation("STOCK_ADDED", {"ream_count": ream_count, "total_yield": total_yield, "department": department}, db=db)
    
    # 5. Trigger PDF Stamping for the total yield
    pdf_path = generate_batch_pdf(start_num, total_yield, batch_id, prefix, start_srn_num=start_srn_num, department=department, slot=template_slot, db=db)
    
    return {
        "message": f"{ream_count} Reams successfully converted for {department} using {template_config.template_name or f'Template {template_slot}'}.",
        "department": department,
        "template_slot": template_slot,
        "template_name": template_config.template_name or f"Template {template_slot}",
        "master_batch_id": batch_id,
        "booklets_generated": total_yield,
        "start_control_num": new_batch.start_control_num,
        "end_control_num": new_batch.end_control_num,
        "start_srn": start_srn_str,
        "end_srn": end_srn_str,
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

@app.get("/api/batch-info/{batch_id}")
def get_batch_info(batch_id: str, db: Session = Depends(get_db)):
    """Fetches metadata for a specific Batch or Pack ID, including course and template info."""
    clean_id = batch_id.strip()
    batch = db.query(models.MasterBatch).filter(
        (models.MasterBatch.qr_code_id == clean_id) |
        (models.MasterBatch.pack_qr_id == clean_id)
    ).first()
    
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch ID '{clean_id}' was not found in the database.")
    
    dept = (batch.department or "CS").upper()
    dept_label = "Computer Science (CS)" if dept == "CS" else ("Information Technology (IT)" if dept == "IT" else dept)
    slot = batch.template_slot or 1
    
    tpl = db.query(models.BookletTemplateConfig).filter(
        models.BookletTemplateConfig.department == dept,
        models.BookletTemplateConfig.slot_number == slot
    ).first()
    tpl_name = tpl.template_name if (tpl and tpl.template_name) else f"Template {slot}"
    
    booklet_count = db.query(models.Booklet).filter(models.Booklet.master_batch_id == batch.id).count()
    
    return {
        "status": "success",
        "batch_id": batch.qr_code_id,
        "pack_qr_id": batch.pack_qr_id,
        "department": dept,
        "department_label": dept_label,
        "template_slot": slot,
        "template_name": tpl_name,
        "batch_status": batch.status,
        "total_yield": batch.total_yield or booklet_count,
        "start_control_num": batch.start_control_num,
        "end_control_num": batch.end_control_num,
        "start_srn": batch.start_srn or "N/A",
        "end_srn": batch.end_srn or "N/A"
    }

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
            
    # 1b. Validate Course & Template Homogeneity (Cannot mix CS and IT covers)
    departments = set((b.department or "CS").upper() for b in batches)
    if len(departments) > 1:
        dept_names = {"CS": "Computer Science (CS)", "IT": "Information Technology (IT)"}
        dept_str = " vs ".join(dept_names.get(d, d) for d in departments)
        raise HTTPException(
            status_code=400, 
            detail=f"Course Template Mismatch: Cannot combine batches across different courses ({dept_str}). "
                   f"All batches in a physical pack must use identical booklet covers."
        )
        
    slots = set(b.template_slot or 1 for b in batches)
    if len(slots) > 1:
        raise HTTPException(
            status_code=400,
            detail="Template Mismatch: Selected batches were generated using different template designs. "
                   "All batches in a pack must use the same template."
        )

    # 2. Sort batches mathematically by their starting reference number
    def extract_num(ref_str):
        # Converts "1ST26-0042" or "REF-0042" into integer 42
        return int(ref_str.split('-')[-1])
        
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
            
    # 4. Generate the unified Supply Pack ID (Daily Sequential: PACK-YYYYMMDD######)
    today_str = datetime.now().strftime("%Y%m%d")
    pack_prefix = f"PACK-{today_str}"
    existing_today_packs = db.query(models.MasterBatch).filter(
        models.MasterBatch.pack_qr_id.like(f"{pack_prefix}%")
    ).all()
    max_pack_seq = 0
    for p in existing_today_packs:
        if p.pack_qr_id:
            suffix = p.pack_qr_id.replace(pack_prefix, "")
            if len(suffix) == 6 and suffix.isdigit() and int(suffix) < 10000:
                max_pack_seq = max(max_pack_seq, int(suffix))
    next_pack_num = max_pack_seq + 1
    pack_id = f"{pack_prefix}{next_pack_num:06d}"
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
    notify_lan_mutation("PACK_ACTIVATED", {"pack_id": pack_id, "total_booklets": total_booklets}, db=db)
    
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
    
    pack_dept = (batches[0].department or "CS").upper()
    pack_dept_label = "Computer Science (CS)" if pack_dept == "CS" else ("Information Technology (IT)" if pack_dept == "IT" else pack_dept)
    pack_slot = batches[0].template_slot or 1
    tpl = db.query(models.BookletTemplateConfig).filter(
        models.BookletTemplateConfig.department == pack_dept,
        models.BookletTemplateConfig.slot_number == pack_slot
    ).first()
    pack_tpl_name = tpl.template_name if (tpl and tpl.template_name) else f"Template {pack_slot}"

    return {
        "message": f"Success! Bundled {len(batches)} batches into active supply.",
        "pack_id": pack_id,
        "department": pack_dept,
        "department_label": pack_dept_label,
        "template_name": pack_tpl_name,
        "total_booklets": total_booklets,
        "start_control_num": batches[0].start_control_num,
        "end_control_num": batches[-1].end_control_num,
        "start_srn": batches[0].start_srn or "N/A",
        "end_srn": batches[-1].end_srn or "N/A",
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
def get_loose_stock_list(department: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(models.Booklet).join(models.MasterBatch).filter(models.Booklet.status == "loose_stock")
    if department and department.upper() in ["CS", "IT"]:
        query = query.filter(models.MasterBatch.department == department.upper())
    loose = query.order_by(models.Booklet.control_num).all()
    return {
        "status": "success", 
        "data": [
            {
                "id": b.id, 
                "control_num": b.control_num, 
                "department": b.master_batch.department if b.master_batch else "CS"
            } 
            for b in loose
        ]
    }

def resolve_enrollment_department(enrollment: models.Enrollment, db: Optional[Session] = None) -> str:
    """
    Authoritatively resolve department (IT or CS) for an enrollment record.
    Prioritizes section code prefix ('BSIT' / 'IT-' vs 'BSCS' / 'CS-').
    If the section matches a specific program but the database stored the opposite,
    it automatically self-heals the database record in-place.
    """
    if not enrollment:
        return "CS"
    sec = (enrollment.section or "").upper()
    if "BSIT" in sec or "IT-" in sec:
        correct_dept = "IT"
    elif "BSCS" in sec or "CS-" in sec:
        correct_dept = "CS"
    elif enrollment.department and enrollment.department.strip().upper() in ["CS", "IT"]:
        correct_dept = enrollment.department.strip().upper()
    else:
        correct_dept = "CS"
        
    # Self-heal database record in-place if detected inconsistent
    if db and enrollment.department != correct_dept:
        try:
            enrollment.department = correct_dept
            db.commit()
        except Exception:
            db.rollback()
            
    return correct_dept

@app.get("/api/pending-distributions")
def get_pending_distributions(db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    active_period = get_active_period(db) # Get the current period!
    
    settings = db.query(models.SystemSettings).first()
    booklet_price = settings.booklet_price if settings else 2.50
    
    data = []
    
    for e in enrollments:
        # MAGIC: Only count booklets currently distributed during THIS specific period
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == e.id,
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "distributed"
        ).count()
        
        remaining = e.student_headcount - fulfilled_count
        
        if remaining > 0:
            # Check for voided defective booklets needing replacement
            defective_count = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == e.id,
                models.Booklet.exam_period == active_period,
                models.Booklet.status == "voided",
                models.Booklet.return_reason.like("Misprint / Defect%")
            ).count()

            # Check grandfathered price
            prior_b = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == e.id,
                models.Booklet.exam_period == active_period,
                models.Booklet.unit_price.isnot(None)
            ).first()
            effective_price = float(prior_b.unit_price) if prior_b else float(booklet_price)
            credit = float(getattr(e, 'replacement_credit', 0.0) or 0.0)

            enr_dept = resolve_enrollment_department(e, db=db)

            data.append({
                "enrollment_id": e.id,
                "teacher_name": e.teacher_name,
                "section": e.section,   # FIXED: Changed from section_name to section
                "subject": e.subject,   # FIXED: Changed from subject_name to subject
                "headcount": e.student_headcount,
                "fulfilled": fulfilled_count,
                "remaining": remaining,
                "is_replacement": bool(defective_count > 0 and credit > 0),
                "replacement_count": defective_count,
                "replacement_credit": credit,
                "unit_price": effective_price,
                "department": enr_dept
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
    Enforces exclusive pack locking per user session and returns department & template metadata."""
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

    primary_batch = batches[0]
    dept = primary_batch.department or "CS"
    slot = primary_batch.template_slot or 1
    tpl_config = db.query(models.BookletTemplateConfig).filter(
        models.BookletTemplateConfig.department == dept,
        models.BookletTemplateConfig.slot_number == slot
    ).first()
    template_name = tpl_config.template_name if (tpl_config and tpl_config.template_name) else f"Template {slot}"

    if available_count == 0:
        return {
            "status": "error",
            "code": "PACK_DEPLETED",
            "message": "This Master Pack has already been fully depleted (0 pcs remaining).",
            "department": dept,
            "template_slot": slot,
            "template_name": template_name
        }

    # Anti-Overlap Collision Protection: Check if held by another active user
    if primary_batch.claimed_by and username and primary_batch.claimed_by != username:
        return {
            "status": "error",
            "code": "PACK_LOCKED",
            "claimed_by": primary_batch.claimed_by,
            "department": dept,
            "template_slot": slot,
            "template_name": template_name,
            "message": f"This Master Pack is currently opened and held by '{primary_batch.claimed_by}'. To maintain an organized workflow, two accounts cannot distribute from the same box. Please scan a different pack."
        }

    # Exclusively lock/claim this pack for the current user
    if username:
        for b in batches:
            b.claimed_by = username
        db.commit()

    return {
        "status": "success", 
        "qr_code": qr_code, 
        "available": available_count, 
        "claimed_by": username,
        "department": dept,
        "template_slot": slot,
        "template_name": template_name
    }


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
    notify_lan_mutation("USER_UPDATED", {"action": "login", "username": user.username, "id": user.id}, db=db)
    
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
        notify_lan_mutation("USER_UPDATED", {"action": "logout", "username": user.username, "id": user.id}, db=db)
    return {"status": "success"}

@app.get("/api/dss-alert")
def check_dss_alert(db: Session = Depends(get_db)):
    settings = db.query(models.SystemSettings).first()
    sheets_per_b = settings.sheets_per_booklet if settings and settings.sheets_per_booklet else 6
    yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83

    # Check if enrollments exist
    enrollment_count = db.query(models.Enrollment).count()
    total_students = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    has_enrollments = bool(enrollment_count > 0 and total_students > 0)

    if not has_enrollments:
        return {
            "alert": False,
            "has_enrollments": False,
            "status": "awaiting_enrollment",
            "message": "No classes have been enrolled yet. Add sections in Manage Classes to activate demand tracking.",
            "reams_needed": 0,
            "deficit_booklets": 0,
            "deficit_sheets": 0
        }

    # 1. Calculate pending demand (Students who haven't received booklets yet)
    distributed = db.query(models.Booklet).filter(models.Booklet.status == "distributed").count()
    pending_demand = max(0, total_students - distributed)
    
    # 2. Calculate available physical stock
    available_stock = db.query(models.Booklet).filter(models.Booklet.status == "available").count()
    
    # 3. Decision Support Logic (Is there a deficit?)
    if available_stock < pending_demand:
        deficit_booklets = pending_demand - available_stock
        deficit_sheets = deficit_booklets * sheets_per_b
        deficit_full_reams = deficit_sheets // 500
        deficit_loose_sheets = deficit_sheets % 500
        reams_to_print = math.ceil(deficit_booklets / yield_per_r)
        
        usage_text = f"{deficit_full_reams} Ream(s) and {deficit_loose_sheets} paper sheets" if deficit_full_reams > 0 else f"{deficit_loose_sheets} paper sheets"
        
        return {
            "alert": True, 
            "has_enrollments": True,
            "message": f"You have an inventory deficit of {deficit_booklets} booklets ({usage_text}). Procure {reams_to_print} whole Ream(s) to suffice this demand.",
            "deficit_booklets": deficit_booklets,
            "deficit_sheets": deficit_sheets,
            "deficit_full_reams": deficit_full_reams,
            "deficit_loose_sheets": deficit_loose_sheets,
            "sheets_per_booklet": sheets_per_b,
            "yield_per_ream": yield_per_r,
            "reams_needed": reams_to_print
        }
        
    return {
        "alert": False,
        "has_enrollments": True,
        "status": "sufficient",
        "message": "Inventory levels are sufficient. No active alerts."
    }    

@app.get("/enrollments", response_class=HTMLResponse)
def read_enrollments(request: Request, db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    return templates.TemplateResponse("enrollments.html", {"request": request, "enrollments": enrollments})

@app.post("/api/enrollments")
def create_enrollment(enr: schemas.EnrollmentCreate, db: Session = Depends(get_db)):
    section_upper = enr.section.strip().upper()
    if "BSIT" in section_upper or "IT-" in section_upper:
        inferred_dept = "IT"
    elif "BSCS" in section_upper or "CS-" in section_upper:
        inferred_dept = "CS"
    elif enr.department and enr.department.strip().upper() in ["CS", "IT"]:
        inferred_dept = enr.department.strip().upper()
    else:
        inferred_dept = "CS"

    new_enr = models.Enrollment(
        teacher_name=enr.teacher_name.strip(),
        section=section_upper,
        subject=enr.subject.strip().upper(),
        student_headcount=enr.student_headcount,
        department=inferred_dept
    )
    db.add(new_enr)
    db.commit()
    db.refresh(new_enr)
    notify_lan_mutation("ENROLLMENT_UPDATED", {"action": "create", "section": section_upper, "id": new_enr.id}, db=db)
    return {"status": "success", "message": "Class record added successfully!", "id": new_enr.id}

@app.post("/api/enrollments/bulk-import")
def bulk_import_enrollments(req: schemas.BulkEnrollmentImport, db: Session = Depends(get_db)):
    if not req.records:
        return {"status": "error", "message": "No enrollment records provided."}
        
    added_count = 0
    updated_count = 0
    
    for item in req.records:
        t_name = item.teacher_name.strip()
        sec_upper = item.section.strip().upper()
        subj_upper = item.subject.strip().upper()
        headcount = int(item.student_headcount)
        
        if not t_name or not sec_upper or not subj_upper or headcount < 1:
            continue
            
        # Deduplication check: Match instructor, section, and subject
        existing = db.query(models.Enrollment).filter(
            models.Enrollment.teacher_name == t_name,
            models.Enrollment.section == sec_upper,
            models.Enrollment.subject == subj_upper
        ).first()
        
        if existing:
            existing.student_headcount = headcount
            # Keep department updated as well
            if "BSIT" in sec_upper or "IT-" in sec_upper:
                existing.department = "IT"
            elif "BSCS" in sec_upper or "CS-" in sec_upper:
                existing.department = "CS"
            elif getattr(item, 'department', None) and item.department.strip().upper() in ["CS", "IT"]:
                existing.department = item.department.strip().upper()
            updated_count += 1
        else:
            if "BSIT" in sec_upper or "IT-" in sec_upper:
                inferred_dept = "IT"
            elif "BSCS" in sec_upper or "CS-" in sec_upper:
                inferred_dept = "CS"
            elif getattr(item, 'department', None) and item.department.strip().upper() in ["CS", "IT"]:
                inferred_dept = item.department.strip().upper()
            else:
                inferred_dept = "CS"

            new_enr = models.Enrollment(
                teacher_name=t_name,
                section=sec_upper,
                subject=subj_upper,
                student_headcount=headcount,
                department=inferred_dept
            )
            db.add(new_enr)
            added_count += 1
            
    db.commit()
    notify_lan_mutation("ENROLLMENT_UPDATED", {"action": "bulk_import", "added": added_count, "updated": updated_count}, db=db)
    
    return {
        "status": "success",
        "message": f"Bulk import complete: {added_count} new class(es) added, {updated_count} existing class(es) updated.",
        "added": added_count,
        "updated": updated_count,
        "total": added_count + updated_count
    }

@app.get("/api/enrollments/template")
def download_enrollment_template():
    csv_content = (
        "Instructor,Section,Subject,Headcount\n"
        "Dr. Maria Santos,BSCS-1A,Introduction to Computing,37\n"
        "Dr. Maria Santos,BSCS-1B,Computer Programming 1,42\n"
        "Engr. Mark Bautista,BSIT-1A,Introduction to Information Technology,44\n"
        "Engr. Mark Bautista,BSIT-1B,Computer Programming 1,43\n"
        "Prof. Juan Reyes,BSCS-2A,Data Structures and Algorithms,38\n"
    )
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=class_enrollment_template.csv"}
    )

@app.delete("/api/enrollments/{enr_id}")
def delete_enrollment(enr_id: int, db: Session = Depends(get_db)):
    enr = db.query(models.Enrollment).filter(models.Enrollment.id == enr_id).first()
    if not enr:
        return {"status": "error", "message": "Class record not found."}
    
    # Transaction Protection Guard: Check if any booklets are distributed to this class
    linked_bks = db.query(models.Booklet).filter(models.Booklet.enrollment_id == enr_id).count()
    if linked_bks > 0:
        return {
            "status": "error",
            "message": f"Cannot delete '{enr.section}' ({enr.subject}): {linked_bks} booklet(s) have already been distributed to this class. To maintain transaction history, classes with distributed booklets cannot be deleted. If this distribution was an error, return or void the booklets in Transaction Trail first."
        }

    sec_name = enr.section
    db.delete(enr)
    db.commit()
    notify_lan_mutation("ENROLLMENT_UPDATED", {"action": "delete", "enrollment_id": enr_id}, db=db)
    return {"status": "success", "message": f"Class '{sec_name}' removed successfully."}

@app.put("/api/enrollments/{enr_id}")
def update_enrollment(enr_id: int, enr: schemas.EnrollmentCreate, db: Session = Depends(get_db)):
    # 1. Find the existing record
    db_enr = db.query(models.Enrollment).filter(models.Enrollment.id == enr_id).first()
    
    if db_enr:
        # 2. Overwrite the old data with the new data from the form
        sec_clean = enr.section.strip().upper()
        db_enr.teacher_name = enr.teacher_name.strip()
        db_enr.section = sec_clean
        db_enr.subject = enr.subject.strip().upper()
        db_enr.student_headcount = enr.student_headcount
        
        # 3. Recalculate and update department upon editing
        if "BSIT" in sec_clean or "IT-" in sec_clean:
            db_enr.department = "IT"
        elif "BSCS" in sec_clean or "CS-" in sec_clean:
            db_enr.department = "CS"
        elif enr.department and enr.department.strip().upper() in ["CS", "IT"]:
            db_enr.department = enr.department.strip().upper()
        
        db.commit()
        notify_lan_mutation("ENROLLMENT_UPDATED", {"action": "update", "enrollment_id": enr_id}, db=db)
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
            fallback_price = settings.booklet_price if settings else 2.50
            unit_price = booklets[0].unit_price if getattr(booklets[0], 'unit_price', None) is not None else fallback_price
            return_value = round(return_qty * unit_price, 2)
            
            if is_defective:
                # Instructor will receive replacements for defective booklets.
                # Calculate new due for the remaining distributed booklets:
                new_due = max(0.0, round(enrollment.amount_due - return_value, 2))
                # How much was paid specifically towards these returned defective booklets?
                paid_towards_returned = max(0.0, round(enrollment.amount_paid - new_due, 2))
                
                # Transfer paid amount towards replacement credit so replacements are pre-paid:
                enrollment.replacement_credit = round(float(enrollment.replacement_credit or 0.0) + paid_towards_returned, 2)
                enrollment.amount_paid = max(0.0, round(enrollment.amount_paid - paid_towards_returned, 2))
                enrollment.amount_due = new_due
            else:
                # For non-defective (dropped/excess): No replacement is needed
                enrollment.amount_due = max(0.0, round(enrollment.amount_due - return_value, 2))
                if enrollment.amount_paid > enrollment.amount_due:
                    enrollment.amount_paid = enrollment.amount_due
            
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
        notify_lan_mutation("RETURN_PROCESSED", {
            "enrollment_id": req.enrollment_id,
            "reason": req.reason,
            "return_qty": return_qty,
            "is_defective": is_defective
        }, db=db)
        
        if is_defective:
            return {"status": "success", "message": f"Voided {return_qty} defective booklet(s). This section now requires a replacement."}
        else:
            return {"status": "success", "message": f"Successfully moved {return_qty} booklet(s) to Loose Stock."}
        
    except Exception as e:
        print(f"Error in /api/process-return: {str(e)}")
        return {"status": "error", "message": str(e)}
    
    
@app.get("/api/booklet-lookup/{identifier}")
def lookup_booklet(identifier: str, expected_dept: Optional[str] = None, db: Session = Depends(get_db)):
    """Resolves any identifier (Booklet QR payload, Control Number, or SRN) to an authentic booklet record."""
    raw = identifier.strip()
    
    # Try exact match on control_num, sort_record_num, or with SRN- prefix
    possible_srn = f"SRN-{raw.zfill(6)}" if raw.isdigit() else raw
    
    booklet = db.query(models.Booklet).join(models.MasterBatch).filter(
        (models.Booklet.control_num == raw) |
        (models.Booklet.sort_record_num == raw) |
        (models.Booklet.sort_record_num == possible_srn)
    ).first()
    
    if not booklet:
        return {"status": "error", "message": f"Booklet '{raw}' was not found in the database."}
        
    batch = booklet.master_batch
    dept = (batch.department if batch and batch.department else "CS").upper()
    
    # Department Verification Guard
    if expected_dept and dept != expected_dept.upper():
        dept_labels = {"CS": "Computer Science (CS)", "IT": "Information Technology (IT)"}
        return {
            "status": "error",
            "code": "DEPT_MISMATCH",
            "message": f"Department Mismatch: Booklet '{booklet.control_num}' is for {dept_labels.get(dept, dept)}, but active transaction requires {dept_labels.get(expected_dept.upper(), expected_dept)}."
        }
        
    return {
        "status": "success",
        "booklet": {
            "id": booklet.id,
            "control_num": booklet.control_num,
            "sort_record_num": booklet.sort_record_num or "N/A",
            "status": booklet.status,
            "department": dept,
            "batch_qr": batch.qr_code_id if batch else None,
            "pack_qr": batch.pack_qr_id if batch else None,
            "template_slot": batch.template_slot if batch else 1
        }
    }

# --- 1. PREVIEW ROUTE (DRY RUN) ---
@app.post("/api/preview-distribution")
def preview_distribution(req: schemas.DistributionRequest, db: Session = Depends(get_db)):
    try:
        # 1. Fetch the active period
        active_period = get_active_period(db)
        
        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == req.enrollment_id).first()
        if not enrollment: return {"status": "error", "message": "Section not found."}

        # 2. FILTER DEMAND BY ACTIVE PERIOD ONLY (Count currently distributed)
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == req.enrollment_id,
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "distributed"
        ).count()
        
        remaining_demand = enrollment.student_headcount - fulfilled_count
        if remaining_demand <= 0: 
            return {"status": "error", "code": "ALREADY_FULFILLED", "message": f"Section already fulfilled for {active_period}."}

        # Layer 5: Department Template Isolation Guard
        enrollment_dept = resolve_enrollment_department(enrollment, db=db)

        booklets_to_distribute = []

        # Check if client provided explicit control numbers (Manual Scan or Hybrid Swapped)
        if req.explicit_control_nums and len(req.explicit_control_nums) > 0:
            if len(req.explicit_control_nums) != remaining_demand:
                return {
                    "status": "error",
                    "message": f"Booklet count mismatch: Exactly {remaining_demand} booklets required, but {len(req.explicit_control_nums)} provided."
                }
            explicit_bks = db.query(models.Booklet).join(models.MasterBatch).filter(
                models.Booklet.control_num.in_(req.explicit_control_nums)
            ).all()
            if len(explicit_bks) != len(req.explicit_control_nums):
                found_c = set(b.control_num for b in explicit_bks)
                missing = [c for c in req.explicit_control_nums if c not in found_c]
                return {"status": "error", "message": f"Booklet(s) not found in system: {', '.join(missing)}"}

            for b in explicit_bks:
                b_dept = (b.master_batch.department or "CS").upper() if b.master_batch else "CS"
                if b_dept != enrollment_dept:
                    dept_label = "Computer Science (CS)" if b_dept == "CS" else "Information Technology (IT)"
                    enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                    return {
                        "status": "error",
                        "code": "DEPT_MISMATCH",
                        "message": f"Department Template Mismatch: Booklet '{b.control_num}' is for {dept_label}, but section {enrollment.section} is {enr_label}."
                    }
                if b.status not in ["available", "loose_stock"]:
                    return {
                        "status": "error",
                        "code": "STOCK_CONFLICT",
                        "message": f"Booklet '{b.control_num}' is not available (Current Status: {b.status})."
                    }
            
            # Sort to preserve sequential order
            explicit_bks.sort(key=lambda x: x.control_num)
            booklets_to_distribute = explicit_bks

        else:
            # --- THE TRIPLE-POOL ALGORITHM (Preview) ---
            # Verify Primary Pack
            primary_batches = db.query(models.MasterBatch).filter(
                (models.MasterBatch.qr_code_id == req.scanned_pack_qr) |
                (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)
            ).all()
            if not primary_batches: return {"status": "error", "message": "Invalid Primary Pack QR."}
            primary_batch_ids = [b.id for b in primary_batches]

            # Verify Bridging Pack (If provided)
            bridging_batch_ids = []
            bridging_batches = []
            if req.bridging_pack_qr:
                if req.scanned_pack_qr and req.bridging_pack_qr.strip() == req.scanned_pack_qr.strip():
                    return {
                        "status": "error",
                        "code": "DUPLICATE_PACK",
                        "message": "Duplicate Pack: The bridging pack cannot be the same as your active primary pack. Please scan a different Master Pack to bridge the transaction."
                    }
                bridging_batches = db.query(models.MasterBatch).filter(
                    (models.MasterBatch.qr_code_id == req.bridging_pack_qr) |
                    (models.MasterBatch.pack_qr_id == req.bridging_pack_qr)
                ).all()
                if not bridging_batches: return {"status": "error", "message": "Invalid Bridging Pack QR."}
                bridging_batch_ids = [b.id for b in bridging_batches]
                if any(b_id in primary_batch_ids for b_id in bridging_batch_ids):
                    return {
                        "status": "error",
                        "code": "DUPLICATE_PACK",
                        "message": "Duplicate Pack: The scanned bridging pack belongs to the same supply batch as your active primary pack. Please scan a different Master Pack to bridge the transaction."
                    }

            for b in primary_batches:
                batch_dept = b.department or "CS"
                if batch_dept != enrollment_dept:
                    dept_label = "Computer Science (CS)" if batch_dept == "CS" else "Information Technology (IT)"
                    enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                    return {
                        "status": "error",
                        "code": "DEPT_MISMATCH",
                        "message": f"Department Template Mismatch: Scanned supply box is for {dept_label}, but section {enrollment.section} is {enr_label}. You cannot distribute {batch_dept} booklet templates to a {enrollment_dept} section."
                    }

            if bridging_batches:
                for b in bridging_batches:
                    batch_dept = b.department or "CS"
                    if batch_dept != enrollment_dept:
                        dept_label = "Computer Science (CS)" if batch_dept == "CS" else "Information Technology (IT)"
                        enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                        return {
                            "status": "error",
                            "code": "BRIDGING_DEPT_MISMATCH",
                            "message": f"Bridging Template Mismatch: The bridging pack is for {dept_label}, but section {enrollment.section} is {enr_label}. All booklets in a single distribution must use the same {enr_label} template."
                        }

            if req.selected_loose_controls:
                mismatched_loose = db.query(models.Booklet).join(models.MasterBatch).filter(
                    models.Booklet.control_num.in_(req.selected_loose_controls),
                    models.MasterBatch.department != enrollment_dept
                ).count()
                if mismatched_loose > 0:
                    enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                    return {
                        "status": "error",
                        "code": "LOOSE_DEPT_MISMATCH",
                        "message": f"Loose Stock Mismatch: Selected loose booklets contain templates from another department. All booklets must match {enr_label}."
                    }

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
        fallback_price = settings.booklet_price if settings else 2.50
        
        # Grandfather price from prior booklets for this enrollment
        prior_b = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == enrollment.id,
            models.Booklet.exam_period == active_period,
            models.Booklet.unit_price.isnot(None)
        ).first()
        effective_price = float(prior_b.unit_price) if prior_b else float(fallback_price)
        
        gross_total = round(remaining_demand * effective_price, 2)
        credit_available = float(getattr(enrollment, 'replacement_credit', 0.0) or 0.0)
        credit_applied = min(credit_available, gross_total)
        net_amount = max(0.0, round(gross_total - credit_applied, 2))
        is_replacement = bool(credit_applied > 0 or credit_available > 0)
        
        return {
            "status": "success", 
            "preview_log": chunk_log_str,
            "booklet_price": effective_price,
            "gross_amount": gross_total,
            "replacement_credit": credit_applied,
            "total_amount": net_amount,
            "is_replacement": is_replacement,
            "allocated_controls": [b.control_num for b in booklets_to_distribute],
            "allocated_details": [
                {
                    "id": b.id,
                    "control_num": b.control_num,
                    "sort_record_num": b.sort_record_num or "N/A",
                    "status": b.status,
                    "department": (b.master_batch.department or "CS") if b.master_batch else "CS"
                }
                for b in booklets_to_distribute
            ]
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

        # 2. FILTER DEMAND BY ACTIVE PERIOD ONLY (Count currently distributed)
        fulfilled_count = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == req.enrollment_id,
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "distributed"
        ).count()
        
        remaining_demand = enrollment.student_headcount - fulfilled_count
        if remaining_demand <= 0:
            return {"status": "error", "code": "ALREADY_FULFILLED", "message": f"Section already fulfilled for {active_period}."}

        # Layer 5: Department Template Isolation Guard (Backend Execution Barrier)
        enrollment_dept = resolve_enrollment_department(enrollment, db=db)

        booklets_to_distribute = []

        # Check if client provided explicit control numbers (Manual Scan or Hybrid Swapped)
        if req.explicit_control_nums and len(req.explicit_control_nums) > 0:
            if len(req.explicit_control_nums) != remaining_demand:
                return {
                    "status": "error",
                    "message": f"Booklet count mismatch: Exactly {remaining_demand} booklets required, but {len(req.explicit_control_nums)} provided."
                }
            explicit_bks = db.query(models.Booklet).join(models.MasterBatch).filter(
                models.Booklet.control_num.in_(req.explicit_control_nums)
            ).all()
            if len(explicit_bks) != len(req.explicit_control_nums):
                found_c = set(b.control_num for b in explicit_bks)
                missing = [c for c in req.explicit_control_nums if c not in found_c]
                return {"status": "error", "message": f"Booklet(s) not found in system: {', '.join(missing)}"}

            for b in explicit_bks:
                b_dept = (b.master_batch.department or "CS").upper() if b.master_batch else "CS"
                if b_dept != enrollment_dept:
                    dept_label = "Computer Science (CS)" if b_dept == "CS" else "Information Technology (IT)"
                    enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                    return {
                        "status": "error",
                        "code": "DEPT_MISMATCH",
                        "message": f"Department Template Mismatch: Booklet '{b.control_num}' is for {dept_label}, but section {enrollment.section} is {enr_label}."
                    }
                if b.status not in ["available", "loose_stock"]:
                    return {
                        "status": "error",
                        "code": "STOCK_CONFLICT",
                        "message": f"Booklet '{b.control_num}' is not available (Current Status: {b.status})."
                    }
            
            explicit_bks.sort(key=lambda x: x.control_num)
            booklets_to_distribute = explicit_bks

        else:
            # --- THE TRIPLE-POOL ALGORITHM (Execution) ---
            primary_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.scanned_pack_qr) | (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)).all()
            if not primary_batches:
                return {"status": "error", "message": "Invalid Primary Pack QR."}
            primary_batch_ids = [b.id for b in primary_batches]

            bridging_batch_ids = []
            bridging_batches = []
            if req.bridging_pack_qr:
                if req.scanned_pack_qr and req.bridging_pack_qr.strip() == req.scanned_pack_qr.strip():
                    return {
                        "status": "error",
                        "code": "DUPLICATE_PACK",
                        "message": "Duplicate Pack: The bridging pack cannot be the same as your active primary pack. Please scan a different Master Pack to bridge the transaction."
                    }
                bridging_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.bridging_pack_qr) | (models.MasterBatch.pack_qr_id == req.bridging_pack_qr)).all()
                if not bridging_batches:
                    return {"status": "error", "message": "Invalid Bridging Pack QR."}
                bridging_batch_ids = [b.id for b in bridging_batches]
                if any(b_id in primary_batch_ids for b_id in bridging_batch_ids):
                    return {
                        "status": "error",
                        "code": "DUPLICATE_PACK",
                        "message": "Duplicate Pack: The scanned bridging pack belongs to the same supply batch as your active primary pack. Please scan a different Master Pack to bridge the transaction."
                    }

            for b in primary_batches:
                batch_dept = b.department or "CS"
                if batch_dept != enrollment_dept:
                    dept_label = "Computer Science (CS)" if batch_dept == "CS" else "Information Technology (IT)"
                    enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                    return {
                        "status": "error",
                        "code": "DEPT_MISMATCH",
                        "message": f"Department Template Mismatch: Scanned supply box is for {dept_label}, but section {enrollment.section} is {enr_label}. You cannot distribute {batch_dept} booklet templates to a {enrollment_dept} section."
                    }

            if bridging_batches:
                for b in bridging_batches:
                    batch_dept = b.department or "CS"
                    if batch_dept != enrollment_dept:
                        dept_label = "Computer Science (CS)" if batch_dept == "CS" else "Information Technology (IT)"
                        enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                        return {
                            "status": "error",
                            "code": "BRIDGING_DEPT_MISMATCH",
                            "message": f"Bridging Template Mismatch: The bridging pack is for {dept_label}, but section {enrollment.section} is {enr_label}. All booklets in a single distribution must use the same {enr_label} template."
                        }

            if req.selected_loose_controls:
                mismatched_loose = db.query(models.Booklet).join(models.MasterBatch).filter(
                    models.Booklet.control_num.in_(req.selected_loose_controls),
                    models.MasterBatch.department != enrollment_dept
                ).count()
                if mismatched_loose > 0:
                    enr_label = "Computer Science (CS)" if enrollment_dept == "CS" else "Information Technology (IT)"
                    return {
                        "status": "error",
                        "code": "LOOSE_DEPT_MISMATCH",
                        "message": f"Loose Stock Mismatch: Selected loose booklets contain templates from another department. All booklets must match {enr_label}."
                    }

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

        # 4. Bind the booklets to the teacher permanently and stamp the period & grandfathered unit price
        settings = db.query(models.SystemSettings).first()
        fallback_price = settings.booklet_price if settings else 2.50
        
        # Grandfather price from prior booklets for this enrollment in this period
        prior_b = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id == enrollment.id,
            models.Booklet.exam_period == active_period,
            models.Booklet.unit_price.isnot(None)
        ).first()
        effective_price = float(prior_b.unit_price) if prior_b else float(fallback_price)

        for b in booklets_to_distribute:
            b.status = "distributed"
            b.enrollment_id = req.enrollment_id
            b.date_distributed = datetime.now()
            b.distributed_by = req.clerk_name
            b.exam_period = active_period
            b.unit_price = effective_price
            
        if "BSCS" in enrollment.section.upper():
            enrollment.department = "CS"
        elif "BSIT" in enrollment.section.upper():
            enrollment.department = "IT"

        # Phase 3: Payment Tracking & Replacement Credit Application
        new_charge = round(len(booklets_to_distribute) * effective_price, 2)
        enrollment.amount_due = round(enrollment.amount_due + new_charge, 2)
        
        # Apply replacement credit if this enrollment has pre-paid credit from returned defective booklets
        credit_available = float(getattr(enrollment, 'replacement_credit', 0.0) or 0.0)
        credit_to_apply = min(credit_available, new_charge)
        
        if credit_to_apply > 0:
            enrollment.amount_paid = round(enrollment.amount_paid + credit_to_apply, 2)
            enrollment.replacement_credit = max(0.0, round(credit_available - credit_to_apply, 2))
        
        if enrollment.amount_due > 0 and enrollment.amount_paid >= enrollment.amount_due:
            enrollment.payment_status = "Fully Paid"
        elif enrollment.amount_paid > 0:
            enrollment.payment_status = "Partially Paid"
        else:
            enrollment.payment_status = "Unpaid"

        db.commit() # The Single-Save!
        notify_lan_mutation("DISTRIBUTION_RECORDED", {
            "enrollment_id": req.enrollment_id,
            "clerk": req.clerk_name,
            "qty": len(booklets_to_distribute)
        }, db=db)
        
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

                    # Defective returns check for audit tag
                    defective_voided_qty = db.query(models.Booklet).filter(
                        models.Booklet.enrollment_id == enr.id,
                        models.Booklet.exam_period == period,
                        models.Booklet.status == "voided",
                        models.Booklet.return_reason.like("Misprint / Defect%")
                    ).count()

                    # Amount paid displayed for this transaction:
                    # If fully paid, display matching transaction_total to reflect deducted price
                    paid_display = transaction_total if is_fully_paid else min(transaction_total, float(enr.amount_paid or 0.0))

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
                        "amount_paid": paid_display,
                        "balance": round(max(0.0, transaction_total - paid_display), 2),
                        "payment_status": enr.payment_status or "Unpaid",
                        "amount": transaction_total,
                        "refs": refs,
                        "exam_period": period if period else "Legacy (No Period)",
                        "days_old": days_old,
                        "defective_voided_qty": defective_voided_qty,
                        "replacement_credit": float(getattr(enr, 'replacement_credit', 0.0) or 0.0)
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
        settings = models.SystemSettings(active_period=period.get("active_period", "1st Sem - Midterm Exam"))
        db.add(settings)
    else:
        settings.active_period = period.get("active_period")
    db.commit()
    notify_lan_mutation("PERIOD_CHANGED", {"active_period": settings.active_period}, db=db)
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

    # 3. Reset system settings to uninitialized (Day 0 Clean Slate)
    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings()
        db.add(settings)
    settings.active_period = "UNSET"
    settings.booklet_price = 0.0
    settings.sheets_per_booklet = None
    settings.ream_price = None
    settings.department_ream_budget = 0.0
    settings.reorder_buffer_percent = None
    settings.current_semester_name = None

    db.commit()
    notify_lan_mutation("SYSTEM_RESET", {
        "booklets_reset": reset_booklets_count,
        "enrollments_reset": reset_enrollments_count
    }, db=db)

    return {
        "status": "success",
        "message": f"Clean slate reset complete! {reset_booklets_count} booklets returned to available stock; {reset_enrollments_count} classes reset to zero dues. System price set to UNSET (Day 0).",
        "booklets_reset": reset_booklets_count,
        "enrollments_reset": reset_enrollments_count
    }




# --- COURSE PRODUCTION METRICS (Dynamic & Settings-Driven) ---
def calculate_course_production_metrics(db: Session, active_period: Optional[str] = None):
    """Calculates period-isolated production demand broken down by course (CS vs IT) with dynamic buffer margin."""
    if not active_period:
        active_period = get_active_period(db)
    
    settings = db.query(models.SystemSettings).first()
    period_cfg = db.query(models.PeriodConfiguration).filter(
        models.PeriodConfiguration.academic_period == active_period
    ).first() if active_period else None

    if period_cfg:
        sheets_per_b = period_cfg.sheets_per_booklet or (settings.sheets_per_booklet if settings else 6)
        buffer_pct = float(period_cfg.reorder_buffer_percent if period_cfg.reorder_buffer_percent is not None else (settings.reorder_buffer_percent if settings and settings.reorder_buffer_percent is not None else 15.0))
    else:
        sheets_per_b = settings.sheets_per_booklet if settings and settings.sheets_per_booklet else 6
        buffer_pct = float(settings.reorder_buffer_percent if settings and settings.reorder_buffer_percent is not None else 15.0)

    yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83

    enrollments = db.query(models.Enrollment).all()
    total_enrollments = len(enrollments)
    total_headcount = sum(e.student_headcount for e in enrollments)
    has_system_enrollments = bool(total_enrollments > 0 and total_headcount > 0)

    def get_dept(e):
        sec = (e.section or '').upper()
        if 'BSIT' in sec: return 'IT'
        if 'BSCS' in sec: return 'CS'
        return getattr(e, 'department', 'CS') or 'CS'

    data = {}
    for dept in ["CS", "IT"]:
        dept_enrollments = [e for e in enrollments if get_dept(e) == dept]
        dept_enrollment_count = len(dept_enrollments)
        headcount = sum(e.student_headcount for e in dept_enrollments)
        dept_has_enrollments = bool(dept_enrollment_count > 0 and headcount > 0)
        
        # Distributed in active period
        distributed = db.query(models.Booklet).join(models.MasterBatch).filter(
            models.MasterBatch.department == dept,
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "distributed"
        ).count()
        
        remaining_demand = max(0, headcount - distributed)
        
        # Available vault stock
        available = db.query(models.Booklet).join(models.MasterBatch).filter(
            models.MasterBatch.department == dept,
            models.Booklet.status.in_(["available", "loose_stock"])
        ).count()
        
        deficit = max(0, remaining_demand - available)
        buffer_booklets = math.ceil(deficit * (buffer_pct / 100.0)) if deficit > 0 else 0
        target_production = deficit + buffer_booklets
        reams_needed = math.ceil(target_production / yield_per_r) if target_production > 0 else 0
        projected_yield = reams_needed * yield_per_r
        
        data[dept] = {
            "headcount": headcount,
            "enrollment_count": dept_enrollment_count,
            "has_enrollments": dept_has_enrollments,
            "distributed": distributed,
            "remaining_demand": remaining_demand,
            "available_stock": available,
            "deficit": deficit,
            "buffer_percent": buffer_pct,
            "buffer_booklets": buffer_booklets,
            "target_production": target_production,
            "reams_needed": reams_needed,
            "projected_yield": projected_yield,
            "is_satisfied": deficit == 0
        }

    return {
        "status": "success",
        "has_enrollments": has_system_enrollments,
        "total_enrollments": total_enrollments,
        "total_headcount": total_headcount,
        "active_period": active_period,
        "sheets_per_booklet": sheets_per_b,
        "yield_per_ream": yield_per_r,
        "buffer_percent": buffer_pct,
        "courses": data,
        "total_reams_needed": data["CS"]["reams_needed"] + data["IT"]["reams_needed"]
    }

@app.get("/api/course-production-metrics")
def get_course_production_metrics_endpoint(period: Optional[str] = None, db: Session = Depends(get_db)):
    return calculate_course_production_metrics(db, period)


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
        
        # 1. Math for Raw Reams Converted (Period Isolated via PeriodConfiguration archive)
        period_cfg = db.query(models.PeriodConfiguration).filter(
            models.PeriodConfiguration.academic_period == active_period
        ).first()

        settings = db.query(models.SystemSettings).first()
        if period_cfg:
            sheets_per_b = period_cfg.sheets_per_booklet or 6
            ream_price = float(period_cfg.ream_price or 250.0)
            booklet_price = float(period_cfg.booklet_price or 2.50)
            department_ream_budget = float(period_cfg.department_ream_budget or 0.0)
        else:
            sheets_per_b = settings.sheets_per_booklet if settings and settings.sheets_per_booklet else 6
            ream_price = float(settings.ream_price or 250.0) if settings else 250.0
            booklet_price = float(settings.booklet_price or 2.50) if settings else 2.50
            department_ream_budget = float(settings.department_ream_budget or 0.0) if settings else 0.0

        yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83

        period_batches = db.query(models.MasterBatch).filter(models.MasterBatch.registered_period == active_period).all()
        total_generated = sum([b.total_yield for b in period_batches])
        reams_converted = sum([b.ream_count for b in period_batches])
        reams_converted_cs = sum([b.ream_count for b in period_batches if b.department == 'CS'])
        reams_converted_it = sum([b.ream_count for b in period_batches if b.department == 'IT'])
        total_generated_sheets = sum([b.total_yield * (b.sheets_per_booklet or sheets_per_b) for b in period_batches])
        
        # 2. Math for Reams Distributed
        total_distributed_sheets = total_issued * sheets_per_b
        reams_distributed = total_distributed_sheets // 500
        loose_distributed = total_distributed_sheets % 500 
        
        defective_booklets = db.query(models.Booklet).filter(
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "voided",
            models.Booklet.return_reason.like("Misprint / Defect%")
        ).all()

        defective_cs = 0
        defective_it = 0
        for b in defective_booklets:
            if b.enrollment_id:
                enr_obj = db.query(models.Enrollment).filter(models.Enrollment.id == b.enrollment_id).first()
                if enr_obj:
                    dept = "CS" if "BSCS" in enr_obj.section.upper() else ("IT" if "BSIT" in enr_obj.section.upper() else enr_obj.department)
                    if dept == "CS": defective_cs += 1
                    elif dept == "IT": defective_it += 1
        defective_count = len(defective_booklets)
        
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
        # (booklet_price and ream_price resolved from PeriodConfiguration above)

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
                        sheets_used = b_count * sheets_per_b
                        s_reams = sheets_used // 500
                        s_loose = sheets_used % 500
                        comp_records[dept][y].append({
                            "teacher": enr.teacher_name,
                            "subject": enr.subject,
                            "section": enr.section,
                            "headcount": b_count,
                            "sheets_used": sheets_used,
                            "reams_and_sheets": f"{s_reams}R {s_loose}s",
                            "usage_display": f"{s_reams} Ream(s) and {s_loose} paper sheets",
                            "whole_reams_to_buy": math.ceil(sheets_used / 500) if sheets_used > 0 else 0,
                            "amount": amount
                        })

        cs_distributed_sheets = cs_count * sheets_per_b
        reams_distributed_cs = cs_distributed_sheets // 500
        loose_distributed_cs = cs_distributed_sheets % 500

        it_distributed_sheets = it_count * sheets_per_b
        reams_distributed_it = it_distributed_sheets // 500
        loose_distributed_it = it_distributed_sheets % 500

        course_metrics = calculate_course_production_metrics(db, active_period)

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
        
        def get_dept(e):
            sec = (e.section or "").upper()
            if "BSIT" in sec:
                return "IT"
            elif "BSCS" in sec:
                return "CS"
            return getattr(e, "department", "CS") or "CS"

        cs_billed = sum(enr.amount_due for enr in enrollments if get_dept(enr) == "CS")
        it_billed = sum(enr.amount_due for enr in enrollments if get_dept(enr) == "IT")
        total_billed = cs_billed + it_billed

        cs_collected = sum(enr.amount_paid for enr in enrollments if get_dept(enr) == "CS")
        it_collected = sum(enr.amount_paid for enr in enrollments if get_dept(enr) == "IT")
        total_collected = cs_collected + it_collected

        accounts_receivable_cs = sum(max(0.0, enr.amount_due - enr.amount_paid) for enr in enrollments if get_dept(enr) == "CS")
        accounts_receivable_it = sum(max(0.0, enr.amount_due - enr.amount_paid) for enr in enrollments if get_dept(enr) == "IT")
        accounts_receivable = accounts_receivable_cs + accounts_receivable_it

        # Department Ream Budget (Cash Allocation in PHP for paper procurement)
        department_ream_budget = float(settings.department_ream_budget or 0.0) if settings else 0.0
        beginning_balance = department_ream_budget
        reams_purchasing_power = int(department_ream_budget // ream_price) if ream_price > 0 else 0

        financials = {
            "beginning_balance": beginning_balance,
            "department_ream_budget": department_ream_budget,
            "reams_purchasing_power": reams_purchasing_power,
            "accounts_receivable": accounts_receivable,
            "accounts_receivable_cs": accounts_receivable_cs,
            "accounts_receivable_it": accounts_receivable_it,
            "cs_billed": cs_billed,
            "it_billed": it_billed,
            "total_billed": total_billed,
            "cs_collected": cs_collected,
            "it_collected": it_collected,
            "total_collected": total_collected,
            "accounts_payable": igp_share,
            "production_cost": production_cost,
            "gross_revenue": total_gross,
            "cs_gross": cs_gross,
            "it_gross": it_gross,
            "cs_share": cs_share,
            "it_share": it_share,
            "combined_subtotal": combined_subtotal,
            "igp_share": igp_share,
            "net_income": net_income,
            "booklet_price": booklet_price,
            "ream_price": ream_price,
            "reorder_buffer_percent": float(course_metrics["buffer_percent"])
        }

        return {
            "status": "success",
            "active_period": active_period,
            "sheets_per_booklet": sheets_per_b,
            "yield_per_ream": yield_per_r,
            "kpi": {
                "reams_converted": reams_converted,
                "reams_converted_cs": reams_converted_cs,
                "reams_converted_it": reams_converted_it,
                "total_generated": total_generated,
                "total_generated_sheets": total_generated_sheets,
                "reams_distributed": reams_distributed,
                "reams_distributed_cs": reams_distributed_cs,
                "reams_distributed_it": reams_distributed_it,
                "loose_distributed": loose_distributed,
                "loose_distributed_cs": loose_distributed_cs,
                "loose_distributed_it": loose_distributed_it,
                "usage_display": f"{reams_distributed} Ream(s) and {loose_distributed} paper sheets",
                "whole_reams_to_buy": math.ceil(total_distributed_sheets / 500) if total_distributed_sheets > 0 else 0,
                "issued": total_issued,
                "issued_cs": cs_count,
                "issued_it": it_count,
                "defective": defective_count,
                "defective_breakdown": {
                    "cs": defective_cs,
                    "it": defective_it,
                    "total": defective_count
                }
            },
            "inventory": {
                "available": available_stock,
                "remaining_demand": remaining_demand,
                "total_headcount": total_headcount,
                "enrollment_count": len(enrollments),
                "has_enrollments": bool(len(enrollments) > 0 and total_headcount > 0)
            },
            "charts": {
                "departments": {"CS": cs_count, "IT": it_count, "Other": max(0, total_issued - (cs_count + it_count))},
                "defective": {"CS": defective_cs, "IT": defective_it, "Total": defective_count},
                "trend": {"labels": trend_labels, "data": trend_data}
            },
            "year_level_sales": year_level_sales,
            "computations_data": {
                "records": comp_records,
                "summary": comp_summary,
                "sheets_per_booklet": sheets_per_b,
                "yield_per_ream": yield_per_r
            },
            "financials": financials,
            "course_breakdown": course_metrics["courses"],
            "course_production": course_metrics
        }
    except Exception as e:
        print(f"Dashboard Error: {str(e)}")
        return {"status": "error", "message": str(e)}

# --- REORDER DECISION EXPERT SYSTEM (Inference Engine) ---
EXPERT_RULES = [
    {
        "conditions": lambda facts: not facts.get('has_enrollments', True),
        "advice": lambda facts: (
            "📋 AWAITING CLASS ENROLLMENTS: No academic classes or student headcounts are registered in the system yet. "
            "The Inference Engine is on standby and will automatically generate heuristic reorder recommendations once class enrollments are established in Manage Classes."
        ),
        "color": "#475569"
    },
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Final' in facts['period'],
        "advice": lambda facts: (
            f"⚠️ SEMESTER ENDING (FINAL EXAM): Total demand is short by {facts['pending_demand'] - facts['available_stock']} booklets ({(facts['pending_demand'] - facts['available_stock']) * facts['sheets_per_booklet']} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_needed', __import__('math').ceil((facts['pending_demand'] - facts['available_stock']) / facts['yield_per_ream']))} Ream(s) total — "
            f"{facts.get('cs_data', {}).get('reams_needed', 0)} Reams for Computer Science (CS) and "
            f"{facts.get('it_data', {}).get('reams_needed', 0)} Reams for Information Technology (IT). "
            f"Convert each course under its designated cover template. Do not print extra buffer stock because the semester is ending."
        ),
        "color": "#e74c3c"
    },
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Midterm' in facts['period'],
        "advice": lambda facts: (
            f"🔥 CRITICAL PEAK (MIDTERM EXAM): Total demand is short by {facts['pending_demand'] - facts['available_stock']} booklets ({(facts['pending_demand'] - facts['available_stock']) * facts['sheets_per_booklet']} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_needed', 0)} Ream(s) total — "
            f"{facts.get('cs_data', {}).get('reams_needed', 0)} Reams for Computer Science (CS) and "
            f"{facts.get('it_data', {}).get('reams_needed', 0)} Reams for Information Technology (IT) "
            f"({'exact demand with 0% buffer configured in Settings' if facts.get('buffer_pct', 15) <= 0 else f'includes +{facts.get("buffer_pct", 15):g}% safety buffer configured in Settings'}). "
            f"Convert each course under its designated cover template to avoid cover mismatch."
        ),
        "color": "#c0392b"
    },
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Midyear' in facts['period'],
        "advice": lambda facts: (
            f"☀️ MIDYEAR TERM: Total demand is short by {facts['pending_demand'] - facts['available_stock']} booklets ({(facts['pending_demand'] - facts['available_stock']) * facts['sheets_per_booklet']} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_needed', 0)} Ream(s) total — "
            f"{facts.get('cs_data', {}).get('reams_needed', 0)} Reams for Computer Science (CS) and "
            f"{facts.get('it_data', {}).get('reams_needed', 0)} Reams for Information Technology (IT). "
            f"Convert each course under its designated cover template as the Midyear term is concluding."
        ),
        "color": "#e67e22"
    },
    {
        "conditions": lambda facts: facts['available_stock'] < facts['pending_demand'] and 'Prelims' in facts['period'],
        "advice": lambda facts: (
            f"📉 NORMAL DEFICIT (PRELIMS): Total demand is short by {facts['pending_demand'] - facts['available_stock']} booklets ({(facts['pending_demand'] - facts['available_stock']) * facts['sheets_per_booklet']} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_needed', 0)} Ream(s) total — "
            f"{facts.get('cs_data', {}).get('reams_needed', 0)} Reams for Computer Science (CS) and "
            f"{facts.get('it_data', {}).get('reams_needed', 0)} Reams for Information Technology (IT) "
            f"({'exact demand with 0% buffer configured in Settings' if facts.get('buffer_pct', 5) <= 0 else f'includes +{facts.get("buffer_pct", 5):g}% safety buffer configured in Settings'}). "
            f"Convert each course under its designated cover template."
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
    enrollment_count = db.query(models.Enrollment).count()
    total_students = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    has_enrollments = bool(enrollment_count > 0 and total_students > 0)

    distributed = db.query(models.Booklet).filter(models.Booklet.status == "distributed").count()
    pending_demand = max(0, total_students - distributed)
    available_stock = db.query(models.Booklet).filter(models.Booklet.status.in_(["available", "loose_stock"])).count()
    
    # Get active period and course breakdown
    period_name = get_active_period(db)
    course_metrics = calculate_course_production_metrics(db, period_name)
    sheets_per_b = course_metrics["sheets_per_booklet"]
    yield_per_r = course_metrics["yield_per_ream"]
    buffer_pct = course_metrics["buffer_percent"]
    buffer_multiplier = 1.0 + (buffer_pct / 100.0)
    
    cs_data = course_metrics["courses"]["CS"]
    it_data = course_metrics["courses"]["IT"]
    total_reams_needed = course_metrics["total_reams_needed"]

    facts = {
        'available_stock': available_stock,
        'pending_demand': pending_demand,
        'total_students': total_students,
        'enrollment_count': enrollment_count,
        'has_enrollments': has_enrollments,
        'period': period_name,
        'sheets_per_booklet': sheets_per_b,
        'yield_per_ream': yield_per_r,
        'buffer_pct': buffer_pct,
        'buffer_multiplier': buffer_multiplier,
        'course_metrics': course_metrics,
        'cs_data': cs_data,
        'it_data': it_data,
        'total_reams_needed': total_reams_needed
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
        "color": advice_color,
        "course_production": course_metrics
    }


@app.get("/manage_users")
def manage_users_page(request: Request):
    return templates.TemplateResponse("manage_user.html", {"request": request})

@app.get("/api/users")
def get_all_users(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    users = db.query(models.User).order_by(models.User.id.asc()).all()
    user_list = []
    for u in users:
        user_list.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "department": u.department or "All",
            "is_online": bool(u.session_token)
        })
    return {"status": "success", "users": user_list}

def validate_username(username: str, db: Session) -> tuple[bool, str]:
    if not username:
        return False, "Username is required."
    clean_username = username.strip()
    if " " in clean_username:
        return False, "Username cannot contain spaces. Use underscores (_) or dots (.) instead."
    if len(clean_username) < 3 or len(clean_username) > 30:
        return False, "Username must be between 3 and 30 characters long."
    if not re.match(r'^[a-zA-Z0-9_.]+$', clean_username):
        return False, "Username can only contain letters, numbers, underscores (_), and periods (.)."
    if clean_username.startswith(('.', '_')) or clean_username.endswith(('.', '_')):
        return False, "Username cannot start or end with a period or underscore."
    if ".." in clean_username:
        return False, "Username cannot contain consecutive periods."
    reserved_words = {"root", "system", "superuser", "null", "undefined", "anonymous"}
    if clean_username.lower() in reserved_words:
        return False, f"Username '{clean_username}' is reserved and cannot be used."
    # Case-insensitive uniqueness check
    existing = db.query(models.User).filter(func.lower(models.User.username) == clean_username.lower()).first()
    if existing:
        return False, "Username already exists."
    return True, ""

def validate_password(password: str, username: Optional[str] = None, current_password: Optional[str] = None) -> tuple[bool, str]:
    if not password:
        return False, "Password is required."
    clean_password = password.strip()
    if len(clean_password) < 8:
        return False, "Password must be at least 8 characters long."
    if len(clean_password) > 72:
        return False, "Password cannot exceed 72 characters."
    if not re.search(r'[A-Za-z]', clean_password) or not re.search(r'[0-9]', clean_password):
        return False, "Password must contain at least one letter and one number."
    if username and clean_password.lower() == username.strip().lower():
        return False, "Password cannot be identical to your username."
    if current_password and clean_password == current_password.strip():
        return False, "New password cannot be identical to your current password."
    return True, ""

@app.post("/api/users/create")
def create_user(req: schemas.CreateUserRequest, db: Session = Depends(get_db)):
    is_valid_user, user_err = validate_username(req.username, db)
    if not is_valid_user:
        return {"status": "error", "message": user_err}
    
    is_valid_pw, pw_err = validate_password(req.password, username=req.username)
    if not is_valid_pw:
        return {"status": "error", "message": pw_err}
    
    # Hash password
    hashed_password = bcrypt.hashpw(req.password.strip().encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    final_department = "All" if req.role == "Admin" else req.department
    
    new_user = models.User(
        username=req.username.strip(),
        password=hashed_password,
        role=req.role,
        department=final_department
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    notify_lan_mutation("USER_UPDATED", {"action": "create", "username": new_user.username, "id": new_user.id}, db=db)
    return {"status": "success", "message": f"Account '{new_user.username}' created successfully!", "id": new_user.id}

@app.put("/api/users/{user_id}")
def update_user(user_id: int, req: schemas.UpdateUserRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"status": "error", "message": "User not found."}
    
    # Root admin protection: cannot demote the primary 'admin' user from Admin role
    if user.username.lower() == "admin" and req.role != "Admin":
        return {"status": "error", "message": "The root 'admin' account role cannot be changed."}
    
    final_dept = "All" if req.role == "Admin" else req.department
    user.role = req.role
    user.department = final_dept
    db.commit()
    db.refresh(user)
    
    notify_lan_mutation("USER_UPDATED", {"action": "update", "user_id": user_id, "username": user.username}, db=db)
    return {"status": "success", "message": f"User '{user.username}' updated successfully!"}

@app.post("/api/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, req: schemas.AdminResetPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"status": "error", "message": "User not found."}
    
    is_valid_pw, pw_err = validate_password(req.new_password, username=user.username)
    if not is_valid_pw:
        return {"status": "error", "message": pw_err}
    
    hashed_pw = bcrypt.hashpw(req.new_password.strip().encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    user.password = hashed_pw
    # Invalidate session token to require logging in with new credentials
    user.session_token = None
    db.commit()
    
    notify_lan_mutation("USER_UPDATED", {"action": "reset_password", "user_id": user_id, "username": user.username}, db=db)
    return {"status": "success", "message": f"Password for '{user.username}' has been reset successfully!"}

@app.post("/api/users/change-password")
def user_change_password(req: schemas.ChangePasswordRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username.strip()).first()
    if not user:
        return {"status": "error", "message": "User not found."}
    
    # Verify current password
    if not bcrypt.checkpw(req.current_password.encode('utf-8'), user.password.encode('utf-8')):
        return {"status": "error", "message": "Current password is incorrect."}
    
    is_valid_pw, pw_err = validate_password(req.new_password, username=user.username, current_password=req.current_password)
    if not is_valid_pw:
        return {"status": "error", "message": pw_err}
    
    hashed_pw = bcrypt.hashpw(req.new_password.strip().encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    user.password = hashed_pw
    db.commit()
    
    notify_lan_mutation("USER_UPDATED", {"action": "change_password", "username": user.username}, db=db)
    return {"status": "success", "message": "Your password has been changed successfully!"}

@app.post("/api/users/{user_id}/revoke-session")
def revoke_user_session(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"status": "error", "message": "User not found."}
    
    user.session_token = None
    db.query(models.MasterBatch).filter(models.MasterBatch.claimed_by == user.username).update({models.MasterBatch.claimed_by: None})
    db.commit()
    
    notify_lan_mutation("USER_UPDATED", {"action": "revoke_session", "username": user.username}, db=db)
    return {"status": "success", "message": f"Active session for '{user.username}' has been revoked."}

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, current_user: Optional[str] = None, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"status": "error", "message": "User not found."}
    
    # Root admin safeguard
    if user.username.lower() == "admin":
        return {"status": "error", "message": "The primary 'admin' account cannot be deleted."}
    
    # Prevent self-deletion if current_user provided
    if current_user and user.username.lower() == current_user.strip().lower():
        return {"status": "error", "message": "You cannot delete your own currently logged-in account."}
    
    # Prevent deleting the last Admin
    if user.role == "Admin":
        admin_count = db.query(models.User).filter(models.User.role == "Admin").count()
        if admin_count <= 1:
            return {"status": "error", "message": "Cannot delete the last remaining Administrator account."}
    
    # Release any claimed batches
    db.query(models.MasterBatch).filter(models.MasterBatch.claimed_by == user.username).update({models.MasterBatch.claimed_by: None})
    
    uname = user.username
    db.delete(user)
    db.commit()
    
    notify_lan_mutation("USER_UPDATED", {"action": "delete", "username": uname}, db=db)
    return {"status": "success", "message": f"Account '{uname}' has been deleted."}







from typing import Optional
import re

class SettingsUpdate(BaseModel):
    current_semester_name: Optional[str] = None
    ream_price: float
    sheets_per_booklet: int
    booklet_price: float
    active_period: str
    department_ream_budget: Optional[float] = 0.0
    budget_is_manual: Optional[bool] = False
    reorder_buffer_percent: Optional[float] = 15.0
    configured_by: Optional[str] = "Administrator"

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

    is_unset = bool(not settings.active_period or settings.active_period == "UNSET" or "UNSET" in settings.active_period)
    
    if is_unset:
        sheets_per_b = settings.sheets_per_booklet if (settings.sheets_per_booklet and settings.sheets_per_booklet > 0) else None
        yield_per_r = (500 // sheets_per_b) if (sheets_per_b and sheets_per_b > 0) else None
        leftover = (500 % sheets_per_b) if (sheets_per_b and sheets_per_b > 0) else None
        ream_p = float(settings.ream_price) if (settings.ream_price and settings.ream_price > 0) else None
        booklet_p = float(settings.booklet_price) if (settings.booklet_price and settings.booklet_price > 0) else None
        budget = float(settings.department_ream_budget) if (settings.department_ream_budget and settings.department_ream_budget > 0) else None
        buffer_pct = float(settings.reorder_buffer_percent) if (settings.reorder_buffer_percent is not None and settings.reorder_buffer_percent >= 0) else None
        sem_prefix = settings.current_semester_name if (settings.current_semester_name and settings.current_semester_name != "1STXX") else None
    else:
        sheets_per_b = settings.sheets_per_booklet if (settings.sheets_per_booklet and settings.sheets_per_booklet > 0) else 6
        yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83
        leftover = 500 % sheets_per_b if sheets_per_b > 0 else 2
        ream_p = float(settings.ream_price or 250.0)
        booklet_p = float(settings.booklet_price or 0.0)
        budget = float(settings.department_ream_budget or 0.0)
        buffer_pct = float(settings.reorder_buffer_percent if settings.reorder_buffer_percent is not None else 15.0)
        sem_prefix = settings.current_semester_name

    total_demand = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    if yield_per_r and yield_per_r > 0 and total_demand > 0 and ream_p and ream_p > 0:
        recommended_reams = math.ceil(total_demand / yield_per_r)
        recommended_budget = round(recommended_reams * ream_p, 2)
    else:
        recommended_reams = 0
        recommended_budget = 0.0

    return {
        "current_semester_name": sem_prefix,
        "ream_price": ream_p,
        "sheets_per_booklet": sheets_per_b,
        "yield_per_ream": yield_per_r,
        "leftover_sheets": leftover,
        "booklet_price": booklet_p,
        "active_period": settings.active_period,
        "department_ream_budget": budget,
        "budget_is_manual": bool(settings.budget_is_manual) if hasattr(settings, 'budget_is_manual') else False,
        "reorder_buffer_percent": buffer_pct,
        "total_demand": total_demand,
        "recommended_reams": recommended_reams,
        "recommended_budget": recommended_budget,
        "is_initialized": not is_unset and bool(booklet_p and booklet_p > 0)
    }

@app.post("/api/settings")
def update_settings(req: SettingsUpdate, db: Session = Depends(get_db)):
    # --- STRICT VALIDATION GUARDS ---
    if not req.active_period or "UNSET" in req.active_period or " | " not in req.active_period:
        raise HTTPException(status_code=400, detail="Active examination period and academic year must be selected.")

    if req.sheets_per_booklet is None or req.sheets_per_booklet < 1 or req.sheets_per_booklet > 50:
        raise HTTPException(status_code=400, detail="Bond paper sheets per booklet must be between 1 and 50.")

    if req.ream_price is None or req.ream_price <= 0:
        raise HTTPException(status_code=400, detail="Price per raw bond paper ream must be greater than ₱0.00.")

    if req.booklet_price is None or req.booklet_price <= 0:
        raise HTTPException(status_code=400, detail="Booklet selling price must be greater than ₱0.00.")

    if req.department_ream_budget is None or req.department_ream_budget <= 0:
        raise HTTPException(status_code=400, detail="Department ream budget must be greater than ₱0.00.")

    if req.reorder_buffer_percent is None or req.reorder_buffer_percent < 0 or req.reorder_buffer_percent > 100:
        raise HTTPException(status_code=400, detail="Reorder buffer safety margin must be between 0% and 100%.")

    settings = db.query(models.SystemSettings).first()
    if not settings:
        settings = models.SystemSettings()
        db.add(settings)
    
    try:
        period_part, ay_part = req.active_period.split(" | ")
        match = re.search(r"AY\s+(\d{4})-(\d{4})", ay_part)
        if match:
            start_year = match.group(1)[-2:]
            end_year = match.group(2)[-2:]
        else:
            start_year = "XX"
            end_year = "XX"
            
        if "1st Sem" in period_part:
            derived_semester_name = f"1ST{start_year}"
        elif "Midyear" in period_part:
            derived_semester_name = f"MID{end_year}"
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
    settings.department_ream_budget = float(req.department_ream_budget or 0.0)
    settings.budget_is_manual = bool(req.budget_is_manual)
    settings.reorder_buffer_percent = float(req.reorder_buffer_percent if req.reorder_buffer_percent is not None else 15.0)

    # --- ARCHIVE SNAPSHOT IN period_configurations ---
    period_cfg = db.query(models.PeriodConfiguration).filter(
        models.PeriodConfiguration.academic_period == req.active_period
    ).first()
    if not period_cfg:
        period_cfg = models.PeriodConfiguration(academic_period=req.active_period)
        db.add(period_cfg)

    period_cfg.semester_prefix = derived_semester_name
    period_cfg.ream_price = req.ream_price
    period_cfg.sheets_per_booklet = req.sheets_per_booklet
    period_cfg.yield_per_ream = 500 // req.sheets_per_booklet if req.sheets_per_booklet > 0 else 83
    period_cfg.booklet_price = new_price
    period_cfg.department_ream_budget = float(req.department_ream_budget or 0.0)
    period_cfg.budget_is_manual = bool(req.budget_is_manual)
    period_cfg.reorder_buffer_percent = float(req.reorder_buffer_percent if req.reorder_buffer_percent is not None else 15.0)
    period_cfg.configured_by = req.configured_by or "Administrator"

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
                enr.amount_due = round(distributed_count * new_price, 2)
                db.query(models.Booklet).filter(
                    models.Booklet.enrollment_id == enr.id,
                    models.Booklet.status == "distributed"
                ).update({models.Booklet.unit_price: new_price}, synchronize_session=False)

                if enr.amount_paid >= enr.amount_due:
                    enr.payment_status = "Fully Paid"
                elif enr.amount_paid > 0:
                    enr.payment_status = "Partially Paid"
                else:
                    enr.payment_status = "Unpaid"

                adjusted_count += 1

    db.commit()
    notify_lan_mutation("SETTINGS_UPDATED", {
        "booklet_price": new_price,
        "active_period": settings.active_period,
        "department_ream_budget": settings.department_ream_budget,
        "reorder_buffer_percent": settings.reorder_buffer_percent
    }, db=db)
    
    msg = f"Settings updated successfully. (Grandfathered {preserved_count} fully paid account(s); re-priced {adjusted_count} pending account(s) at ₱{new_price:.2f}/pc)" if old_price != new_price else "Settings updated successfully."
    return {"status": "success", "message": msg}

@app.get("/api/period-configurations")
def list_period_configurations(db: Session = Depends(get_db)):
    """Returns all archived period configurations ordered by last updated date for audit history."""
    configs = db.query(models.PeriodConfiguration).order_by(models.PeriodConfiguration.updated_at.desc()).all()
    return [{
        "id": c.id,
        "academic_period": c.academic_period,
        "semester_prefix": c.semester_prefix,
        "ream_price": c.ream_price,
        "sheets_per_booklet": c.sheets_per_booklet,
        "yield_per_ream": c.yield_per_ream,
        "booklet_price": c.booklet_price,
        "department_ream_budget": c.department_ream_budget,
        "budget_is_manual": c.budget_is_manual,
        "reorder_buffer_percent": c.reorder_buffer_percent,
        "configured_by": c.configured_by,
        "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M:%S") if c.updated_at else ""
    } for c in configs]

@app.get("/api/period-configuration")
def get_period_configuration(period: str, db: Session = Depends(get_db)):
    """Returns saved parameters for a specific period, or smart defaults if new."""
    cfg = db.query(models.PeriodConfiguration).filter(
        models.PeriodConfiguration.academic_period == period
    ).first()

    total_demand = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    
    if cfg:
        yield_per_r = cfg.yield_per_ream or (500 // cfg.sheets_per_booklet)
        recommended_reams = math.ceil(total_demand / yield_per_r) if total_demand > 0 and yield_per_r > 0 else 0
        recommended_budget = round(recommended_reams * cfg.ream_price, 2)
        return {
            "found": True,
            "academic_period": cfg.academic_period,
            "semester_prefix": cfg.semester_prefix,
            "ream_price": cfg.ream_price,
            "sheets_per_booklet": cfg.sheets_per_booklet,
            "yield_per_ream": yield_per_r,
            "booklet_price": cfg.booklet_price,
            "department_ream_budget": cfg.department_ream_budget,
            "budget_is_manual": cfg.budget_is_manual,
            "reorder_buffer_percent": cfg.reorder_buffer_percent,
            "configured_by": cfg.configured_by,
            "recommended_budget": recommended_budget,
            "recommended_reams": recommended_reams,
            "total_demand": total_demand
        }
    else:
        # Unconfigured period: return found: False with None for fields to require manual input
        return {
            "found": False,
            "ream_price": None,
            "sheets_per_booklet": None,
            "yield_per_ream": None,
            "booklet_price": None,
            "department_ream_budget": None,
            "reorder_buffer_percent": None,
            "recommended_budget": 0.0,
            "recommended_reams": 0,
            "total_demand": total_demand
        }


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

            # Query earliest distribution date for this class
            first_booklet = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).order_by(models.Booklet.date_distributed.asc()).first()

            dist_date = first_booklet.date_distributed if (first_booklet and first_booklet.date_distributed) else None
            dist_date_str = dist_date.strftime("%Y-%m-%d") if dist_date else ""
            dist_date_display = dist_date.strftime("%d-%b-%Y") if dist_date else "N/A"

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
                "booklet_price": effective_price,
                "distribution_date": dist_date_str,
                "date_display": dist_date_display
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
        notify_lan_mutation("PAYMENT_RECORDED", {
            "enrollment_id": enrollment_id,
            "amount": req.amount,
            "status": enrollment.payment_status
        }, db=db)
        return {"status": "success", "message": "Payment recorded successfully."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==========================================
# PRIORITY 5: BOOKLET TEMPLATE CUSTOMIZATION
# ==========================================

@app.get("/booklet-template", response_class=HTMLResponse)
def get_booklet_template_page(request: Request):
    return templates.TemplateResponse("booklet_template.html", {"request": request})

@app.get("/api/booklet-template/status")
def get_booklet_template_status(db: Session = Depends(get_db)):
    """Returns the configuration readiness of all 3 slots for both CS and IT booklet cover templates."""
    status_dict = {}
    for dept in ["CS", "IT"]:
        slots_info = []
        has_any_configured = False
        for s in [1, 2, 3]:
            cfg = get_active_template_config(db, department=dept, slot=s)
            raw_pdf = get_raw_template_pdf_path(department=dept, slot=s, config=cfg)
            has_file = bool(raw_pdf and os.path.exists(raw_pdf) and not raw_pdf.endswith("blank_template.pdf") and not raw_pdf.endswith(f"blank_template_{dept}.pdf"))
            is_cfg = bool(cfg and cfg.is_configured and has_file)
            if is_cfg:
                has_any_configured = True
            png_path = f"static/templates/active_template_{dept}_slot{s}_preview.png"
            preview_url = f"/{png_path}?t={int(datetime.now().timestamp())}" if (has_file and os.path.exists(png_path)) else None

            # Stamped proof image path (authentic printed copy with numbers & QR)
            proof_png_path = f"static/front_page_pdfs/template_preview_{dept}_slot{s}_proof.png"
            if has_file and not os.path.exists(proof_png_path):
                try:
                    generate_sample_preview_pdf(department=dept, slot=s, config=cfg, db=db)
                except Exception as e:
                    print(f"Notice: Failed to auto-generate proof for status ({dept} slot {s}): {e}")
            proof_url = f"/{proof_png_path}?t={int(datetime.now().timestamp())}" if (has_file and os.path.exists(proof_png_path)) else None

            slots_info.append({
                "slot": s,
                "slot_number": s,
                "template_name": cfg.template_name or f"Template {s}",
                "is_configured": is_cfg,
                "has_pdf_file": has_file,
                "pdf_filename": cfg.pdf_filename,
                "preview_image_url": preview_url,
                "proof_image_url": proof_url,
                "control_x": cfg.control_x,
                "control_y": cfg.control_y,
                "control_font_size": cfg.control_font_size,
                "control_enabled": cfg.control_enabled,
                "srn_x": cfg.srn_x,
                "srn_y": cfg.srn_y,
                "srn_font_size": cfg.srn_font_size,
                "srn_enabled": cfg.srn_enabled,
                "qr_x": cfg.qr_x,
                "qr_y": cfg.qr_y,
                "qr_size": cfg.qr_size,
                "qr_enabled": cfg.qr_enabled,
                "updated_at": cfg.updated_at.strftime("%Y-%m-%d %H:%M:%S") if (cfg and cfg.updated_at) else ""
            })
        status_dict[dept] = {
            "department": dept,
            "is_configured": has_any_configured,
            "has_any_configured": has_any_configured,
            "slots": slots_info
        }
    return status_dict

@app.get("/api/booklet-template")
def get_booklet_template_api(department: str = "CS", slot: int = 1, db: Session = Depends(get_db)):
    dept = (department or "CS").upper()
    if dept not in ["CS", "IT"]:
        dept = "CS"
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    config = get_active_template_config(db, department=dept, slot=slot_num)
    
    # Ensure active preview image exists if raw PDF file exists on disk
    png_path = f"static/templates/active_template_{dept}_slot{slot_num}_preview.png"
    raw_pdf = get_raw_template_pdf_path(department=dept, slot=slot_num, config=config)
    has_file = bool(raw_pdf and os.path.exists(raw_pdf) and not raw_pdf.endswith("blank_template.pdf") and not raw_pdf.endswith(f"blank_template_{dept}.pdf"))
    
    if not has_file:
        preview_img_url = None
        if os.path.exists(png_path):
            try:
                os.remove(png_path)
            except Exception:
                pass
    else:
        if not os.path.exists(png_path):
            render_template_preview_image(pdf_path=raw_pdf, output_png_path=png_path, department=dept, slot=slot_num)
        preview_img_url = f"/{png_path}?t={int(datetime.now().timestamp())}" if os.path.exists(png_path) else None

    # A4 Landscape dimensions: 841.89 pt x 595.28 pt (297 mm x 210 mm)
    return {
        "id": config.id,
        "department": config.department or dept,
        "slot_number": config.slot_number or slot_num,
        "is_configured": bool(config.is_configured and has_file),
        "has_pdf_file": has_file,
        "template_name": config.template_name or f"Template {slot_num}",
        "pdf_filename": config.pdf_filename,
        "preview_image_url": preview_img_url,
        "page_width_pt": 841.89,
        "page_height_pt": 595.28,
        "page_width_mm": 297.0,
        "page_height_mm": 210.0,
        "control_x": config.control_x,
        "control_y": config.control_y,
        "control_font_size": config.control_font_size,
        "control_enabled": config.control_enabled,
        "srn_x": config.srn_x,
        "srn_y": config.srn_y,
        "srn_font_size": config.srn_font_size,
        "srn_enabled": config.srn_enabled,
        "qr_x": config.qr_x,
        "qr_y": config.qr_y,
        "qr_size": config.qr_size,
        "qr_enabled": config.qr_enabled,
        "updated_at": config.updated_at.strftime("%Y-%m-%d %H:%M:%S") if config.updated_at else ""
    }

@app.post("/api/booklet-template")
def update_booklet_template_api(req: schemas.BookletTemplateUpdate, db: Session = Depends(get_db)):
    dept = (req.department or "CS").upper()
    if dept not in ["CS", "IT"]:
        dept = "CS"
    try:
        slot_num = int(req.slot_number or 1)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    config = get_active_template_config(db, department=dept, slot=slot_num)
    config.department = dept
    config.slot_number = slot_num
    
    # Check if a physical template PDF is uploaded
    raw_pdf = get_raw_template_pdf_path(department=dept, slot=slot_num, config=config)
    has_valid_file = bool(raw_pdf and os.path.exists(raw_pdf) and not raw_pdf.endswith("blank_template.pdf") and not raw_pdf.endswith(f"blank_template_{dept}.pdf"))

    is_official = bool(req.is_official)
    if is_official:
        if not has_valid_file:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot save as official template: Please upload a front-cover PDF for {dept} ({req.template_name or f'Template {slot_num}'}) first."
            )
        config.is_configured = True
    
    config.template_name = req.template_name or config.template_name or f"Template {slot_num}"
    config.control_x = float(req.control_x)
    config.control_y = float(req.control_y)
    config.control_font_size = int(req.control_font_size)
    config.control_enabled = bool(req.control_enabled)
    config.srn_x = float(req.srn_x)
    config.srn_y = float(req.srn_y)
    config.srn_font_size = int(req.srn_font_size)
    config.srn_enabled = bool(req.srn_enabled)
    config.qr_x = float(req.qr_x)
    config.qr_y = float(req.qr_y)
    config.qr_size = float(req.qr_size)
    config.qr_enabled = bool(req.qr_enabled)
    db.commit()
    db.refresh(config)

    # Automatically regenerate proof image with current coordinates
    if has_valid_file:
        try:
            generate_sample_preview_pdf(department=dept, slot=slot_num, config=config, db=db)
        except Exception as e:
            print(f"Notice: Failed to update proof image on save: {e}")

    notify_lan_mutation("TEMPLATE_UPDATED", {"template_id": config.id, "department": dept, "slot": slot_num, "is_official": is_official}, db=db)
    
    msg = f"Official template for {dept} {config.template_name} saved and activated for printing!" if is_official else f"{dept} {config.template_name} draft coordinates saved."
    return {
        "status": "success", 
        "is_configured": bool(config.is_configured and has_valid_file),
        "is_official": is_official,
        "message": msg, 
        "department": dept,
        "slot_number": slot_num,
        "template_name": config.template_name
    }

@app.post("/api/booklet-template/upload")
async def upload_booklet_template_pdf(department: str = "CS", slot: int = 1, file: UploadFile = File(...), db: Session = Depends(get_db)):
    dept = (department or "CS").upper()
    if dept not in ["CS", "IT"]:
        dept = "CS"
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files (.pdf) are allowed as cover templates.")
    
    os.makedirs("static/templates", exist_ok=True)
    target_filename = f"active_template_{dept}_slot{slot_num}.pdf"
    target_path = f"static/templates/{target_filename}"
    
    content = await file.read()
    if len(content) < 100:
        raise HTTPException(status_code=400, detail="Uploaded file is too small or invalid.")
        
    with open(target_path, "wb") as f:
        f.write(content)
        
    # Render new template cover to high-resolution PNG preview
    preview_png_path = f"static/templates/active_template_{dept}_slot{slot_num}_preview.png"
    render_template_preview_image(pdf_path=target_path, output_png_path=preview_png_path, department=dept, slot=slot_num)
    preview_url = f"/{preview_png_path}?t={int(datetime.now().timestamp())}"
    
    config = get_active_template_config(db, department=dept, slot=slot_num)
    config.pdf_filename = target_filename
    config.is_configured = False  # Staged as DRAFT until explicitly saved as official!
    db.commit()

    # Generate initial sample proof image for the newly uploaded PDF
    try:
        generate_sample_preview_pdf(department=dept, slot=slot_num, config=config, db=db)
    except Exception as e:
        print(f"Notice: Failed to generate initial proof image on upload: {e}")

    notify_lan_mutation("TEMPLATE_UPDATED", {"template_id": config.id, "action": "pdf_upload", "department": dept, "slot": slot_num}, db=db)
    return {
        "status": "success", 
        "is_configured": False,
        "has_pdf_file": True,
        "department": dept,
        "slot_number": slot_num,
        "template_name": config.template_name or f"Template {slot_num}",
        "message": f"Cover template for {dept} {config.template_name} uploaded to workspace. Calibrate and save as official template to activate for printing.",
        "preview_image_url": preview_url
    }

@app.get("/api/booklet-template/preview")
def get_booklet_template_preview(department: str = "CS", slot: int = 1, db: Session = Depends(get_db)):
    dept = (department or "CS").upper()
    if dept not in ["CS", "IT"]:
        dept = "CS"
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    config = get_active_template_config(db, department=dept, slot=slot_num)
    preview_path = generate_sample_preview_pdf(department=dept, slot=slot_num, config=config, db=db)
    proof_png_path = f"static/front_page_pdfs/template_preview_{dept}_slot{slot_num}_proof.png"
    timestamp = int(datetime.now().timestamp())
    return {
        "status": "success",
        "department": dept,
        "slot_number": slot_num,
        "template_name": config.template_name or f"Template {slot_num}",
        "preview_url": f"/{preview_path}?t={timestamp}",
        "preview_image_url": f"/{proof_png_path}?t={timestamp}" if os.path.exists(proof_png_path) else None
    }

@app.post("/api/booklet-template/delete")
def delete_booklet_template_api(department: str = "CS", slot: int = 1, db: Session = Depends(get_db)):
    dept = (department or "CS").upper()
    if dept not in ["CS", "IT"]:
        dept = "CS"
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    config = get_active_template_config(db, department=dept, slot=slot_num)
    old_name = config.template_name or f"Template {slot_num}"
    
    # Remove files from disk
    files_to_remove = [
        f"static/templates/active_template_{dept}_slot{slot_num}.pdf",
        f"static/templates/active_template_{dept}_slot{slot_num}_preview.png",
        f"static/front_page_pdfs/template_preview_{dept}_slot{slot_num}.pdf",
        f"static/front_page_pdfs/template_preview_{dept}_slot{slot_num}_proof.png"
    ]
    for file_path in files_to_remove:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Warning: could not delete {file_path}: {e}")

    # Reset config in DB
    config.is_configured = False
    config.pdf_filename = None
    config.template_name = f"Template {slot_num}"
    config.control_x = 500.0
    config.control_y = 400.0
    config.control_font_size = 18
    config.control_enabled = True
    config.srn_x = 500.0
    config.srn_y = 375.0
    config.srn_font_size = 11
    config.srn_enabled = True
    config.qr_x = 680.0
    config.qr_y = 360.0
    config.qr_size = 65.0
    config.qr_enabled = True
    db.commit()
    db.refresh(config)

    notify_lan_mutation("TEMPLATE_DELETED", {"template_id": config.id, "department": dept, "slot": slot_num}, db=db)
    return {
        "status": "success",
        "message": f"{dept} {old_name} has been deleted and reset.",
        "department": dept,
        "slot_number": slot_num
    }

@app.post("/api/booklet-template/reset")
def reset_booklet_template_api(department: str = "CS", slot: int = 1, db: Session = Depends(get_db)):
    dept = (department or "CS").upper()
    if dept not in ["CS", "IT"]:
        dept = "CS"
    try:
        slot_num = int(slot)
        if slot_num not in [1, 2, 3]:
            slot_num = 1
    except Exception:
        slot_num = 1

    config = get_active_template_config(db, department=dept, slot=slot_num)
    config.control_x = 500.0
    config.control_y = 400.0
    config.control_font_size = 18
    config.control_enabled = True
    config.srn_x = 500.0
    config.srn_y = 375.0
    config.srn_font_size = 11
    config.srn_enabled = True
    config.qr_x = 680.0
    config.qr_y = 360.0
    config.qr_size = 65.0
    config.qr_enabled = True
    db.commit()
    db.refresh(config)
    notify_lan_mutation("TEMPLATE_UPDATED", {"template_id": config.id, "action": "reset", "department": dept, "slot": slot_num}, db=db)
    return {
        "status": "success", 
        "message": f"{dept} {config.template_name} layout coordinates restored to factory defaults.", 
        "department": dept,
        "slot_number": slot_num
    }

