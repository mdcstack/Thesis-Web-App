from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, Float, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base

class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    teacher_name = Column(String(255), index=True)
    section = Column(String(255), index=True)
    subject = Column(String(255))
    student_headcount = Column(Integer)
    
    # Phase 3: Payment Tracking fields
    amount_due = Column(Float, default=0.0)
    amount_paid = Column(Float, default=0.0)
    payment_status = Column(String(50), default="Unpaid")
    replacement_credit = Column(Float, default=0.0)
    
    # Phase 4: Department Isolation
    department = Column(String(50), default="CS")
    
    # Relationship to track which booklets were given to this class
    distributed_booklets = relationship("Booklet", back_populates="enrollment")

class MasterBatch(Base):
    __tablename__ = "master_batches"

    id = Column(Integer, primary_key=True, index=True)
    qr_code_id = Column(String(255), unique=True, index=True) 
    department = Column(String(50), default="CS", index=True)
    date_registered = Column(DateTime, default=datetime.now)
    ream_count = Column(Integer, default=1)
    total_yield = Column(Integer, default=83) 
    start_control_num = Column(String(255)) 
    end_control_num = Column(String(255))   
    start_srn = Column(String(50), nullable=True)
    end_srn = Column(String(50), nullable=True)
    status = Column(String(255), default="pending_assembly") 
    registered_period = Column(String(255), nullable=True) 
    
    # Links multiple batches into one physical supply pack
    pack_qr_id = Column(String(255), index=True, nullable=True) 
    
    # Financial and production lock-ins for the semester
    ream_price = Column(Float, default=0.0)
    sheets_per_booklet = Column(Integer, default=6)
    template_slot = Column(Integer, default=1)

    # Exclusive session-bound user claim
    claimed_by = Column(String(50), nullable=True)

    booklets = relationship("Booklet", back_populates="master_batch")
    
class Booklet(Base):
    __tablename__ = "booklets"

    id = Column(Integer, primary_key=True, index=True)
    control_num = Column(String(255), unique=True, index=True) # e.g., 1ST26-0001
    sort_record_num = Column(String(50), unique=True, index=True, nullable=True) # e.g., SRN-000001
    
    # Links back to the batch it was printed from
    master_batch_id = Column(Integer, ForeignKey("master_batches.id"))
    master_batch = relationship("MasterBatch", back_populates="booklets")
    
    # Core statuses for hybrid allocation: "available", "distributed", "loose_stock"
    status = Column(String(255), default="available") 
    
    # If distributed, link it to the specific teacher/section
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), nullable=True)
    enrollment = relationship("Enrollment", back_populates="distributed_booklets")
    
    date_distributed = Column(DateTime, nullable=True)
    distributed_by = Column(String(255), nullable=True, default="System Clerk")
    return_reason = Column(String(255), nullable=True)
    exam_period = Column(String(50), nullable=True)
    unit_price = Column(Float, default=2.50)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, index=True)
    password = Column(String(255))
    role = Column(String(20))
    department = Column(String(10))
    session_token = Column(String(255), nullable=True)

class SystemSettings(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    # Changed from "1st Sem - Midterms" to "UNSET"
    active_period = Column(String(50), default="UNSET")
    
    # Global Semester Configurations
    current_semester_name = Column(String(50), default="1ST26")
    ream_price = Column(Float, default=250.00)
    sheets_per_booklet = Column(Integer, default=6)
    booklet_price = Column(Float, default=0.0)
    department_ream_budget = Column(Float, default=0.0)
    budget_is_manual = Column(Boolean, default=False)
    reorder_buffer_percent = Column(Float, default=15.0)
    data_version = Column(Integer, default=1)

class PeriodConfiguration(Base):
    __tablename__ = "period_configurations"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    academic_period = Column(String(100), unique=True, index=True, nullable=False) # e.g. "1st Sem - Midterm Exam | AY 2026-2027"
    semester_prefix = Column(String(50), nullable=False) # e.g. "1ST26"
    ream_price = Column(Float, default=250.00)
    sheets_per_booklet = Column(Integer, default=6)
    yield_per_ream = Column(Integer, default=83)
    booklet_price = Column(Float, default=2.50)
    department_ream_budget = Column(Float, default=0.0)
    budget_is_manual = Column(Boolean, default=False)
    reorder_buffer_percent = Column(Float, default=15.0)
    configured_by = Column(String(50), default="Administrator")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class BookletTemplateConfig(Base):
    __tablename__ = "booklet_template_configs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    department = Column(String(20), default="CS", index=True)
    slot_number = Column(Integer, default=1, index=True)
    is_configured = Column(Boolean, default=False)
    template_name = Column(String(100), default="Template 1")
    pdf_filename = Column(String(255), nullable=True)
    
    # Control Number Layout (Coordinates in PDF points from bottom-left)
    control_x = Column(Float, default=500.0)
    control_y = Column(Float, default=400.0)
    control_font_size = Column(Integer, default=18)
    control_enabled = Column(Boolean, default=True)
    
    # Sort Record Number (SRN) Layout
    srn_x = Column(Float, default=500.0)
    srn_y = Column(Float, default=375.0)
    srn_font_size = Column(Integer, default=11)
    srn_enabled = Column(Boolean, default=True)
    
    # Individual Booklet QR Code Layout
    qr_x = Column(Float, default=680.0)
    qr_y = Column(Float, default=360.0)
    qr_size = Column(Float, default=65.0)
    qr_enabled = Column(Boolean, default=True)
    
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)