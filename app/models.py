from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum
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
    
    # Relationship to track which booklets were given to this class
    distributed_booklets = relationship("Booklet", back_populates="enrollment")

class MasterBatch(Base):
    __tablename__ = "master_batches"

    id = Column(Integer, primary_key=True, index=True)
    qr_code_id = Column(String(255), unique=True, index=True) 
    date_registered = Column(DateTime, default=datetime.now)
    ream_count = Column(Integer, default=1)
    total_yield = Column(Integer, default=83) 
    start_reference = Column(String(255)) 
    end_reference = Column(String(255))   
    status = Column(String(255), default="pending_assembly") 
    
    # Links multiple batches into one physical supply pack
    pack_qr_id = Column(String(255), index=True, nullable=True) 
    
    booklets = relationship("Booklet", back_populates="master_batch")
    
class Booklet(Base):
    __tablename__ = "booklets"

    id = Column(Integer, primary_key=True, index=True)
    reference_num = Column(String(255), unique=True, index=True) # e.g., REF-0001
    
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

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, index=True)
    password = Column(String(255))
    role = Column(String(20))
    department = Column(String(10))

class SystemSettings(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    # Changed from "1st Sem - Midterms" to "UNSET"
    active_period = Column(String(50), default="UNSET")