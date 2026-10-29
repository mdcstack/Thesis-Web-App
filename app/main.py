from collections import defaultdict
import math
import os
import re
import asyncio
import json
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, UploadFile, File
import bcrypt
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_
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


def ensure_program_template_slots(dept_code: str, db: Session):
    """Ensures 3 BookletTemplateConfig slots exist for a given department/program."""
    dept = (dept_code or "CS").upper()
    for slot_idx in [1, 2, 3]:
        cfg = db.query(models.BookletTemplateConfig).filter(
            models.BookletTemplateConfig.department == dept,
            models.BookletTemplateConfig.slot_number == slot_idx
        ).first()
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
            db.add(new_cfg)
    try:
        db.commit()
    except Exception:
        db.rollback()

def ensure_schema_migrations():
    try:
        from sqlalchemy import inspect, text
        import shutil
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        
        # 0. academic_programs table creation & Day-0 defaults
        if 'academic_programs' not in tables:
            models.AcademicProgram.__table__.create(bind=engine, checkfirst=True)
            tables.append('academic_programs')
        
        db_prog_seed = SessionLocal()
        try:
            if db_prog_seed.query(models.AcademicProgram).count() == 0:
                db_prog_seed.add_all([
                    models.AcademicProgram(code="CS", name="Computer Science", section_prefix="BSCS", color_hex="#3498db", is_active=True),
                    models.AcademicProgram(code="IT", name="Information Technology", section_prefix="BSIT", color_hex="#e67e22", is_active=True)
                ])
                db_prog_seed.commit()
        except Exception as seed_err:
            db_prog_seed.rollback()
            print(f"Notice: AcademicProgram seeding: {seed_err}")
        finally:
            db_prog_seed.close()
        
        # 1. system_settings
        if 'system_settings' in tables:
            columns = [c['name'] for c in inspector.get_columns('system_settings')]
            with engine.connect() as conn:
                if 'department_ream_budget' not in columns:
                    conn.execute(text("ALTER TABLE system_settings ADD COLUMN department_ream_budget FLOAT DEFAULT 0.0"))
                    conn.commit()
                if 'reorder_buffer_percent' not in columns:
                    conn.execute(text("ALTER TABLE system_settings ADD COLUMN reorder_buffer_percent FLOAT DEFAULT 0.0"))
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
            active_progs = db_mig.query(models.AcademicProgram).all()
            depts = [p.code for p in active_progs] if active_progs else ["CS", "IT"]
            for dept in depts:
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
            target_dept = resolve_enrollment_department(enr, db=init_db)
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
    valid_programs = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
    if valid_programs and department not in valid_programs:
        department = valid_programs[0]

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

    # Strict Demand Guard: Prevent batch generation if course demand is already 100% fulfilled in current period
    metrics = calculate_course_production_metrics(db, current_period)
    course_metric = metrics["courses"].get(department) if metrics and "courses" in metrics else None

    if course_metric and (course_metric.get("is_satisfied") or course_metric.get("target_production", 0) <= 0 or course_metric.get("reams_needed", 0) <= 0):
        existing_stock = course_metric.get("total_existing_stock", 0)
        headcount = course_metric.get("headcount", 0)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Production Locked: Examination booklet demand for {department} in {current_period} "
                f"is already fully satisfied ({existing_stock} booklets in stock for {headcount} enrolled students). "
                f"Generating additional batches is locked to prevent unnecessary paper material expenses."
            )
        )

    ream_count = request_data.ream_count
    # Deterministic yield capacity based on paper sheets
    yield_per_ream = 500 // settings.sheets_per_booklet
    ream_capacity = ream_count * yield_per_ream
    
    # Cap total_yield to the exact net target_needed (deficit + safety buffer) so excess unassigned barcodes are never produced
    target_needed = course_metric.get("target_production", 0) if course_metric else 0
    total_yield = min(ream_capacity, target_needed) if target_needed > 0 else ream_capacity
    
    # 1. Find the last generated reference number for THIS department (Per-course independent sequence)
    last_booklet = db.query(models.Booklet).join(models.MasterBatch).filter(
        models.MasterBatch.department == department
    ).order_by(models.Booklet.id.desc()).first()
    start_num = 1
    if last_booklet and last_booklet.control_num:
        try:
            start_num = int(last_booklet.control_num.split("-")[-1]) + 1
        except (ValueError, IndexError):
            start_num = 1
        
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
    prefix = settings.current_semester_name or ""
    # Course-specific Control Number prefix: {semester_prefix}-{DEPT}
    course_ctrl_prefix = f"{prefix}-{department}" if prefix else department

    # 3. Calculate Department Sort Record Number (SRN) Range
    last_srn_booklet = db.query(models.Booklet).join(models.MasterBatch).filter(
        models.MasterBatch.department == department,
        models.Booklet.sort_record_num.isnot(None)
    ).order_by(models.Booklet.id.desc()).first()

    start_srn_num = 1
    if last_srn_booklet and last_srn_booklet.sort_record_num:
        try:
            start_srn_num = int(last_srn_booklet.sort_record_num.split("-")[-1]) + 1
        except (ValueError, IndexError):
            start_srn_num = 1

    start_srn_str = f"SRN-{department}-{start_srn_num:06d}"
    end_srn_str = f"SRN-{department}-{(start_srn_num + total_yield - 1):06d}"
    
    new_batch = models.MasterBatch(
        qr_code_id=batch_id,
        department=department,
        ream_count=ream_count,
        total_yield=total_yield, 
        start_control_num=f"{course_ctrl_prefix}-{start_num:04d}",
        end_control_num=f"{course_ctrl_prefix}-{(start_num + total_yield - 1):04d}",
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
        ref_num = f"{course_ctrl_prefix}-{(start_num + i):04d}"
        srn_num = f"SRN-{department}-{(start_srn_num + i):06d}"
        new_booklet = models.Booklet(
            control_num=ref_num,
            sort_record_num=srn_num,
            master_batch_id=new_batch.id,
            status="pending"
        )
        db.add(new_booklet)
    db.commit()
    notify_lan_mutation("STOCK_ADDED", {"ream_count": ream_count, "total_yield": total_yield, "department": department}, db=db)
    
    # 5. Trigger PDF Stamping for the exact capped yield (Zero excess pages produced)
    buffer_pct = float(course_metric.get("buffer_percent", 15.0)) if course_metric else 15.0
    has_warning_page = False  # Yield is strictly capped at target_needed; all pages are active quota
    warning_page_number = None
    recommended_print_range = f"2-{total_yield + 1}"

    pdf_path = generate_batch_pdf(
        start_num,
        total_yield,
        batch_id,
        prefix,
        start_srn_num=start_srn_num,
        department=department,
        slot=template_slot,
        db=db,
        target_needed=target_needed,
        academic_period=current_period,
        buffer_pct=buffer_pct,
        ream_count=ream_count
    )
    
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
        "pdf_download_link": f"/{pdf_path}",
        "target_needed": target_needed,
        "has_warning_page": has_warning_page,
        "warning_page_number": warning_page_number,
        "recommended_print_range": recommended_print_range
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

@app.get("/api/pending-batches")
def get_pending_batches(db: Session = Depends(get_db)):
    """Fetches all batches that are generated (PDF created) but not yet bundled/activated into a Supply Pack."""
    try:
        active_period = get_active_period(db)
        
        # Query all MasterBatch rows that are pending assembly and not yet assigned to an active supply pack
        query = db.query(models.MasterBatch).filter(
            models.MasterBatch.pack_qr_id.is_(None),
            models.MasterBatch.status != "active"
        )
        if active_period and active_period != "UNSET":
            query = query.filter(
                (models.MasterBatch.registered_period == active_period) | 
                (models.MasterBatch.registered_period.is_(None))
            )
            
        batches = query.all()
        
        # Get all active AcademicPrograms for labels, colors, and sorting
        active_progs = db.query(models.AcademicProgram).order_by(models.AcademicProgram.id.asc()).all()
        prog_map = {}
        for p in active_progs:
            prog_map[p.code.upper()] = {
                "code": p.code.upper(),
                "name": p.name,
                "color": p.color_hex or "#2563eb",
                "prefix": getattr(p, "section_prefix", p.code.upper())
            }
        if not prog_map:
            prog_map = {
                "CS": {"code": "CS", "name": "Computer Science", "color": "#2563eb", "prefix": "CS"},
                "IT": {"code": "IT", "name": "Information Technology", "color": "#7c3aed", "prefix": "IT"}
            }
            
        # Get all booklet template configs for human template names
        tpl_configs = db.query(models.BookletTemplateConfig).all()
        tpl_map = {(t.department.upper(), t.slot_number): t.template_name for t in tpl_configs if t.department}

        def extract_control_num(ref_str: Optional[str]) -> int:
            if not ref_str:
                return 0
            try:
                return int(ref_str.split('-')[-1])
            except Exception:
                return 0

        # Sort batches by department, template_slot, then start control number
        sorted_batches = sorted(
            batches, 
            key=lambda b: (
                (b.department or "CS").upper(), 
                b.template_slot or 1, 
                extract_control_num(b.start_control_num)
            )
        )

        batch_list = []
        course_stats = {}
        
        for b in sorted_batches:
            dept = (b.department or "CS").upper()
            prog_info = prog_map.get(dept, {"code": dept, "name": dept, "color": "#2563eb", "prefix": dept})
            slot = b.template_slot or 1
            tpl_name = tpl_map.get((dept, slot)) or f"Template {slot}"
            
            # Count actual booklets if total_yield is null/0
            b_count = b.total_yield or 0
            if not b_count:
                b_count = db.query(models.Booklet).filter(models.Booklet.master_batch_id == b.id).count()

            batch_data = {
                "id": b.id,
                "batch_id": b.qr_code_id,
                "department": dept,
                "department_name": prog_info["name"],
                "department_color": prog_info["color"],
                "department_label": f"{prog_info['name']} ({dept})",
                "template_slot": slot,
                "template_name": tpl_name,
                "ream_count": b.ream_count or 1,
                "total_yield": b_count,
                "start_control_num": b.start_control_num,
                "end_control_num": b.end_control_num,
                "start_num": extract_control_num(b.start_control_num),
                "end_num": extract_control_num(b.end_control_num),
                "start_srn": b.start_srn or "N/A",
                "end_srn": b.end_srn or "N/A",
                "date_registered": b.date_registered.strftime("%Y-%m-%d %H:%M") if b.date_registered else "",
                "status": b.status
            }
            batch_list.append(batch_data)

            if dept not in course_stats:
                course_stats[dept] = {
                    "code": dept,
                    "name": prog_info["name"],
                    "color": prog_info["color"],
                    "pending_batches": 0,
                    "pending_yield": 0,
                    "pending_reams": 0
                }
            course_stats[dept]["pending_batches"] += 1
            course_stats[dept]["pending_yield"] += b_count
            course_stats[dept]["pending_reams"] += (b.ream_count or 1)

        # Return structured courses list including all active programs plus any course with pending batches
        courses_summary = []
        seen_codes = set()
        for code, p in prog_map.items():
            stats = course_stats.get(code, {
                "code": code,
                "name": p["name"],
                "color": p["color"],
                "pending_batches": 0,
                "pending_yield": 0,
                "pending_reams": 0
            })
            courses_summary.append(stats)
            seen_codes.add(code)
        for code, stats in course_stats.items():
            if code not in seen_codes:
                courses_summary.append(stats)

        return {
            "status": "success",
            "active_period": active_period,
            "total_pending_batches": len(batch_list),
            "courses": courses_summary,
            "batches": batch_list
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch pending batches: {str(e)}")


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
    prog = db.query(models.AcademicProgram).filter_by(code=dept).first()
    dept_label = f"{prog.name} ({prog.code})" if prog else ("Computer Science (CS)" if dept == "CS" else ("Information Technology (IT)" if dept == "IT" else dept))
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
            
    # 1b. Validate Course & Template Homogeneity (Cannot mix different course covers)
    departments = set((b.department or "CS").upper() for b in batches)
    if len(departments) > 1:
        active_progs = db.query(models.AcademicProgram).all()
        prog_map = {p.code.upper(): f"{p.name} ({p.code.upper()})" for p in active_progs} if active_progs else {"CS": "Computer Science (CS)", "IT": "Information Technology (IT)"}
        dept_str = " vs ".join(prog_map.get(d, d) for d in departments)
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
    all_loose = db.query(models.Booklet).join(models.MasterBatch).filter(models.Booklet.status == "loose_stock").order_by(models.Booklet.control_num).all()
    
    dept_counts = {}
    for b in all_loose:
        d = (b.master_batch.department or "CS").upper() if b.master_batch else "CS"
        dept_counts[d] = dept_counts.get(d, 0) + 1
        
    filtered = all_loose
    if department and department.strip().upper() not in ["ALL", ""]:
        target = department.strip().upper()
        filtered = [b for b in all_loose if ((b.master_batch.department or "CS").upper() if b.master_batch else "CS") == target]
        
    return {
        "status": "success", 
        "total_count": len(all_loose),
        "counts_by_dept": dept_counts,
        "data": [
            {
                "id": b.id, 
                "control_num": b.control_num, 
                "sort_record_num": b.sort_record_num or "N/A",
                "department": (b.master_batch.department or "CS").upper() if b.master_batch else "CS"
            } 
            for b in filtered
        ]
    }

def detect_department_from_section(section_str: str, db: Optional[Session] = None) -> str:
    """
    Intelligently detects academic department code (e.g. 'CS', 'IT', 'IM')
    by dynamically matching section prefixes configured in the database.
    """
    if not section_str:
        return "CS"
    sec_clean = section_str.strip().upper()
    
    if db:
        try:
            programs = db.query(models.AcademicProgram).filter_by(is_active=True).all()
            if programs:
                # Sort by section_prefix length descending so more specific matches first (e.g. BSCS before CS)
                programs.sort(key=lambda p: len(p.section_prefix), reverse=True)
                for p in programs:
                    pref = p.section_prefix.upper()
                    code = p.code.upper()
                    if pref in sec_clean or f"{code}-" in sec_clean or f"{code} " in sec_clean:
                        return p.code
                # Secondary boundary check against code
                for p in programs:
                    code = p.code.upper()
                    if sec_clean.startswith(code) or f"-{code}" in sec_clean or f" {code}" in sec_clean:
                        return p.code
                return programs[0].code
        except Exception:
            pass
            
    # Heuristic fallback if DB is unavailable
    if "BSIT" in sec_clean or "IT-" in sec_clean or "IT " in sec_clean:
        return "IT"
    return "CS"

def resolve_enrollment_department(enrollment: models.Enrollment, db: Optional[Session] = None) -> str:
    """
    Authoritatively resolve academic department for an enrollment record.
    Prioritizes dynamic section prefix matching. Self-heals the database record in-place.
    """
    if not enrollment:
        return "CS"
        
    detected = detect_department_from_section(enrollment.section, db=db)
    
    if db:
        try:
            programs = db.query(models.AcademicProgram).filter_by(is_active=True).all()
            active_codes = [p.code for p in programs] if programs else ["CS", "IT"]
            
            # If enrollment.department is already valid and section doesn't override with another program
            if enrollment.department and enrollment.department in active_codes:
                correct_dept = detected if detected in active_codes else enrollment.department
            else:
                correct_dept = detected
                
            if enrollment.department != correct_dept:
                enrollment.department = correct_dept
                db.commit()
            return correct_dept
        except Exception:
            db.rollback()
            
    return detected

def get_program_label(code: Optional[str], db: Optional[Session] = None) -> str:
    """Returns human-readable label e.g. 'Computer Science (CS)' or 'Civil Engineering (CE)'."""
    if not code:
        return "Unknown Department"
    c_upper = code.strip().upper()
    if db:
        try:
            prog = db.query(models.AcademicProgram).filter(func.upper(models.AcademicProgram.code) == c_upper).first()
            if prog:
                return f"{prog.name} ({prog.code})"
        except Exception:
            pass
    fallback_map = {
        "CS": "Computer Science (CS)",
        "IT": "Information Technology (IT)",
        "IM": "Information Management (IM)",
        "CE": "Civil Engineering (CE)"
    }
    return fallback_map.get(c_upper, f"Department ({c_upper})")

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
            "id": user.id,
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
        return {
            "status": "valid",
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "department": user.department,
            "updated_by": getattr(user, "updated_by", None) or "Administrator"
        }
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

    # Check if enrollments exist
    enrollment_count = db.query(models.Enrollment).count()
    total_students = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    has_enrollments = bool(enrollment_count > 0 and total_students > 0)

    # 0. Strict check: Has Administrator established System Settings?
    is_period_unset = bool(not settings or not settings.active_period or settings.active_period == "UNSET" or "UNSET" in settings.active_period)
    is_settings_unconfigured = bool(
        is_period_unset or 
        not settings.sheets_per_booklet or 
        settings.sheets_per_booklet <= 0 or 
        not settings.ream_price or 
        settings.ream_price <= 0
    )

    if is_settings_unconfigured:
        return {
            "alert": False,
            "is_configured": False,
            "has_enrollments": has_enrollments,
            "status": "awaiting_settings",
            "message": "System parameters (academic examination period, booklet specifications, and ream pricing) have not been established yet. Awaiting Administrator configuration in Settings.",
            "reams_needed": 0,
            "deficit_booklets": 0,
            "deficit_sheets": 0
        }

    active_period = settings.active_period if settings else None
    metrics = calculate_course_production_metrics(db, active_period)

    sheets_per_b = metrics.get("sheets_per_booklet", 5)
    yield_per_r = metrics.get("yield_per_ream", 100)
    has_system_enrollments = metrics.get("has_enrollments", has_enrollments)

    if not has_system_enrollments:
        return {
            "alert": False,
            "is_configured": True,
            "has_enrollments": False,
            "status": "awaiting_enrollment",
            "message": "No classes have been enrolled yet. Add sections in Manage Classes to activate demand tracking.",
            "reams_needed": 0,
            "deficit_booklets": 0,
            "deficit_sheets": 0,
            "buffer_booklets": 0,
            "buffer_sheets": 0,
            "buffer_percent": 0.0,
            "target_production": 0,
            "total_sheets_needed": 0
        }

    total_deficit = metrics.get("total_deficit", 0)
    total_buffer_booklets = metrics.get("total_buffer_booklets", 0)
    total_target_production = metrics.get("total_target_production", 0)
    total_gross_sheets_needed = metrics.get("total_gross_sheets_needed", 0)
    total_prev_excess_credited = metrics.get("total_prev_excess_credited", 0)
    total_sheets_needed = metrics.get("total_sheets_needed", 0)
    total_reams_to_procure = metrics.get("total_reams_to_procure", 0)
    sum_course_rounded_reams = metrics.get("sum_course_rounded_reams", total_reams_to_procure)
    buffer_pct = metrics.get("buffer_percent", 0.0)

    # Reams recommendation aligns strictly with course-level procurement sum (e.g. 82 whole reams)
    reams_recommended = sum_course_rounded_reams if sum_course_rounded_reams > 0 else total_reams_to_procure

    # 3. Decision Support Logic (Is there production demand / deficit?)
    if total_target_production > 0:
        deficit_sheets = total_deficit * sheets_per_b
        buffer_sheets = total_buffer_booklets * sheets_per_b
        exact_reams = total_sheets_needed // 500
        loose_sheets = total_sheets_needed % 500
        usage_text = f"{exact_reams} Ream(s) and {loose_sheets} paper sheets" if exact_reams > 0 else f"{loose_sheets} paper sheets"

        # Format course breakdown summary for admin guidance
        course_parts = []
        for c_code, c_info in metrics.get("courses", {}).items():
            c_reams = c_info.get("reams_needed", 0)
            course_parts.append(f"{c_code}: {c_reams} Ream{'s' if c_reams != 1 else ''}")
        course_breakdown_text = f" ({', '.join(course_parts)})" if course_parts else ""

        if total_buffer_booklets > 0:
            msg = f"You have an inventory deficit of {total_deficit:,} booklets plus {total_buffer_booklets:,} buffer booklets. Procure {reams_recommended} whole Ream(s){course_breakdown_text} to suffice this demand."
        else:
            msg = f"You have an inventory deficit of {total_deficit:,} booklets. Procure {reams_recommended} whole Ream(s){course_breakdown_text} to suffice this demand."

        return {
            "alert": True,
            "is_configured": True,
            "has_enrollments": True,
            "message": msg,
            "deficit_booklets": total_deficit,
            "deficit_sheets": deficit_sheets,
            "buffer_booklets": total_buffer_booklets,
            "buffer_sheets": buffer_sheets,
            "buffer_percent": buffer_pct,
            "target_production": total_target_production,
            "gross_sheets_needed": total_gross_sheets_needed,
            "prev_excess_credited": total_prev_excess_credited,
            "total_sheets_needed": total_sheets_needed,
            "exact_reams": exact_reams,
            "loose_sheets": loose_sheets,
            "sheets_per_booklet": sheets_per_b,
            "yield_per_ream": yield_per_r,
            "reams_needed": reams_recommended,
            "sum_course_rounded_reams": sum_course_rounded_reams,
            "total_reams_to_procure": reams_recommended,
            "courses": metrics.get("courses", {})
        }

    return {
        "alert": False,
        "is_configured": True,
        "has_enrollments": True,
        "status": "sufficient",
        "message": "Inventory levels are sufficient. No active alerts.",
        "reams_needed": 0,
        "deficit_booklets": 0,
        "deficit_sheets": 0,
        "buffer_booklets": 0,
        "buffer_sheets": 0,
        "buffer_percent": buffer_pct,
        "target_production": 0,
        "total_sheets_needed": 0
    }    

@app.get("/enrollments", response_class=HTMLResponse)
def read_enrollments(request: Request, db: Session = Depends(get_db)):
    enrollments = db.query(models.Enrollment).all()
    return templates.TemplateResponse("enrollments.html", {"request": request, "enrollments": enrollments})

@app.post("/api/enrollments")
def create_enrollment(enr: schemas.EnrollmentCreate, db: Session = Depends(get_db)):
    section_upper = enr.section.strip().upper()
    inferred_dept = detect_department_from_section(section_upper, db=db)

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
            existing.department = detect_department_from_section(sec_upper, db=db)
            updated_count += 1
        else:
            inferred_dept = detect_department_from_section(sec_upper, db=db)

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
        db_enr.department = detect_department_from_section(sec_clean, db=db)
        
        db.commit()
        notify_lan_mutation("ENROLLMENT_UPDATED", {"action": "update", "enrollment_id": enr_id}, db=db)
        return {"status": "success", "message": "Class updated successfully!"}
        
    return {"status": "error", "message": "Record not found"}


from typing import Optional
from pydantic import BaseModel
from datetime import datetime

def reconcile_enrollment_payments(enr: models.Enrollment, db: Session, first_booklet=None):
    """
    Ensures that any amount_paid on an enrollment is fully accounted for in payment_records.
    If an enrollment has amount_paid > sum(recorded payments), auto-creates and persists
    the missing PaymentRecord row in MySQL.
    """
    logged_sum = sum(round(pr.amount, 2) for pr in (enr.payment_records or []))
    unlogged_diff = round((enr.amount_paid or 0.0) - logged_sum, 2)
    if unlogged_diff > 0:
        if first_booklet and getattr(first_booklet, 'date_distributed', None):
            dist_dt = first_booklet.date_distributed
        else:
            fb = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).order_by(models.Booklet.date_distributed.asc()).first()
            dist_dt = fb.date_distributed if (fb and getattr(fb, 'date_distributed', None)) else datetime.now()
        
        balance_after = round(max(0.0, (enr.amount_due or 0.0) - (enr.amount_paid or 0.0)), 2)
        backfill_record = models.PaymentRecord(
            enrollment_id=enr.id,
            amount=unlogged_diff,
            balance_after=balance_after,
            payment_date=dist_dt,
            recorded_by="System Clerk",
            notes="Initial / Reconciled Payment"
        )
        db.add(backfill_record)
        db.commit()
        db.refresh(enr)

@app.get("/api/public-receipts")
def get_public_receipts(db: Session = Depends(get_db)):
    try:
        active_programs = db.query(models.AcademicProgram).filter(models.AcademicProgram.is_active == True).order_by(models.AcademicProgram.code).all()
        programs_data = [
            {
                "code": p.code,
                "name": p.name,
                "color": getattr(p, 'color_hex', '#3498db') or "#3498db",
                "color_hex": getattr(p, 'color_hex', '#3498db') or "#3498db",
                "section_prefix": p.section_prefix or p.code
            }
            for p in active_programs
        ]

        enrollments = db.query(models.Enrollment).all()
        receipts_data = []
        
        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50
        
        for enr in enrollments:
            reconcile_enrollment_payments(enr, db)
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

                    # Payment records audit trail for this enrollment
                    payments_list = []
                    for pr in (enr.payment_records or []):
                        payments_list.append({
                            "id": pr.id,
                            "amount": round(pr.amount, 2),
                            "balance_after": round(pr.balance_after, 2),
                            "payment_date": pr.payment_date.strftime("%Y-%m-%d %H:%M:%S") if pr.payment_date else "",
                            "date_display": pr.payment_date.strftime("%d-%b-%Y %I:%M %p") if pr.payment_date else "N/A",
                            "recorded_by": pr.recorded_by or "System Clerk",
                            "notes": pr.notes or ""
                        })

                    receipts_data.append({
                        "enrollment_id": enr.id,
                        "date": date_str,
                        "teacher": enr.teacher_name,
                        "section": enr.section,
                        "subject": enr.subject,
                        "department": enr.department or "CS",
                        "qty": qty,
                        "unit_price": effective_unit_price,
                        "amount": receipt_amount,
                        "payment_status": enr.payment_status or "Unpaid",
                        "amount_due": round(enr.amount_due or 0.0, 2),
                        "amount_paid": round(enr.amount_paid or 0.0, 2),
                        "balance": round(max(0.0, (enr.amount_due or 0.0) - (enr.amount_paid or 0.0)), 2),
                        "staff": staff_name,
                        "log": log_strings,
                        "exam_period": period if period else "Legacy (No Period)",
                        "payments": payments_list
                    })
        
        return {
            "status": "success", 
            "booklet_price": booklet_price, 
            "programs": programs_data,
            "data": receipts_data
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "programs": [], "data": []}
    

# --- PUBLIC RECEIPTS PORTAL ROUTE ---
@app.get("/receipts", response_class=HTMLResponse)
def public_receipts_portal(request: Request):
    return templates.TemplateResponse("receipts.html", {"request": request})

# --- PUBLIC INSTRUCTOR TRANSACTION PROFILE ROUTE ---
@app.get("/transaction-profile", response_class=HTMLResponse)
def public_transaction_profile_portal(request: Request):
    return templates.TemplateResponse("transaction_profile.html", {"request": request})

@app.get("/api/public-instructor-profiles")
def get_public_instructor_profiles(db: Session = Depends(get_db)):
    try:
        active_programs = db.query(models.AcademicProgram).filter(models.AcademicProgram.is_active == True).order_by(models.AcademicProgram.code).all()
        programs_data = [
            {
                "code": p.code,
                "name": p.name,
                "color": getattr(p, 'color_hex', '#3498db') or "#3498db",
                "color_hex": getattr(p, 'color_hex', '#3498db') or "#3498db",
                "section_prefix": p.section_prefix or p.code
            }
            for p in active_programs
        ]

        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50

        enrollments = db.query(models.Enrollment).all()
        instructors_map = {}

        for enr in enrollments:
            teacher_clean = (enr.teacher_name or "Unknown").strip()
            if not teacher_clean:
                continue

            distributed_booklets = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).order_by(models.Booklet.id).all()

            if not distributed_booklets and (enr.amount_due or 0.0) <= 0:
                continue

            reconcile_enrollment_payments(enr, db)

            if teacher_clean not in instructors_map:
                instructors_map[teacher_clean] = {
                    "teacher_name": teacher_clean,
                    "department": enr.department or "CS",
                    "total_due": 0.0,
                    "total_paid": 0.0,
                    "total_balance": 0.0,
                    "total_qty": 0,
                    "classes": [],
                    "payments": []
                }

            inst = instructors_map[teacher_clean]
            if enr.department and inst["department"] == "CS" and enr.department != "CS":
                inst["department"] = enr.department

            qty = len(distributed_booklets)
            inst["total_qty"] += qty
            due = round(float(enr.amount_due or 0.0), 2)
            paid = round(float(enr.amount_paid or 0.0), 2)
            bal = round(max(0.0, due - paid), 2)

            inst["total_due"] += due
            inst["total_paid"] += paid

            # Class item
            inst["classes"].append({
                "enrollment_id": enr.id,
                "section": enr.section,
                "subject": enr.subject,
                "department": enr.department or "CS",
                "qty": qty,
                "amount_due": due,
                "amount_paid": paid,
                "balance": bal,
                "payment_status": enr.payment_status or "Unpaid"
            })

            # Payment records for this enrollment
            for pr in (enr.payment_records or []):
                inst["payments"].append({
                    "id": pr.id,
                    "enrollment_id": enr.id,
                    "section": enr.section,
                    "subject": enr.subject,
                    "amount": round(pr.amount, 2),
                    "balance_after": round(pr.balance_after, 2),
                    "payment_date": pr.payment_date.strftime("%Y-%m-%d %H:%M:%S") if pr.payment_date else "",
                    "date_display": pr.payment_date.strftime("%d-%b-%Y %I:%M %p") if pr.payment_date else "N/A",
                    "recorded_by": pr.recorded_by or "System Clerk",
                    "notes": pr.notes or ""
                })

        instructors_list = []
        for teacher, inst in instructors_map.items():
            inst["total_due"] = round(inst["total_due"], 2)
            inst["total_paid"] = round(inst["total_paid"], 2)
            inst["total_balance"] = round(max(0.0, inst["total_due"] - inst["total_paid"]), 2)
            
            if inst["total_balance"] <= 0 and inst["total_due"] > 0:
                inst["overall_status"] = "Fully Paid"
            elif inst["total_paid"] > 0:
                inst["overall_status"] = "Partially Paid"
            else:
                inst["overall_status"] = "Unpaid"

            inst["payments"].sort(key=lambda x: x["payment_date"])
            instructors_list.append(inst)

        instructors_list.sort(key=lambda x: x["teacher_name"].lower())

        return {
            "status": "success",
            "booklet_price": booklet_price,
            "programs": programs_data,
            "instructors": instructors_list
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "programs": [], "instructors": []}

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
    exp_dept_clean = expected_dept.strip().upper() if (expected_dept and expected_dept.strip() and expected_dept.strip().upper() not in ["ALL", "NONE"]) else None
    
    # Candidate match conditions
    possible_srn = f"SRN-{raw.zfill(6)}" if raw.isdigit() else raw
    query_conditions = [
        models.Booklet.control_num == raw,
        models.Booklet.sort_record_num == raw,
        models.Booklet.sort_record_num == possible_srn
    ]
    if raw.isdigit():
        if exp_dept_clean:
            query_conditions.append(models.Booklet.sort_record_num == f"SRN-{exp_dept_clean}-{raw.zfill(6)}")
            query_conditions.append(models.Booklet.control_num.like(f"%-{exp_dept_clean}-{raw.zfill(4)}"))
            query_conditions.append(models.Booklet.control_num.like(f"%-{exp_dept_clean}-{raw}"))
        query_conditions.append(models.Booklet.control_num.like(f"%-{raw.zfill(4)}"))
    
    query = db.query(models.Booklet).join(models.MasterBatch).filter(or_(*query_conditions))
    if exp_dept_clean:
        booklet = query.filter(models.MasterBatch.department == exp_dept_clean).first()
        if not booklet:
            booklet = query.first()
    else:
        booklet = query.first()
    
    if not booklet:
        return {"status": "error", "message": f"Booklet '{raw}' was not found in the database."}
        
    batch = booklet.master_batch
    dept = (batch.department if batch and batch.department else "CS").upper()
    
    # Department Verification Guard
    if exp_dept_clean and dept != exp_dept_clean:
        dept_label = get_program_label(dept, db)
        exp_label = get_program_label(exp_dept_clean, db)
        return {
            "status": "error",
            "code": "DEPT_MISMATCH",
            "message": f"Department Mismatch: Booklet '{booklet.control_num}' is for {dept_label}, but active transaction requires {exp_label}."
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
                    dept_label = get_program_label(b_dept, db)
                    enr_label = get_program_label(enrollment_dept, db)
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
            still_needed = remaining_demand

            # Pool 1: Loose Stock
            if req.selected_loose_controls:
                mismatched_loose = db.query(models.Booklet).join(models.MasterBatch).filter(
                    models.Booklet.control_num.in_(req.selected_loose_controls),
                    models.MasterBatch.department != enrollment_dept
                ).count()
                if mismatched_loose > 0:
                    enr_label = get_program_label(enrollment_dept, db)
                    return {
                        "status": "error",
                        "code": "LOOSE_DEPT_MISMATCH",
                        "message": f"Loose Stock Mismatch: Selected loose booklets contain templates from another department. All booklets must match {enr_label}."
                    }

                loose = db.query(models.Booklet).filter(
                    models.Booklet.control_num.in_(req.selected_loose_controls),
                    models.Booklet.status == "loose_stock"
                ).all()
                if loose:
                    booklets_to_distribute.extend(loose)
                    still_needed -= len(loose)

            primary_batch_ids = []
            bridging_batch_ids = []

            # Only verify Primary Pack and Bridging Pack if still needed or explicitly scanned
            if still_needed > 0 or req.scanned_pack_qr:
                primary_batches = db.query(models.MasterBatch).filter(
                    (models.MasterBatch.qr_code_id == req.scanned_pack_qr) |
                    (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)
                ).all()
                if not primary_batches: return {"status": "error", "message": "Invalid Primary Pack QR."}
                primary_batch_ids = [b.id for b in primary_batches]

                # Verify Bridging Pack (If provided)
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
                    batch_dept = (b.department or "CS").upper()
                    if batch_dept != enrollment_dept:
                        dept_label = get_program_label(batch_dept, db)
                        enr_label = get_program_label(enrollment_dept, db)
                        return {
                            "status": "error",
                            "code": "DEPT_MISMATCH",
                            "message": f"Department Template Mismatch: Scanned supply box is for {dept_label}, but section {enrollment.section} is {enr_label}. You cannot distribute {batch_dept} booklet templates to a {enrollment_dept} section."
                        }

                if bridging_batches:
                    for b in bridging_batches:
                        batch_dept = (b.department or "CS").upper()
                        if batch_dept != enrollment_dept:
                            dept_label = get_program_label(batch_dept, db)
                            enr_label = get_program_label(enrollment_dept, db)
                            return {
                                "status": "error",
                                "code": "BRIDGING_DEPT_MISMATCH",
                                "message": f"Bridging Template Mismatch: The bridging pack is for {dept_label}, but section {enrollment.section} is {enr_label}. All booklets in a single distribution must use the same {enr_label} template."
                            }

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
                return {
                    "status": "error",
                    "code": "INSUFFICIENT_STOCK",
                    "shortage": still_needed,
                    "message": f"Insufficient stock even with bridging. Short by {still_needed} pcs."
                }

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
                    dept_label = get_program_label(b_dept, db)
                    enr_label = get_program_label(enrollment_dept, db)
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
            still_needed = remaining_demand

            if req.selected_loose_controls:
                mismatched_loose = db.query(models.Booklet).join(models.MasterBatch).filter(
                    models.Booklet.control_num.in_(req.selected_loose_controls),
                    models.MasterBatch.department != enrollment_dept
                ).count()
                if mismatched_loose > 0:
                    enr_label = get_program_label(enrollment_dept, db)
                    return {
                        "status": "error",
                        "code": "LOOSE_DEPT_MISMATCH",
                        "message": f"Loose Stock Mismatch: Selected loose booklets contain templates from another department. All booklets must match {enr_label}."
                    }

                loose = db.query(models.Booklet).filter(
                    models.Booklet.control_num.in_(req.selected_loose_controls),
                    models.Booklet.status == "loose_stock"
                ).all()
                booklets_to_distribute.extend(loose)
                still_needed -= len(loose)

            primary_batch_ids = []
            bridging_batch_ids = []

            # Only verify Primary Pack and Bridging Pack if still needed or explicitly scanned
            if still_needed > 0 or req.scanned_pack_qr:
                primary_batches = db.query(models.MasterBatch).filter((models.MasterBatch.qr_code_id == req.scanned_pack_qr) | (models.MasterBatch.pack_qr_id == req.scanned_pack_qr)).all()
                if not primary_batches:
                    return {"status": "error", "message": "Invalid Primary Pack QR."}
                primary_batch_ids = [b.id for b in primary_batches]

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
                    batch_dept = (b.department or "CS").upper()
                    if batch_dept != enrollment_dept:
                        dept_label = get_program_label(batch_dept, db)
                        enr_label = get_program_label(enrollment_dept, db)
                        return {
                            "status": "error",
                            "code": "DEPT_MISMATCH",
                            "message": f"Department Template Mismatch: Scanned supply box is for {dept_label}, but section {enrollment.section} is {enr_label}. You cannot distribute {batch_dept} booklet templates to a {enrollment_dept} section."
                        }

                if bridging_batches:
                    for b in bridging_batches:
                        batch_dept = (b.department or "CS").upper()
                        if batch_dept != enrollment_dept:
                            dept_label = get_program_label(batch_dept, db)
                            enr_label = get_program_label(enrollment_dept, db)
                            return {
                                "status": "error",
                                "code": "BRIDGING_DEPT_MISMATCH",
                                "message": f"Bridging Template Mismatch: The bridging pack is for {dept_label}, but section {enrollment.section} is {enr_label}. All booklets in a single distribution must use the same {enr_label} template."
                            }

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
            b.return_reason = None
            
        enrollment.department = resolve_enrollment_department(enrollment, db=db)

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
        enr_map = {e.id: e for e in enrollments}
        active_period = get_active_period(db)
        
        settings = db.query(models.SystemSettings).first()
        booklet_price = settings.booklet_price if settings else 2.50
        
        # High-performance bulk query: fetch all eligible booklets in a single SQL query
        booklets_query = db.query(models.Booklet).filter(
            models.Booklet.enrollment_id.isnot(None),
            models.Booklet.status.in_(["distributed", "voided", "loose_stock"])
        )
        if department.upper() not in ["ALL", "ADMIN"]:
            booklets_query = booklets_query.filter(models.Booklet.enrollment_id.in_(enr_map.keys()))
        
        all_booklets = booklets_query.order_by(models.Booklet.id).all()
        
        # Group booklets by (enrollment_id, exam_period) in Python memory (<5ms)
        groups = defaultdict(lambda: {"distributed": [], "defective": [], "dropped": []})
        for b in all_booklets:
            enr = enr_map.get(b.enrollment_id)
            if not enr:
                continue
            period = b.exam_period if b.exam_period else "Legacy (No Period)"
            key = (enr.id, period)
            if b.status == "distributed":
                groups[key]["distributed"].append(b)
            elif b.status == "voided" and (b.return_reason or "").startswith("Misprint / Defect"):
                groups[key]["defective"].append(b)
            elif b.status == "loose_stock" and (b.return_reason or "").startswith("Student Dropped"):
                groups[key]["dropped"].append(b)
                
        history_data = []
        
        for enr in enrollments:
            enr_periods = [p for (e_id, p) in groups.keys() if e_id == enr.id]
            for period in enr_periods:
                b_dict = groups[(enr.id, period)]
                booklets = b_dict["distributed"]
                defective_bks = b_dict["defective"]
                dropped_bks = b_dict["dropped"]
                
                defective_voided_qty = len(defective_bks)
                dropped_returned_qty = len(dropped_bks)

                if booklets or defective_bks or dropped_bks:
                    qty = len(booklets)
                    if booklets:
                        refs = f"{booklets[0].control_num} ➔ {booklets[-1].control_num}" if qty > 1 else booklets[0].control_num
                        dist_date = getattr(booklets[0], 'date_distributed', None)
                    else:
                        all_prior = defective_bks + dropped_bks
                        all_prior.sort(key=lambda x: x.id)
                        refs = f"{all_prior[0].control_num} ➔ {all_prior[-1].control_num}" if len(all_prior) > 1 else all_prior[0].control_num
                        dist_date = getattr(all_prior[0], 'date_distributed', None)

                    if dist_date:
                        date_str = dist_date.strftime("%d-%b-%Y %I:%M %p")
                        days_old = (datetime.now() - dist_date).days
                    else:
                        date_str = datetime.now().strftime("%d-%b-%Y %I:%M %p")
                        days_old = 0

                    # Determine effective price based on Payment Status Grandfathering Rule
                    is_fully_paid = (enr.payment_status == "Fully Paid" or (enr.amount_due > 0 and enr.amount_paid >= enr.amount_due))
                    b_prices = [getattr(b, 'unit_price', None) for b in (booklets or defective_bks or dropped_bks) if getattr(b, 'unit_price', None) is not None]

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
                    paid_display = transaction_total if is_fully_paid else min(transaction_total, float(enr.amount_paid or 0.0))
                    enr_dept = enr.department or resolve_enrollment_department(enr, db)

                    history_data.append({
                        "enrollment_id": enr.id,
                        "date": date_str,
                        "teacher": enr.teacher_name,
                        "section": enr.section,
                        "subject": enr.subject,
                        "department": enr_dept,
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
                        "dropped_returned_qty": dropped_returned_qty,
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
        "ream_price": float(settings.ream_price or 0.0),
        "sheets_per_booklet": int(settings.sheets_per_booklet or 0),
        "current_semester_name": settings.current_semester_name or "",
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
    settings.booklet_price = None
    settings.sheets_per_booklet = None
    settings.ream_price = None
    settings.department_ream_budget = None
    settings.reorder_buffer_percent = None
    settings.current_semester_name = None
    settings.staples_box_price = None
    settings.staples_per_box = None
    settings.staples_per_booklet = None

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




def get_previous_period_financial_summary(current_period: Optional[str], db: Session):
    """
    Computes financial carryover from the chronologically preceding period configuration:
    1. Previous unspent material budget = max(0.0, prev_budget - prev_production_cost)
    2. Previous DCS net operating profit
    3. Total rollover equity available
    4. Course-by-course leftover excess paper sheets from rounded-up reams
    """
    query = db.query(models.PeriodConfiguration)
    if current_period:
        query = query.filter(models.PeriodConfiguration.academic_period != current_period)
    prev_cfg = query.order_by(models.PeriodConfiguration.updated_at.desc(), models.PeriodConfiguration.id.desc()).first()
    
    if not prev_cfg:
        return {
            "has_previous_period": False,
            "prev_period_name": "",
            "prev_budget": 0.0,
            "prev_reams_conv": 0,
            "prev_paper_cost": 0.0,
            "prev_st_cost": 0.0,
            "prev_prod_cost": 0.0,
            "prev_unspent_budget": 0.0,
            "prev_net_profit": 0.0,
            "total_carryover_available": 0.0,
            "course_excess_sheets": {}
        }

    prev_period = prev_cfg.academic_period
    prev_budget = float(prev_cfg.department_ream_budget or 0.0)
    prev_ream_p = float(prev_cfg.ream_price or 250.0)
    prev_sheets_b = int(prev_cfg.sheets_per_booklet or 6)
    prev_st_box_p = float(prev_cfg.staples_box_price if prev_cfg.staples_box_price is not None else 50.0)
    prev_st_per_box = int(prev_cfg.staples_per_box if prev_cfg.staples_per_box is not None else 1000)
    prev_st_per_b = int(prev_cfg.staples_per_booklet if prev_cfg.staples_per_booklet is not None else 2)
    prev_booklet_p = float(prev_cfg.booklet_price or 2.50)

    # Batches generated in previous period
    prev_batches = db.query(models.MasterBatch).filter(models.MasterBatch.registered_period == prev_period).all()
    prev_generated_b = sum([b.total_yield for b in prev_batches])
    prev_reams_conv = sum([b.ream_count for b in prev_batches])

    # Booklets issued in previous period
    prev_issued_count = db.query(models.Booklet).filter(
        models.Booklet.exam_period == prev_period,
        models.Booklet.status == "distributed"
    ).count()

    # Materials cost in previous period
    prev_paper_cost = round(prev_reams_conv * prev_ream_p, 2)
    prev_st_needed = max(prev_issued_count * prev_st_per_b, prev_generated_b * prev_st_per_b)
    prev_whole_boxes = math.ceil(prev_st_needed / prev_st_per_box) if prev_st_needed > 0 and prev_st_per_box > 0 else 0
    prev_st_cost = round(prev_whole_boxes * prev_st_box_p, 2)
    prev_prod_cost = round(prev_paper_cost + prev_st_cost, 2)

    prev_unspent_budget = max(0.0, round(prev_budget - prev_prod_cost, 2))

    # Net profit in previous period
    prev_gross = round(prev_issued_count * prev_booklet_p, 2)
    prev_dept_share = round(prev_gross * 0.01, 2)
    prev_subtotal = round(prev_gross - prev_dept_share, 2)
    prev_igp_share = round(prev_subtotal * 0.20, 2)
    prev_net_profit = max(0.0, round(prev_subtotal - prev_igp_share - prev_prod_cost, 2))

    total_carryover = round(prev_unspent_budget + prev_net_profit, 2)

    # Calculate excess paper sheets per course from previous period's rounded-up procurement
    active_progs = db.query(models.AcademicProgram).filter_by(is_active=True).all()
    dept_codes = [p.code for p in active_progs] if active_progs else ["CS", "IT"]

    course_excess_map = {}
    for d_code in dept_codes:
        dept_batches = [b for b in prev_batches if b.department == d_code]
        dept_reams = sum([b.ream_count for b in dept_batches])
        dept_procured_sheets = dept_reams * 500
        
        dept_distributed = db.query(models.Booklet).join(models.MasterBatch).filter(
            models.MasterBatch.department == d_code,
            models.Booklet.exam_period == prev_period,
            models.Booklet.status == "distributed"
        ).count()
        dept_consumed_sheets = dept_distributed * prev_sheets_b

        # If batches were recorded on disk, calculate actual leftover sheets:
        if dept_reams > 0:
            excess_sheets = max(0, dept_procured_sheets - dept_consumed_sheets)
        else:
            # Fallback to theoretical round-up excess if batches were not created
            enrollments = db.query(models.Enrollment).all()
            dept_enrollments = [e for e in enrollments if resolve_enrollment_department(e, db) == d_code]
            dept_headcount = sum(e.student_headcount for e in dept_enrollments)
            theo_sheets = dept_headcount * prev_sheets_b
            theo_reams = math.ceil(theo_sheets / 500) if theo_sheets > 0 else 0
            excess_sheets = max(0, (theo_reams * 500) - theo_sheets)

        course_excess_map[d_code] = {
            "reams_procured": dept_reams,
            "sheets_procured": dept_procured_sheets,
            "distributed_booklets": dept_distributed,
            "sheets_consumed": dept_consumed_sheets,
            "excess_sheets": excess_sheets
        }

    return {
        "has_previous_period": True,
        "prev_period_name": prev_period,
        "prev_budget": prev_budget,
        "prev_reams_conv": prev_reams_conv,
        "prev_paper_cost": prev_paper_cost,
        "prev_st_cost": prev_st_cost,
        "prev_prod_cost": prev_prod_cost,
        "prev_unspent_budget": prev_unspent_budget,
        "prev_net_profit": prev_net_profit,
        "total_carryover_available": total_carryover,
        "course_excess_sheets": course_excess_map
    }


# --- COURSE PRODUCTION METRICS (Dynamic & Settings-Driven) ---
def calculate_course_production_metrics(db: Session, active_period: Optional[str] = None):
    """Calculates period-isolated production demand broken down across all active academic courses with dynamic buffer margin and prior period excess paper sheets credit."""
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

    # Query preceding period excess paper sheets per course
    prev_summary = get_previous_period_financial_summary(active_period, db)
    prev_course_excess_map = prev_summary.get("course_excess_sheets", {}) if prev_summary else {}
    prev_period_name = prev_summary.get("prev_period_name", "") if prev_summary else ""

    enrollments = db.query(models.Enrollment).all()
    total_enrollments = len(enrollments)
    total_headcount = sum(e.student_headcount for e in enrollments)
    has_system_enrollments = bool(total_enrollments > 0 and total_headcount > 0)

    # Fetch all active academic programs
    active_progs = db.query(models.AcademicProgram).filter_by(is_active=True).order_by(models.AcademicProgram.id.asc()).all()
    if not active_progs:
        program_list = [
            {"code": "CS", "name": "Computer Science", "prefix": "BSCS", "color": "#3498db"},
            {"code": "IT", "name": "Information Technology", "prefix": "BSIT", "color": "#e67e22"}
        ]
    else:
        program_list = [
            {"code": p.code, "name": p.name, "prefix": p.section_prefix, "color": p.color_hex or "#3498db"}
            for p in active_progs
        ]

    data = {}
    for p_info in program_list:
        dept = p_info["code"]
        dept_enrollments = [e for e in enrollments if resolve_enrollment_department(e, db) == dept]
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
        
        # Available vault stock (packaged and activated supply packs or loose stock)
        available = db.query(models.Booklet).join(models.MasterBatch).filter(
            models.MasterBatch.department == dept,
            models.Booklet.status.in_(["available", "loose_stock"])
        ).count()

        # Pending generated stock (batches stamped/printed on PDF but not yet activated into supply packs)
        pending_stock = db.query(models.Booklet).join(models.MasterBatch).filter(
            models.MasterBatch.department == dept,
            models.Booklet.status == "pending"
        ).count()
        
        total_existing_stock = available + pending_stock
        deficit = max(0, remaining_demand - total_existing_stock)
        buffer_booklets = math.ceil(deficit * (buffer_pct / 100.0)) if deficit > 0 else 0
        target_production = deficit + buffer_booklets
        
        # Excess paper sheets carried over from prior examination period
        prev_excess = prev_course_excess_map.get(dept, {}).get("excess_sheets", 0)

        # Exact paper sheets and breakdown for this specific course
        gross_sheets_needed = target_production * sheets_per_b
        net_sheets_needed = max(0, gross_sheets_needed - prev_excess)

        reams_whole = net_sheets_needed // 500
        sheets_remainder = net_sheets_needed % 500
        reams_needed = math.ceil(net_sheets_needed / 500) if net_sheets_needed > 0 else 0
        course_procured_sheets = reams_needed * 500
        course_excess_sheets = max(0, (course_procured_sheets + prev_excess) - gross_sheets_needed)
        course_excess_booklets = course_excess_sheets // sheets_per_b if sheets_per_b > 0 else 0

        if net_sheets_needed == 0:
            paper_required_text = "0 Sheets (Excess Stock Met)" if gross_sheets_needed > 0 else "0 Sheets (Demand Met)"
        else:
            paper_required_text = f"{reams_whole} Ream(s) & {sheets_remainder} Sheet(s)"
            
        projected_yield = target_production
        
        data[dept] = {
            "code": dept,
            "name": p_info["name"],
            "prefix": p_info["prefix"],
            "color": p_info["color"],
            "headcount": headcount,
            "enrollment_count": dept_enrollment_count,
            "has_enrollments": dept_has_enrollments,
            "distributed": distributed,
            "remaining_demand": remaining_demand,
            "available_stock": available,
            "pending_stock": pending_stock,
            "total_existing_stock": total_existing_stock,
            "deficit": deficit,
            "buffer_percent": buffer_pct,
            "buffer_booklets": buffer_booklets,
            "target_production": target_production,
            "gross_sheets_needed": gross_sheets_needed,
            "prev_excess_sheets": prev_excess,
            "net_sheets_needed": net_sheets_needed,
            "sheets_needed": net_sheets_needed,
            "reams_whole": reams_whole,
            "sheets_remainder": sheets_remainder,
            "paper_required_text": paper_required_text,
            "reams_needed": reams_needed,
            "excess_sheets": course_excess_sheets,
            "excess_booklets_yield": course_excess_booklets,
            "projected_yield": projected_yield,
            "prev_period_name": prev_period_name,
            "is_satisfied": bool(target_production == 0)
        }

    total_distributed = sum(c["distributed"] for c in data.values())
    total_remaining_demand = sum(c["remaining_demand"] for c in data.values())
    total_available_stock = sum(c["available_stock"] for c in data.values())
    total_pending_stock = sum(c["pending_stock"] for c in data.values())
    total_existing_stock = sum(c["total_existing_stock"] for c in data.values())
    total_deficit = sum(c["deficit"] for c in data.values())
    total_buffer_booklets = sum(c["buffer_booklets"] for c in data.values())
    total_target_production = sum(c["target_production"] for c in data.values())
    total_gross_sheets_needed = sum(c["gross_sheets_needed"] for c in data.values())
    total_prev_excess_credited = sum(c["prev_excess_sheets"] for c in data.values())
    total_sheets_needed = sum(c["sheets_needed"] for c in data.values())
    total_reams_to_procure = math.ceil(total_sheets_needed / 500) if total_sheets_needed > 0 else 0
    # Sum of individual course-level rounded-up whole reams
    sum_course_rounded_reams = sum(c["reams_needed"] for c in data.values())
    total_sheets_procured = sum_course_rounded_reams * 500
    excess_sheets = max(0, (total_sheets_procured + total_prev_excess_credited) - total_gross_sheets_needed)
    excess_booklets_yield = excess_sheets // sheets_per_b if sheets_per_b > 0 else 0

    return {
        "status": "success",
        "has_enrollments": has_system_enrollments,
        "total_enrollments": total_enrollments,
        "total_headcount": total_headcount,
        "active_period": active_period,
        "prev_period_name": prev_period_name,
        "sheets_per_booklet": sheets_per_b,
        "yield_per_ream": yield_per_r,
        "buffer_percent": buffer_pct,
        "courses": data,
        "total_deficit": total_deficit,
        "total_buffer_booklets": total_buffer_booklets,
        "total_target_production": total_target_production,
        "total_gross_sheets_needed": total_gross_sheets_needed,
        "total_prev_excess_credited": total_prev_excess_credited,
        "total_sheets_needed": total_sheets_needed,
        "total_reams_to_procure": total_reams_to_procure,
        "sum_course_rounded_reams": sum_course_rounded_reams,
        "total_sheets_procured": total_sheets_procured,
        "excess_sheets": excess_sheets,
        "excess_booklets_yield": excess_booklets_yield,
        "total_reams_needed": total_reams_to_procure
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
            sheets_per_b = period_cfg.sheets_per_booklet or 0
            ream_price = float(period_cfg.ream_price or 0.0)
            booklet_price = float(period_cfg.booklet_price or 0.0)
            department_ream_budget = float(period_cfg.department_ream_budget or 0.0)
            staples_box_price = float(period_cfg.staples_box_price if period_cfg.staples_box_price is not None else 50.0)
            staples_per_box = int(period_cfg.staples_per_box if period_cfg.staples_per_box is not None else 1000)
            staples_per_booklet = int(period_cfg.staples_per_booklet if period_cfg.staples_per_booklet is not None else 2)
        else:
            sheets_per_b = settings.sheets_per_booklet if settings and settings.sheets_per_booklet else 6
            ream_price = float(settings.ream_price or 0.0) if settings else 0.0
            booklet_price = float(settings.booklet_price or 0.0) if settings else 0.0
            department_ream_budget = float(settings.department_ream_budget or 0.0) if settings else 0.0
            staples_box_price = float(settings.staples_box_price if settings and settings.staples_box_price is not None else 50.0)
            staples_per_box = int(settings.staples_per_box if settings and settings.staples_per_box is not None else 1000)
            staples_per_booklet = int(settings.staples_per_booklet if settings and settings.staples_per_booklet is not None else 2)

        yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83

        # Fetch active academic programs
        active_progs = db.query(models.AcademicProgram).filter_by(is_active=True).order_by(models.AcademicProgram.id.asc()).all()
        if not active_progs:
            programs_meta = [
                {"code": "CS", "name": "Computer Science", "prefix": "BSCS", "color": "#3498db"},
                {"code": "IT", "name": "Information Technology", "prefix": "BSIT", "color": "#e67e22"}
            ]
        else:
            programs_meta = [
                {"code": p.code, "name": p.name, "prefix": p.section_prefix, "color": p.color_hex or "#3498db"}
                for p in active_progs
            ]
        prog_codes = [p["code"] for p in programs_meta]

        period_batches = db.query(models.MasterBatch).filter(models.MasterBatch.registered_period == active_period).all()
        total_generated = sum([b.total_yield for b in period_batches])
        reams_converted = sum([b.ream_count for b in period_batches])
        reams_converted_per_dept = {code: sum([b.ream_count for b in period_batches if b.department == code]) for code in prog_codes}
        reams_converted_cs = reams_converted_per_dept.get("CS", 0)
        reams_converted_it = reams_converted_per_dept.get("IT", 0)
        total_generated_sheets = sum([b.total_yield * (b.sheets_per_booklet or sheets_per_b) for b in period_batches])
        
        converted_sheets_per_dept = {}
        for code in prog_codes:
            dept_batches = [b for b in period_batches if b.department == code]
            d_sheets = sum(b.total_yield * (b.sheets_per_booklet or sheets_per_b) for b in dept_batches)
            d_reams_part = d_sheets // 500
            d_sheets_part = d_sheets % 500
            d_reams_whole = sum(b.ream_count for b in dept_batches)
            d_booklets = sum(b.total_yield for b in dept_batches)
            converted_sheets_per_dept[code] = {
                "sheets_total": d_sheets,
                "reams_part": d_reams_part,
                "sheets_part": d_sheets_part,
                "reams_whole": d_reams_whole,
                "booklets": d_booklets
            }
        total_generated_reams_part = total_generated_sheets // 500
        total_generated_sheets_part = total_generated_sheets % 500
        total_generated_rounded_reams = math.ceil(total_generated_sheets / 500) if total_generated_sheets > 0 else 0
        
        # 2. Math for Reams Distributed
        total_distributed_sheets = total_issued * sheets_per_b
        reams_distributed = total_distributed_sheets // 500
        loose_distributed = total_distributed_sheets % 500 
        
        defective_booklets = db.query(models.Booklet).filter(
            models.Booklet.exam_period == active_period,
            models.Booklet.status == "voided",
            models.Booklet.return_reason.like("Misprint / Defect%")
        ).all()

        defective_by_dept = {code: 0 for code in prog_codes}
        for b in defective_booklets:
            if b.enrollment_id:
                enr_obj = db.query(models.Enrollment).filter(models.Enrollment.id == b.enrollment_id).first()
                if enr_obj:
                    dept = resolve_enrollment_department(enr_obj, db)
                    if dept in defective_by_dept:
                        defective_by_dept[dept] += 1
                    else:
                        defective_by_dept[dept] = defective_by_dept.get(dept, 0) + 1
        defective_cs = defective_by_dept.get("CS", 0)
        defective_it = defective_by_dept.get("IT", 0)
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
        year_level_sales = {code: {"1": 0, "2": 0, "3": 0, "4": 0} for code in prog_codes}
        comp_records = {code: {"1": [], "2": [], "3": [], "4": []} for code in prog_codes}
        comp_summary = {code: {"1": 0, "2": 0, "3": 0, "4": 0, "total": 0} for code in prog_codes}
        dept_counts = {code: 0 for code in prog_codes}

        for enr in enrollments:
            b_count = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.exam_period == active_period,
                models.Booklet.status == "distributed"
            ).count()

            if b_count > 0:
                amount_due = float(enr.amount_due if enr.amount_due is not None else (b_count * booklet_price))
                amount_paid = float(enr.amount_paid or 0.0)
                pending_balance = max(0.0, amount_due - amount_paid)
                payment_status = enr.payment_status or ("Paid" if pending_balance == 0 and amount_due > 0 else ("Partial" if amount_paid > 0 else "Unpaid"))

                dept = resolve_enrollment_department(enr, db)
                if dept in dept_counts:
                    dept_counts[dept] += b_count
                else:
                    dept_counts[dept] = b_count

                if dept in year_level_sales:
                    y = next((char for char in enr.section if char.isdigit()), "1")
                    if y in ["1", "2", "3", "4"]:
                        # Record realized collections in year level sales
                        year_level_sales[dept][y] += amount_paid
                        comp_summary[dept][y] += amount_paid
                        comp_summary[dept]["total"] += amount_paid

                        # Track detailed billing metrics in summary
                        if f"{y}_billed" not in comp_summary[dept]:
                            comp_summary[dept][f"{y}_billed"] = 0.0
                            comp_summary[dept][f"{y}_collected"] = 0.0
                            comp_summary[dept][f"{y}_pending"] = 0.0
                        comp_summary[dept][f"{y}_billed"] += amount_due
                        comp_summary[dept][f"{y}_collected"] += amount_paid
                        comp_summary[dept][f"{y}_pending"] += pending_balance

                        if "total_billed" not in comp_summary[dept]:
                            comp_summary[dept]["total_billed"] = 0.0
                            comp_summary[dept]["total_collected"] = 0.0
                            comp_summary[dept]["total_pending"] = 0.0
                        comp_summary[dept]["total_billed"] += amount_due
                        comp_summary[dept]["total_collected"] += amount_paid
                        comp_summary[dept]["total_pending"] += pending_balance

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
                            "amount": amount_paid,
                            "amount_paid": amount_paid,
                            "amount_due": amount_due,
                            "pending_balance": pending_balance,
                            "payment_status": payment_status
                        })

        distributed_sheets_by_dept = {code: dept_counts.get(code, 0) * sheets_per_b for code in prog_codes}
        reams_distributed_by_dept = {code: distributed_sheets_by_dept[code] // 500 for code in prog_codes}
        loose_distributed_by_dept = {code: distributed_sheets_by_dept[code] % 500 for code in prog_codes}
        
        cs_count = dept_counts.get("CS", 0)
        it_count = dept_counts.get("IT", 0)
        reams_distributed_cs = reams_distributed_by_dept.get("CS", 0)
        loose_distributed_cs = loose_distributed_by_dept.get("CS", 0)
        reams_distributed_it = reams_distributed_by_dept.get("IT", 0)
        loose_distributed_it = loose_distributed_by_dept.get("IT", 0)

        course_metrics = calculate_course_production_metrics(db, active_period)

        # --- 4. EXPENDITURE & ROI REPORT (FINANCIALS) ---
        # Faculty Billed, Realized Cash Collections & Accounts Receivable
        billed_by_dept = {code: sum(enr.amount_due for enr in enrollments if resolve_enrollment_department(enr, db) == code) for code in prog_codes}
        collected_by_dept = {code: sum(enr.amount_paid for enr in enrollments if resolve_enrollment_department(enr, db) == code) for code in prog_codes}
        ar_by_dept = {code: sum(max(0.0, enr.amount_due - enr.amount_paid) for enr in enrollments if resolve_enrollment_department(enr, db) == code) for code in prog_codes}

        cs_billed = billed_by_dept.get("CS", 0.0)
        it_billed = billed_by_dept.get("IT", 0.0)
        total_billed = sum(billed_by_dept.values())

        cs_collected = collected_by_dept.get("CS", 0.0)
        it_collected = collected_by_dept.get("IT", 0.0)
        total_collected = sum(collected_by_dept.values())

        accounts_receivable_cs = ar_by_dept.get("CS", 0.0)
        accounts_receivable_it = ar_by_dept.get("IT", 0.0)
        accounts_receivable = sum(ar_by_dept.values())

        # Real-Time Realized Cash Gross Sales Revenue (Recognizing actual collected payments)
        dept_gross = {code: collected_by_dept.get(code, 0.0) for code in prog_codes}
        total_gross = sum(dept_gross.values())
        
        # 1% Program Allocation calculated strictly on realized cash collections
        dept_shares = {code: dept_gross[code] * 0.01 for code in prog_codes}
        total_dept_shares = sum(dept_shares.values())
        
        cs_gross = dept_gross.get("CS", 0.0)
        it_gross = dept_gross.get("IT", 0.0)
        cs_share = dept_shares.get("CS", 0.0)
        it_share = dept_shares.get("IT", 0.0)
        
        combined_subtotal = total_gross - total_dept_shares
        igp_share = combined_subtotal * 0.20
        
        paper_cost = reams_converted * ream_price
        total_staples_consumed = total_generated * staples_per_booklet
        distributed_staples_consumed = total_issued * staples_per_booklet
        staples_needed = max(distributed_staples_consumed, total_staples_consumed)
        staples_boxes_consumed = round(distributed_staples_consumed / staples_per_box, 2) if staples_per_box > 0 else 0.0
        whole_boxes_to_buy = math.ceil(staples_needed / staples_per_box) if staples_needed > 0 and staples_per_box > 0 else 0
        staples_cost = whole_boxes_to_buy * staples_box_price
        production_cost = paper_cost + staples_cost
        net_income = combined_subtotal - igp_share - production_cost

        # Multi-Course Actual Paper Consumption Breakdown (Based on Actual Distributed Booklets & Sheet Conversion)
        course_actual_paper = {}
        total_actual_consumed_sheets = 0
        total_actual_consumed_booklets = 0
        
        for p_info in programs_meta:
            code = p_info["code"]
            dist_b = dept_counts.get(code, 0)
            sheets_used = dist_b * sheets_per_b
            r_whole = sheets_used // 500
            s_rem = sheets_used % 500
            
            # Check if batches were converted for this department
            dept_c_info = converted_sheets_per_dept.get(code, {})
            c_sheets = dept_c_info.get("sheets_total", 0)
            c_reams = dept_c_info.get("reams_whole", 0)
            
            course_actual_paper[code] = {
                "code": code,
                "name": p_info["name"],
                "color": p_info.get("color", "#3498db"),
                "booklets_distributed": dist_b,
                "sheets_consumed": sheets_used,
                "reams_whole": r_whole,
                "sheets_remainder": s_rem,
                "reams_converted": c_reams,
                "converted_sheets": c_sheets,
                "display": f"{r_whole} ream(s) & {s_rem} paper sheets ({sheets_used:,} sheets)" if sheets_used > 0 else (
                    f"{c_reams} ream(s) converted ({c_sheets:,} sheets)" if c_sheets > 0 else "0 paper sheets"
                )
            }
            total_actual_consumed_sheets += sheets_used
            total_actual_consumed_booklets += dist_b

        # Multi-Course Target Paper Breakdown & Exact Production Consumption
        course_target_paper = {}
        total_target_sheets = 0
        total_target_booklets = 0
        
        for p_info in programs_meta:
            code = p_info["code"]
            c_metric = course_metrics.get("courses", {}).get(code, {})
            target_b = c_metric.get("target_production", 0)
            sheets_needed = c_metric.get("sheets_needed", target_b * sheets_per_b)
            r_whole = sheets_needed // 500
            s_rem = sheets_needed % 500
            
            course_target_paper[code] = {
                "code": code,
                "name": p_info["name"],
                "color": p_info.get("color", "#3498db"),
                "target_booklets": target_b,
                "sheets_needed": sheets_needed,
                "reams_whole": r_whole,
                "sheets_remainder": s_rem,
                "display": f"{r_whole} ream(s) & {s_rem} paper sheets" if sheets_needed > 0 else "0 paper sheets"
            }
            total_target_sheets += sheets_needed
            total_target_booklets += target_b
            
        combined_whole_reams_to_procure = math.ceil(total_target_sheets / 500) if total_target_sheets > 0 else 0
        combined_reams_part = total_target_sheets // 500
        combined_sheets_part = total_target_sheets % 500
        combined_usage_display = f"{combined_reams_part} ream{'s' if combined_reams_part != 1 else ''} & {combined_sheets_part} paper sheet{'s' if combined_sheets_part != 1 else ''}"

        # Department Material Budget (Cash Allocation in PHP for paper & staples procurement)
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
            "ar_by_dept": ar_by_dept,
            "cs_billed": cs_billed,
            "it_billed": it_billed,
            "total_billed": total_billed,
            "billed_by_dept": billed_by_dept,
            "cs_collected": cs_collected,
            "it_collected": it_collected,
            "total_collected": total_collected,
            "collected_by_dept": collected_by_dept,
            "accounts_payable": igp_share,
            "paper_cost": paper_cost,
            "staples_cost": staples_cost,
            "production_cost": production_cost,
            "staples_box_price": staples_box_price,
            "staples_per_box": staples_per_box,
            "staples_per_booklet": staples_per_booklet,
            "total_staples_consumed": total_staples_consumed,
            "distributed_booklets": total_issued,
            "distributed_sheets_consumed": total_distributed_sheets,
            "distributed_reams": reams_distributed,
            "distributed_loose_sheets": loose_distributed,
            "distributed_staples_consumed": distributed_staples_consumed,
            "staples_boxes_consumed": staples_boxes_consumed,
            "whole_boxes_to_buy": whole_boxes_to_buy,
            "gross_revenue": total_gross,
            "dept_gross": dept_gross,
            "dept_shares": dept_shares,
            "cs_gross": cs_gross,
            "it_gross": it_gross,
            "cs_share": cs_share,
            "it_share": it_share,
            "combined_subtotal": combined_subtotal,
            "igp_share": igp_share,
            "net_income": net_income,
            "booklet_price": booklet_price,
            "ream_price": ream_price,
            "reorder_buffer_percent": float(course_metrics["buffer_percent"]),
            # Multi-Course Actual Paper Consumption Breakdown
            "course_actual_paper": course_actual_paper,
            "total_actual_consumed_sheets": total_actual_consumed_sheets,
            "total_actual_consumed_booklets": total_actual_consumed_booklets,
            # Multi-Course Exact Target Paper Fields
            "course_target_paper": course_target_paper,
            "total_target_sheets": total_target_sheets,
            "total_target_booklets": total_target_booklets,
            "combined_whole_reams_to_procure": combined_whole_reams_to_procure,
            "combined_reams_part": combined_reams_part,
            "combined_sheets_part": combined_sheets_part,
            "combined_usage_display": combined_usage_display
        }

        # Department chart counts
        dept_chart_counts = {code: dept_counts.get(code, 0) for code in prog_codes}
        dept_chart_counts["Other"] = max(0, total_issued - sum(dept_counts.values()))

        return {
            "status": "success",
            "active_period": active_period,
            "programs": programs_meta,
            "defective_by_dept": defective_by_dept,
            "issued_by_dept": dept_counts,
            "revenue_by_dept": dept_gross,
            "dept_shares": dept_shares,
            "sheets_per_booklet": sheets_per_b,
            "yield_per_ream": yield_per_r,
            "kpi": {
                "reams_converted": reams_converted,
                "reams_converted_per_dept": reams_converted_per_dept,
                "reams_converted_cs": reams_converted_cs,
                "reams_converted_it": reams_converted_it,
                "converted_sheets_per_dept": converted_sheets_per_dept,
                "total_generated": total_generated,
                "total_generated_sheets": total_generated_sheets,
                "total_generated_reams_part": total_generated_reams_part,
                "total_generated_sheets_part": total_generated_sheets_part,
                "total_generated_rounded_reams": total_generated_rounded_reams,
                "course_actual_paper": course_actual_paper,
                "total_actual_consumed_sheets": total_actual_consumed_sheets,
                "total_actual_consumed_booklets": total_actual_consumed_booklets,
                "course_target_paper": course_target_paper,
                "total_target_sheets": total_target_sheets,
                "total_target_booklets": total_target_booklets,
                "combined_whole_reams_to_procure": combined_whole_reams_to_procure,
                "combined_reams_part": combined_reams_part,
                "combined_sheets_part": combined_sheets_part,
                "combined_usage_display": combined_usage_display,
                "reams_distributed": reams_distributed,
                "reams_distributed_per_dept": reams_distributed_by_dept,
                "reams_distributed_cs": reams_distributed_cs,
                "reams_distributed_it": reams_distributed_it,
                "loose_distributed": loose_distributed,
                "loose_distributed_per_dept": loose_distributed_by_dept,
                "loose_distributed_cs": loose_distributed_cs,
                "loose_distributed_it": loose_distributed_it,
                "usage_display": f"{reams_distributed} Ream(s) and {loose_distributed} paper sheets",
                "whole_reams_to_buy": math.ceil(total_distributed_sheets / 500) if total_distributed_sheets > 0 else 0,
                "issued": total_issued,
                "issued_per_dept": dept_counts,
                "issued_cs": cs_count,
                "issued_it": it_count,
                "defective": defective_count,
                "defective_breakdown": {
                    **defective_by_dept,
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
                "departments": dept_chart_counts,
                "defective": {**defective_by_dept, "CS": defective_cs, "IT": defective_it, "Total": defective_count},
                "trend": {"labels": trend_labels, "data": trend_data}
            },
            "year_level_sales": year_level_sales,
            "computations_data": {
                "programs": programs_meta,
                "records": comp_records,
                "summary": comp_summary,
                "sheets_per_booklet": sheets_per_b,
                "yield_per_ream": yield_per_r,
                "dept_gross": dept_gross,
                "dept_shares": dept_shares,
                "dept_billed": billed_by_dept,
                "dept_collected": collected_by_dept,
                "dept_pending": ar_by_dept,
                "total_billed": total_billed,
                "total_collected": total_collected,
                "total_pending": accounts_receivable
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
        "conditions": lambda facts: facts.get('is_settings_unconfigured', False),
        "advice": lambda facts: (
            "⚙️ AWAITING SYSTEM SETTINGS CONFIGURATION: Academic examination period and booklet specifications have not been configured yet. "
            "Please navigate to System Settings to establish the active examination period, sheets per booklet, and ream pricing before generating procurement recommendations."
        ),
        "color": "#f59e0b"
    },
    {
        "conditions": lambda facts: not facts.get('has_enrollments', True),
        "advice": lambda facts: (
            "📋 AWAITING CLASS ENROLLMENTS: No academic classes or student headcounts are registered in the system yet. "
            "The Inference Engine is on standby and will automatically generate heuristic reorder recommendations once class enrollments are established in Manage Classes."
        ),
        "color": "#475569"
    },
    {
        "conditions": lambda facts: facts.get('total_target_production', 0) > 0 and 'Final' in facts.get('period', ''),
        "advice": lambda facts: (
            f"⚠️ SEMESTER ENDING (FINAL EXAM): Production deficit across courses: {len(facts.get('deficit_courses', []))} course(s) require producing {facts.get('total_target_production', 0)} booklets ({facts.get('total_sheets_needed', 0)} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_to_procure', 0)} Ream(s) total — "
            f"{facts.get('courses_breakdown_text', '')}. "
            f"Convert each course under its designated cover template. Do not print excessive buffer as the semester is concluding."
        ),
        "color": "#e74c3c"
    },
    {
        "conditions": lambda facts: facts.get('total_target_production', 0) > 0 and 'Midterm' in facts.get('period', ''),
        "advice": lambda facts: (
            f"🔥 CRITICAL PEAK (MIDTERM EXAM): Production deficit across courses: {len(facts.get('deficit_courses', []))} course(s) require producing {facts.get('total_target_production', 0)} booklets ({facts.get('total_sheets_needed', 0)} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_to_procure', 0)} Ream(s) total — "
            f"{facts.get('courses_breakdown_text', '')} "
            f"({'exact demand with 0% buffer configured in Settings' if facts.get('buffer_pct', 0) <= 0 else f'includes +{facts.get("buffer_pct", 0):g}% safety buffer configured in Settings'}). "
            f"Convert each course under its designated cover template to avoid cover mismatch."
        ),
        "color": "#c0392b"
    },
    {
        "conditions": lambda facts: facts.get('total_target_production', 0) > 0 and 'Midyear' in facts.get('period', ''),
        "advice": lambda facts: (
            f"☀️ MIDYEAR TERM: Production deficit across courses: {len(facts.get('deficit_courses', []))} course(s) require producing {facts.get('total_target_production', 0)} booklets ({facts.get('total_sheets_needed', 0)} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_to_procure', 0)} Ream(s) total — "
            f"{facts.get('courses_breakdown_text', '')}. "
            f"Convert each course under its designated cover template as the Midyear term is concluding."
        ),
        "color": "#e67e22"
    },
    {
        "conditions": lambda facts: facts.get('total_target_production', 0) > 0 and 'Prelims' in facts.get('period', ''),
        "advice": lambda facts: (
            f"📉 NORMAL DEFICIT (PRELIMS): Production deficit across courses: {len(facts.get('deficit_courses', []))} course(s) require producing {facts.get('total_target_production', 0)} booklets ({facts.get('total_sheets_needed', 0)} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_to_procure', 0)} Ream(s) total — "
            f"{facts.get('courses_breakdown_text', '')} "
            f"({'exact demand with 0% buffer configured in Settings' if facts.get('buffer_pct', 0) <= 0 else f'includes +{facts.get("buffer_pct", 0):g}% safety buffer configured in Settings'}). "
            f"Convert each course under its designated cover template."
        ),
        "color": "#e67e22"
    },
    {
        "conditions": lambda facts: facts.get('total_target_production', 0) > 0,
        "advice": lambda facts: (
            f"📦 PRODUCTION SHORTAGE DETECTED: {len(facts.get('deficit_courses', []))} course(s) require producing {facts.get('total_target_production', 0)} booklets ({facts.get('total_sheets_needed', 0)} paper sheets). "
            f"Action: Procure & convert {facts.get('total_reams_to_procure', 0)} Ream(s) total — "
            f"{facts.get('courses_breakdown_text', '')}."
        ),
        "color": "#d97706"
    },
    {
        "conditions": lambda facts: facts.get('total_target_production', 0) == 0,
        "advice": lambda facts: "✅ INVENTORY HEALTHY: Available stock and distributed booklets fully satisfy enrolled student headcounts across all courses. No reordering necessary.",
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
    sheets_per_b = course_metrics.get("sheets_per_booklet", 5)
    yield_per_r = course_metrics.get("yield_per_ream", 100)
    buffer_pct = course_metrics.get("buffer_percent", 3.0)
    buffer_multiplier = 1.0 + (buffer_pct / 100.0)
    
    courses_dict = course_metrics.get("courses", {})
    cs_data = courses_dict.get("CS", {})
    it_data = courses_dict.get("IT", {})
    
    total_deficit = course_metrics.get("total_deficit", 0)
    total_buffer_booklets = course_metrics.get("total_buffer_booklets", 0)
    total_target_production = course_metrics.get("total_target_production", 0)
    total_sheets_needed = course_metrics.get("total_sheets_needed", 0)
    total_reams_to_procure = course_metrics.get("total_reams_to_procure", 0)
    total_sheets_procured = course_metrics.get("total_sheets_procured", 0)
    excess_sheets = course_metrics.get("excess_sheets", 0)
    excess_booklets_yield = course_metrics.get("excess_booklets_yield", 0)
    sum_course_rounded_reams = course_metrics.get("sum_course_rounded_reams", total_reams_to_procure)

    deficit_courses = [v for v in courses_dict.values() if not v.get("is_satisfied", False)]
    satisfied_courses = [v for v in courses_dict.values() if v.get("is_satisfied", False)]

    # Dynamic verbal breakdown across deficit courses
    course_paper_parts = [
        f"{v.get('paper_required_text', '0 Sheets')} for {v.get('name', k)} ({k})"
        for k, v in courses_dict.items()
        if not v.get("is_satisfied", False)
    ]
    if len(course_paper_parts) == 0:
        breakdown_text = "All courses satisfied"
    elif len(course_paper_parts) == 1:
        breakdown_text = course_paper_parts[0]
    else:
        breakdown_text = ", ".join(course_paper_parts[:-1]) + " and " + course_paper_parts[-1]

    # Check if settings are unconfigured
    is_period_unset = bool(not period_name or period_name == "UNSET" or "UNSET" in period_name)
    sys_settings = db.query(models.SystemSettings).first()
    is_settings_unconfigured = bool(
        is_period_unset or 
        not sys_settings or 
        not sys_settings.sheets_per_booklet or 
        sys_settings.sheets_per_booklet <= 0 or 
        not sys_settings.ream_price or 
        sys_settings.ream_price <= 0
    )

    facts = {
        'available_stock': available_stock,
        'pending_demand': pending_demand,
        'total_students': total_students,
        'enrollment_count': enrollment_count,
        'has_enrollments': has_enrollments,
        'is_settings_unconfigured': is_settings_unconfigured,
        'period': period_name,
        'sheets_per_booklet': sheets_per_b,
        'yield_per_ream': yield_per_r,
        'buffer_pct': buffer_pct,
        'buffer_multiplier': buffer_multiplier,
        'course_metrics': course_metrics,
        'courses_breakdown_text': breakdown_text,
        'cs_data': cs_data,
        'it_data': it_data,
        'total_deficit': total_deficit,
        'total_buffer_booklets': total_buffer_booklets,
        'total_target_production': total_target_production,
        'total_sheets_needed': total_sheets_needed,
        'total_reams_to_procure': total_reams_to_procure,
        'total_sheets_procured': total_sheets_procured,
        'excess_sheets': excess_sheets,
        'excess_booklets_yield': excess_booklets_yield,
        'deficit_courses': deficit_courses,
        'satisfied_courses': satisfied_courses,
        'total_reams_needed': total_reams_to_procure
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
        expert_advice.append("System analyzing active period and inventory variables...")

    # 3. Build Structured Recommendation Object for Modern UI
    if is_settings_unconfigured:
        status_title = "Awaiting System Settings Configuration"
        status_type = "standby"
        badge_color = "#f59e0b"
        summary_text = "System parameters (academic examination period, booklet specifications, and ream pricing) have not been established yet. The Inference Engine is awaiting Administrator setup in Settings before generating procurement orders."
        action_steps = [
            "Navigate to System Settings to establish the active examination period and academic year.",
            "Configure bond paper sheets per booklet, ream acquisition price, and reorder buffer margin.",
            "Once saved, the Inference Engine will calculate the exact paper reams required for all academic courses."
        ]
    elif not has_enrollments:
        status_title = "Awaiting Class Enrollments"
        status_type = "standby"
        badge_color = "#475569"
        summary_text = "No academic classes or student headcounts are registered in the system yet. The Inference Engine is on standby and will generate reorder recommendations once class enrollments are established."
        action_steps = [
            "Navigate to Manage Classes to register instructor sections and student headcounts.",
            "Verify curriculum subjects and academic program assignments before printing booklets."
        ]
    elif total_target_production > 0:
        if "Final" in period_name:
            status_title = "Semester Ending (Final Exam)"
            status_type = "danger"
            badge_color = "#e74c3c"
        elif "Midterm" in period_name:
            status_title = "Critical Peak Demand (Midterm Exam)"
            status_type = "critical"
            badge_color = "#c0392b"
        elif "Midyear" in period_name:
            status_title = "Midyear Term Demand"
            status_type = "warning"
            badge_color = "#e67e22"
        elif "Prelims" in period_name:
            status_title = "Normal Deficit (Prelims Exam)"
            status_type = "warning"
            badge_color = "#e67e22"
        else:
            status_title = "Production Shortage Detected"
            status_type = "warning"
            badge_color = "#d97706"

        buf_note = f" (+{buffer_pct:g}% safety buffer included)" if buffer_pct > 0 else ""
        procure_reams_display = sum_course_rounded_reams if sum_course_rounded_reams > 0 else total_reams_to_procure
        procure_sheets_display = procure_reams_display * 500
        excess_sheets_display = max(0, procure_sheets_display - total_sheets_needed)
        excess_booklets_display = excess_sheets_display // sheets_per_b if sheets_per_b > 0 else 0

        summary_text = (
            f"Production needed across {len(deficit_courses)} course(s): Total target of {total_target_production:,} booklets "
            f"({total_sheets_needed:,} paper sheets){buf_note}. "
            f"Procure {procure_reams_display:,} Whole Ream(s) total from suppliers."
        )

        action_steps = [
            f"Procure {procure_reams_display:,} Whole Ream(s) of bond paper ({procure_sheets_display:,} sheets total, 500 sheets/ream)."
        ]
        for c in deficit_courses:
            action_steps.append(
                f"Print & convert {c['target_production']:,} booklets for {c['name']} ({c['code']}) — exact paper: {c['paper_required_text']} ({c['sheets_needed']:,} sheets) using [{c['prefix']}] template."
            )
        if excess_sheets > 0:
            action_steps.append(
                f"Reserve the remaining {excess_sheets:,} excess paper sheets as buffer stock (yields up to ~{excess_booklets_yield:,} reserve booklets)."
            )
        action_steps.append(
            "Scan and activate generated Master QR packs in Booklet Generator before releasing to instructors."
        )
    else:
        status_title = "Inventory Healthy (All Demands Met)"
        status_type = "success"
        badge_color = "#27ae60"
        summary_text = "Available vault stock and distributed booklets fully satisfy all enrolled student headcounts across all courses. No paper reorders or printing required at this time."
        action_steps = [
            "All active courses currently meet or exceed student headcount requirements.",
            "Monitor ongoing distributions in Live POS and track any dropped or misprinted returns in Distribution History."
        ]

    course_allocations = [
        {
            "code": c["code"],
            "name": c["name"],
            "prefix": c["prefix"],
            "color": c["color"],
            "headcount": c["headcount"],
            "distributed": c["distributed"],
            "available_stock": c["available_stock"],
            "deficit": c["deficit"],
            "buffer_booklets": c["buffer_booklets"],
            "target_production": c["target_production"],
            "gross_sheets_needed": c.get("gross_sheets_needed", 0),
            "prev_excess_sheets": c.get("prev_excess_sheets", 0),
            "net_sheets_needed": c.get("net_sheets_needed", c["sheets_needed"]),
            "sheets_needed": c["sheets_needed"],
            "reams_needed": c.get("reams_needed", math.ceil(c["sheets_needed"] / 500) if c["sheets_needed"] > 0 else 0),
            "excess_sheets": c.get("excess_sheets", 0),
            "excess_booklets_yield": c.get("excess_booklets_yield", 0),
            "reams_whole": c["reams_whole"],
            "sheets_remainder": c["sheets_remainder"],
            "paper_required_text": c["paper_required_text"],
            "is_satisfied": c["is_satisfied"]
        }
        for c in courses_dict.values()
    ]

    recommendation = {
        "status_title": status_title,
        "status_type": status_type,
        "badge_color": badge_color,
        "summary": summary_text,
        "total_reams_to_procure": sum_course_rounded_reams,
        "sum_course_rounded_reams": sum_course_rounded_reams,
        "total_sheets_needed": total_sheets_needed,
        "total_sheets_procured": procure_sheets_display if total_target_production > 0 else 0,
        "excess_sheets": excess_sheets_display if total_target_production > 0 else 0,
        "excess_booklets_yield": excess_booklets_display if total_target_production > 0 else 0,
        "total_target_production": total_target_production,
        "total_deficit": total_deficit,
        "total_buffer_booklets": total_buffer_booklets,
        "buffer_pct": buffer_pct,
        "sheets_per_booklet": sheets_per_b,
        "active_period": period_name,
        "course_allocations": course_allocations,
        "action_steps": action_steps
    }

    return {
        "facts": facts,
        "advice": expert_advice[0],
        "color": advice_color,
        "recommendation": recommendation,
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
    admin_actor = getattr(req, "updated_by", None) or "Administrator"
    user.role = req.role
    user.department = final_dept
    user.updated_by = admin_actor
    db.commit()
    db.refresh(user)
    
    notify_lan_mutation("USER_UPDATED", {
        "action": "update",
        "user_id": user_id,
        "username": user.username,
        "role": user.role,
        "department": final_dept,
        "updated_by": admin_actor
    }, db=db)
    return {
        "status": "success", 
        "message": f"User '{user.username}' updated successfully!",
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "department": user.department,
            "updated_by": admin_actor
        }
    }

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
    reorder_buffer_percent: Optional[float] = 0.0
    configured_by: Optional[str] = "Administrator"
    staples_box_price: Optional[float] = 0.00
    staples_per_box: Optional[int] = 0
    staples_per_booklet: Optional[int] = 0

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
        sem_prefix = settings.current_semester_name if (settings.current_semester_name and settings.current_semester_name != "") else None
    else:
        sheets_per_b = settings.sheets_per_booklet if (settings.sheets_per_booklet and settings.sheets_per_booklet > 0) else 6
        yield_per_r = 500 // sheets_per_b if sheets_per_b > 0 else 83
        leftover = 500 % sheets_per_b if sheets_per_b > 0 else 2
        ream_p = float(settings.ream_price or 0.0)
        booklet_p = float(settings.booklet_price or 0.0)
        budget = float(settings.department_ream_budget or 0.0)
        buffer_pct = float(settings.reorder_buffer_percent if settings.reorder_buffer_percent is not None else 15.0)
        sem_prefix = settings.current_semester_name

    total_demand = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    period_name = settings.active_period if (settings and settings.active_period and "UNSET" not in settings.active_period) else None
    course_metrics = calculate_course_production_metrics(db, period_name)
    course_demand_list = [
        {
            "code": c["code"],
            "name": c["name"],
            "headcount": c["headcount"],
            "target_production": c["target_production"],
            "gross_sheets_needed": c.get("gross_sheets_needed", 0),
            "prev_excess_sheets": c.get("prev_excess_sheets", 0),
            "net_sheets_needed": c.get("net_sheets_needed", c["sheets_needed"]),
            "sheets_needed": c["sheets_needed"],
            "reams_needed": c["reams_needed"],
            "excess_sheets": c.get("excess_sheets", 0),
            "excess_booklets_yield": c.get("excess_booklets_yield", 0)
        }
        for c in course_metrics.get("courses", {}).values()
    ]
    sum_course_rounded_reams = course_metrics.get("sum_course_rounded_reams", 0)
    total_booklet_demand = course_metrics.get("total_target_production", total_demand)
    if total_booklet_demand == 0:
        total_booklet_demand = total_demand

    if yield_per_r and yield_per_r > 0 and total_demand > 0 and ream_p and ream_p > 0:
        recommended_reams = sum_course_rounded_reams if sum_course_rounded_reams > 0 else math.ceil(total_demand / yield_per_r)
        paper_budget = recommended_reams * ream_p

        st_box_price = float(settings.staples_box_price if settings.staples_box_price is not None else 50.0)
        st_per_box = int(settings.staples_per_box if settings.staples_per_box is not None else 1000)
        st_per_booklet = int(settings.staples_per_booklet if settings.staples_per_booklet is not None else 2)

        total_staples = total_booklet_demand * st_per_booklet
        staples_boxes = math.ceil(total_staples / st_per_box) if st_per_box > 0 else 0
        staples_budget = staples_boxes * st_box_price

        recommended_budget = round(paper_budget + staples_budget, 2)
    else:
        recommended_reams = sum_course_rounded_reams
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
        "total_booklet_demand": total_booklet_demand,
        "course_demands": course_demand_list,
        "sum_course_rounded_reams": sum_course_rounded_reams,
        "recommended_reams": recommended_reams,
        "recommended_budget": recommended_budget,
        "staples_box_price": float(settings.staples_box_price if settings.staples_box_price is not None else 50.0),
        "staples_per_box": int(settings.staples_per_box if settings.staples_per_box is not None else 1000),
        "staples_per_booklet": int(settings.staples_per_booklet if settings.staples_per_booklet is not None else 2),
        "previous_summary": get_previous_period_financial_summary(period_name, db),
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
        derived_semester_name = req.current_semester_name or ""
    
    old_price = float(settings.booklet_price) if settings.booklet_price is not None else 0.0
    new_price = float(req.booklet_price)

    settings.current_semester_name = derived_semester_name
    settings.ream_price = req.ream_price
    settings.sheets_per_booklet = req.sheets_per_booklet
    settings.booklet_price = new_price
    settings.active_period = req.active_period
    settings.department_ream_budget = float(req.department_ream_budget or 0.0)
    settings.budget_is_manual = bool(req.budget_is_manual)
    settings.reorder_buffer_percent = float(req.reorder_buffer_percent if req.reorder_buffer_percent is not None else 15.0)
    settings.staples_box_price = float(req.staples_box_price if req.staples_box_price is not None else 50.0)
    settings.staples_per_box = int(req.staples_per_box if req.staples_per_box is not None else 1000)
    settings.staples_per_booklet = int(req.staples_per_booklet if req.staples_per_booklet is not None else 2)

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
    period_cfg.staples_box_price = settings.staples_box_price
    period_cfg.staples_per_box = settings.staples_per_box
    period_cfg.staples_per_booklet = settings.staples_per_booklet
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
        "staples_box_price": float(c.staples_box_price if c.staples_box_price is not None else 50.0),
        "staples_per_box": int(c.staples_per_box if c.staples_per_box is not None else 1000),
        "staples_per_booklet": int(c.staples_per_booklet if c.staples_per_booklet is not None else 2),
        "configured_by": c.configured_by,
        "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M:%S") if c.updated_at else ""
    } for c in configs]

@app.get("/api/period-configuration")
def get_period_configuration(period: str, db: Session = Depends(get_db)):
    """Returns saved parameters for a specific period, or smart defaults if new, plus preceding period financial carryover."""
    cfg = db.query(models.PeriodConfiguration).filter(
        models.PeriodConfiguration.academic_period == period
    ).first()

    total_demand = int(db.query(func.sum(models.Enrollment.student_headcount)).scalar() or 0)
    prev_summary = get_previous_period_financial_summary(period, db)
    course_metrics = calculate_course_production_metrics(db, period)
    course_demand_list = [
        {
            "code": c["code"],
            "name": c["name"],
            "headcount": c["headcount"],
            "target_production": c["target_production"],
            "sheets_needed": c["sheets_needed"],
            "reams_needed": c["reams_needed"]
        }
        for c in course_metrics.get("courses", {}).values()
    ]
    sum_course_rounded_reams = course_metrics.get("sum_course_rounded_reams", 0)
    total_booklet_demand = course_metrics.get("total_target_production", total_demand)
    if total_booklet_demand == 0:
        total_booklet_demand = total_demand
    
    if cfg:
        yield_per_r = cfg.yield_per_ream or (500 // cfg.sheets_per_booklet)
        if total_demand > 0 and yield_per_r > 0 and cfg.ream_price:
            recommended_reams = sum_course_rounded_reams if sum_course_rounded_reams > 0 else math.ceil(total_demand / yield_per_r)
            paper_budget = recommended_reams * float(cfg.ream_price)

            st_box_price = float(cfg.staples_box_price if cfg.staples_box_price is not None else 50.0)
            st_per_box = int(cfg.staples_per_box if cfg.staples_per_box is not None else 1000)
            st_per_booklet = int(cfg.staples_per_booklet if cfg.staples_per_booklet is not None else 2)

            total_staples = total_booklet_demand * st_per_booklet
            staples_boxes = math.ceil(total_staples / st_per_box) if st_per_box > 0 else 0
            staples_budget = staples_boxes * st_box_price

            recommended_budget = round(paper_budget + staples_budget, 2)
        else:
            recommended_reams = sum_course_rounded_reams
            recommended_budget = 0.0
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
            "staples_box_price": float(cfg.staples_box_price if cfg.staples_box_price is not None else 50.0),
            "staples_per_box": int(cfg.staples_per_box if cfg.staples_per_box is not None else 1000),
            "staples_per_booklet": int(cfg.staples_per_booklet if cfg.staples_per_booklet is not None else 2),
            "configured_by": cfg.configured_by,
            "recommended_budget": recommended_budget,
            "recommended_reams": recommended_reams,
            "total_demand": total_demand,
            "total_booklet_demand": total_booklet_demand,
            "course_demands": course_demand_list,
            "sum_course_rounded_reams": sum_course_rounded_reams,
            "previous_period_summary": prev_summary
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
            "staples_box_price": 50.0,
            "staples_per_box": 1000,
            "staples_per_booklet": 2,
            "recommended_budget": 0.0,
            "recommended_reams": sum_course_rounded_reams,
            "total_demand": total_demand,
            "total_booklet_demand": total_booklet_demand,
            "course_demands": course_demand_list,
            "sum_course_rounded_reams": sum_course_rounded_reams,
            "previous_period_summary": prev_summary
        }


# ==========================================
# PHASE 3: INSTRUCTOR PAYMENT TRACKING
# ==========================================

from pydantic import BaseModel
class PaymentRequest(BaseModel):
    amount: float
    recorded_by: Optional[str] = "System Clerk"
    notes: Optional[str] = None


# --- ACADEMIC PROGRAMS CONFIGURATION (Dynamic Multi-Course Engine) ---

@app.get("/api/academic-programs")
def list_academic_programs(include_inactive: bool = False, db: Session = Depends(get_db)):
    """Returns all configured academic programs/courses with section prefix identifiers and theme colors."""
    query = db.query(models.AcademicProgram)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    programs = query.order_by(models.AcademicProgram.id.asc()).all()
    return {
        "status": "success",
        "programs": [
            {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "section_prefix": p.section_prefix,
                "color_hex": p.color_hex or "#3498db",
                "is_active": p.is_active,
                "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "",
                "updated_at": p.updated_at.strftime("%Y-%m-%d %H:%M:%S") if p.updated_at else ""
            }
            for p in programs
        ]
    }

@app.post("/api/academic-programs")
def create_academic_program(prog: schemas.AcademicProgramCreate, db: Session = Depends(get_db)):
    """Registers a new academic program with code, full name, section naming prefix, and visual color."""
    code_clean = prog.code.strip().upper()
    name_clean = prog.name.strip()
    prefix_clean = prog.section_prefix.strip().upper()
    color_clean = (prog.color_hex or "#3498db").strip()

    if not code_clean:
        raise HTTPException(status_code=400, detail="Course Code is required (e.g. 'IM', 'CPE').")
    if not name_clean:
        raise HTTPException(status_code=400, detail="Full Course Name is required (e.g. 'Information Management').")
    if not prefix_clean:
        raise HTTPException(status_code=400, detail="Section Naming Identifier Prefix is required (e.g. 'BSIM').")

    # Check for existing duplicate code
    existing_code = db.query(models.AcademicProgram).filter(
        func.upper(models.AcademicProgram.code) == code_clean
    ).first()
    if existing_code:
        if not existing_code.is_active:
            # Reactivate if it was previously deactivated
            existing_code.is_active = True
            existing_code.name = name_clean
            existing_code.section_prefix = prefix_clean
            existing_code.color_hex = color_clean
            db.commit()
            ensure_program_template_slots(code_clean, db)
            notify_lan_mutation("PROGRAM_UPDATED", {"code": code_clean, "action": "reactivate"}, db=db)
            return {"status": "success", "message": f"Program '{code_clean}' reactivated successfully!", "id": existing_code.id}
        raise HTTPException(status_code=400, detail=f"Course Code '{code_clean}' already exists.")

    # Check for duplicate prefix
    existing_prefix = db.query(models.AcademicProgram).filter(
        func.upper(models.AcademicProgram.section_prefix) == prefix_clean
    ).first()
    if existing_prefix and existing_prefix.is_active:
        raise HTTPException(status_code=400, detail=f"Section Prefix '{prefix_clean}' is already assigned to {existing_prefix.name} ({existing_prefix.code}).")

    new_prog = models.AcademicProgram(
        code=code_clean,
        name=name_clean,
        section_prefix=prefix_clean,
        color_hex=color_clean,
        is_active=True
    )
    db.add(new_prog)
    db.commit()
    db.refresh(new_prog)

    # Immediately ensure 3 booklet template config slots are generated for this new department
    ensure_program_template_slots(code_clean, db)

    notify_lan_mutation("PROGRAM_CREATED", {"code": code_clean, "name": name_clean}, db=db)
    return {
        "status": "success",
        "message": f"Academic Course '{name_clean} ({code_clean})' registered successfully with section prefix '{prefix_clean}'!",
        "program": {
            "id": new_prog.id,
            "code": new_prog.code,
            "name": new_prog.name,
            "section_prefix": new_prog.section_prefix,
            "color_hex": new_prog.color_hex,
            "is_active": new_prog.is_active
        }
    }

@app.put("/api/academic-programs/{prog_id}")
def update_academic_program(prog_id: int, prog: schemas.AcademicProgramUpdate, db: Session = Depends(get_db)):
    """Updates academic program metadata (name, section prefix, color, or active status)."""
    item = db.query(models.AcademicProgram).filter(models.AcademicProgram.id == prog_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Academic program not found.")

    if prog.name is not None and prog.name.strip():
        item.name = prog.name.strip()
    if prog.section_prefix is not None and prog.section_prefix.strip():
        pref = prog.section_prefix.strip().upper()
        # Verify no collision with other active programs
        collision = db.query(models.AcademicProgram).filter(
            func.upper(models.AcademicProgram.section_prefix) == pref,
            models.AcademicProgram.id != prog_id,
            models.AcademicProgram.is_active == True
        ).first()
        if collision:
            raise HTTPException(status_code=400, detail=f"Section Prefix '{pref}' is already in use by {collision.name} ({collision.code}).")
        item.section_prefix = pref
    if prog.color_hex is not None and prog.color_hex.strip():
        item.color_hex = prog.color_hex.strip()
    if prog.is_active is not None:
        item.is_active = bool(prog.is_active)

    db.commit()
    notify_lan_mutation("PROGRAM_UPDATED", {"id": prog_id, "code": item.code}, db=db)
    return {"status": "success", "message": f"Program '{item.code}' updated successfully!"}

@app.delete("/api/academic-programs/{prog_id}")
def delete_academic_program(prog_id: int, db: Session = Depends(get_db)):
    """Safely deactivates or removes an academic program while preserving relational integrity."""
    item = db.query(models.AcademicProgram).filter(models.AcademicProgram.id == prog_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Academic program not found.")

    # Prevent deleting core defaults CS or IT if they are the only two
    total_active = db.query(models.AcademicProgram).filter_by(is_active=True).count()
    if total_active <= 1:
        raise HTTPException(status_code=400, detail="Cannot deactivate the last remaining academic program. The system must have at least one active course.")

    # Referential check: are there classes or booklet batches tied to this department?
    has_enrollments = db.query(models.Enrollment).filter(models.Enrollment.department == item.code).count()
    has_batches = db.query(models.MasterBatch).filter(models.MasterBatch.department == item.code).count()
    has_users = db.query(models.User).filter(models.User.department == item.code).count()

    if has_enrollments > 0 or has_batches > 0 or has_users > 0:
        # Soft-deactivate for audit & historical ledger safety
        item.is_active = False
        db.commit()
        notify_lan_mutation("PROGRAM_UPDATED", {"id": prog_id, "code": item.code, "action": "deactivated"}, db=db)
        return {
            "status": "success",
            "message": f"Program '{item.code}' has active transactions or enrollments, so it was safely deactivated instead of deleted. Historical reports remain preserved.",
            "action": "deactivated"
        }
    else:
        # Clean hard delete
        code_deleted = item.code
        # Also clean up unconfigured template slots for this department
        db.query(models.BookletTemplateConfig).filter(models.BookletTemplateConfig.department == code_deleted).delete()
        db.delete(item)
        db.commit()
        notify_lan_mutation("PROGRAM_DELETED", {"id": prog_id, "code": code_deleted}, db=db)
        return {
            "status": "success",
            "message": f"Academic Program '{code_deleted}' removed successfully.",
            "action": "deleted"
        }


@app.get("/payment-tracking", response_class=HTMLResponse)
def payment_tracking_page(request: Request):
    return templates.TemplateResponse("payment_tracking.html", {"request": request})

@app.get("/department-breakdown", response_class=HTMLResponse)
def department_breakdown_page(request: Request):
    return templates.TemplateResponse("department_breakdown.html", {"request": request})

@app.get("/expenditure-report", response_class=HTMLResponse)
def expenditure_report_page(request: Request):
    return templates.TemplateResponse("expenditure_report.html", {"request": request})

def reconcile_enrollment_payments(enr: models.Enrollment, db: Session, first_booklet=None):
    """
    Ensures that any amount_paid on an enrollment is fully accounted for in payment_records.
    If an enrollment has amount_paid > sum(recorded payments), auto-creates and persists
    the missing PaymentRecord row in MySQL.
    """
    logged_sum = sum(round(pr.amount, 2) for pr in (enr.payment_records or []))
    unlogged_diff = round((enr.amount_paid or 0.0) - logged_sum, 2)
    if unlogged_diff > 0:
        if first_booklet and first_booklet.date_distributed:
            dist_dt = first_booklet.date_distributed
        else:
            fb = db.query(models.Booklet).filter(
                models.Booklet.enrollment_id == enr.id,
                models.Booklet.status == "distributed"
            ).order_by(models.Booklet.date_distributed.asc()).first()
            dist_dt = fb.date_distributed if (fb and fb.date_distributed) else datetime.now()
        
        balance_after = round(max(0.0, (enr.amount_due or 0.0) - (enr.amount_paid or 0.0)), 2)
        backfill_record = models.PaymentRecord(
            enrollment_id=enr.id,
            amount=unlogged_diff,
            balance_after=balance_after,
            payment_date=dist_dt,
            recorded_by="System Clerk",
            notes="Initial / Reconciled Payment"
        )
        db.add(backfill_record)
        db.commit()
        db.refresh(enr)


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
        query = query.options(joinedload(models.Enrollment.payment_records))
        enrollments = query.all()

        if not enrollments:
            return {"status": "success", "data": []}

        enr_ids = [enr.id for enr in enrollments]

        # Bulk count query for distributed and loose_stock booklets
        counts_rows = db.query(
            models.Booklet.enrollment_id,
            models.Booklet.status,
            func.count(models.Booklet.id)
        ).filter(
            models.Booklet.enrollment_id.in_(enr_ids),
            models.Booklet.status.in_(["distributed", "loose_stock"])
        ).group_by(models.Booklet.enrollment_id, models.Booklet.status).all()

        booklet_counts = {}
        for enr_id, status, cnt in counts_rows:
            booklet_counts[(enr_id, status)] = cnt

        # Bulk earliest distribution date query
        earliest_rows = db.query(
            models.Booklet.enrollment_id,
            func.min(models.Booklet.date_distributed)
        ).filter(
            models.Booklet.enrollment_id.in_(enr_ids),
            models.Booklet.status == "distributed"
        ).group_by(models.Booklet.enrollment_id).all()

        earliest_dates = {enr_id: dt for enr_id, dt in earliest_rows}

        data = []
        for enr in enrollments:
            distributed_count = booklet_counts.get((enr.id, "distributed"), 0)
            loose_returned = booklet_counts.get((enr.id, "loose_stock"), 0)

            is_fully_paid = (enr.payment_status == "Fully Paid" or (enr.amount_due > 0 and enr.amount_paid >= enr.amount_due))
            effective_price = round(enr.amount_due / distributed_count, 2) if (is_fully_paid and distributed_count > 0) else current_price

            dist_date = earliest_dates.get(enr.id)
            dist_date_str = dist_date.strftime("%Y-%m-%d") if dist_date else ""
            dist_date_display = dist_date.strftime("%d-%b-%Y") if dist_date else "N/A"

            # Check if reconciliation needed (in-memory sum check first)
            logged_sum = sum(round(pr.amount, 2) for pr in (enr.payment_records or []))
            if round((enr.amount_paid or 0.0) - logged_sum, 2) > 0:
                reconcile_enrollment_payments(enr, db, earliest_date=dist_date)

            # Serialize payment installments audit trail
            payments_list = []
            for pr in (enr.payment_records or []):
                payments_list.append({
                    "id": pr.id,
                    "amount": round(pr.amount, 2),
                    "balance_after": round(pr.balance_after, 2),
                    "payment_date": pr.payment_date.strftime("%Y-%m-%d %H:%M:%S") if pr.payment_date else "",
                    "date_display": pr.payment_date.strftime("%d-%b-%Y %I:%M %p") if pr.payment_date else "N/A",
                    "recorded_by": pr.recorded_by or "System Clerk",
                    "notes": pr.notes or ""
                })

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
                "date_display": dist_date_display,
                "payments": payments_list
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
            
        new_amount_paid = enrollment.amount_paid + req.amount
        new_balance = round(max(0.0, enrollment.amount_due - new_amount_paid), 2)
        enrollment.amount_paid = new_amount_paid
        
        # Recalculate status
        if enrollment.amount_paid >= enrollment.amount_due:
            enrollment.payment_status = "Fully Paid"
        elif enrollment.amount_paid > 0:
            enrollment.payment_status = "Partially Paid"
        else:
            enrollment.payment_status = "Unpaid"
            
        # Create and persist audit record
        payment_record = models.PaymentRecord(
            enrollment_id=enrollment.id,
            amount=round(req.amount, 2),
            balance_after=new_balance,
            payment_date=datetime.now(),
            recorded_by=req.recorded_by or "System Clerk",
            notes=req.notes or ""
        )
        db.add(payment_record)
        db.commit()
        db.refresh(payment_record)

        notify_lan_mutation("PAYMENT_RECORDED", {
            "enrollment_id": enrollment_id,
            "amount": req.amount,
            "balance_after": new_balance,
            "status": enrollment.payment_status,
            "recorded_by": payment_record.recorded_by,
            "payment_date": payment_record.payment_date.strftime("%Y-%m-%d %H:%M:%S")
        }, db=db)
        return {
            "status": "success", 
            "message": "Payment recorded successfully.",
            "payment_id": payment_record.id,
            "balance_after": new_balance,
            "payment_status": enrollment.payment_status
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==========================================

@app.get("/api/enrollments/{enrollment_id}/payment-history")
def get_enrollment_payment_history(enrollment_id: int, db: Session = Depends(get_db)):
    try:
        enrollment = db.query(models.Enrollment).filter(models.Enrollment.id == enrollment_id).first()
        if not enrollment:
            return {"status": "error", "message": "Enrollment not found."}
        
        reconcile_enrollment_payments(enrollment, db)
        
        payments_list = []
        for pr in enrollment.payment_records:
            payments_list.append({
                "id": pr.id,
                "amount": round(pr.amount, 2),
                "balance_after": round(pr.balance_after, 2),
                "payment_date": pr.payment_date.strftime("%Y-%m-%d %H:%M:%S") if pr.payment_date else "",
                "date_display": pr.payment_date.strftime("%d-%b-%Y %I:%M %p") if pr.payment_date else "N/A",
                "recorded_by": pr.recorded_by or "System Clerk",
                "notes": pr.notes or ""
            })
        return {
            "status": "success",
            "enrollment_id": enrollment.id,
            "teacher_name": enrollment.teacher_name,
            "amount_due": enrollment.amount_due,
            "amount_paid": enrollment.amount_paid,
            "balance": round(max(0.0, enrollment.amount_due - enrollment.amount_paid), 2),
            "payment_status": enrollment.payment_status,
            "payments": payments_list
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# PRIORITY 5: BOOKLET TEMPLATE CUSTOMIZATION
# ==========================================

@app.get("/booklet-template", response_class=HTMLResponse)
def get_booklet_template_page(request: Request):
    return templates.TemplateResponse("booklet_template.html", {"request": request})

@app.get("/api/booklet-template/status")
def get_booklet_template_status(db: Session = Depends(get_db)):
    """Returns the configuration readiness of all 3 slots across all registered academic programs."""
    status_dict = {}
    active_progs = db.query(models.AcademicProgram).filter_by(is_active=True).order_by(models.AcademicProgram.id.asc()).all()
    depts = [p.code for p in active_progs] if active_progs else ["CS", "IT"]
    for dept in depts:
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
            needs_regen = has_file and (
                not os.path.exists(proof_png_path) or 
                (raw_pdf and os.path.exists(raw_pdf) and os.path.getmtime(raw_pdf) > os.path.getmtime(proof_png_path))
            )
            if needs_regen:
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
    valid_codes = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
    if valid_codes and dept not in valid_codes:
        dept = valid_codes[0]
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
    valid_codes = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
    if valid_codes and dept not in valid_codes:
        dept = valid_codes[0]
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
    valid_codes = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
    if valid_codes and dept not in valid_codes:
        dept = valid_codes[0]
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
    valid_codes = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
    if valid_codes and dept not in valid_codes:
        dept = valid_codes[0]
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
    valid_codes = [p.code for p in db.query(models.AcademicProgram).filter_by(is_active=True).all()]
    if valid_codes and dept not in valid_codes:
        dept = valid_codes[0]
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

