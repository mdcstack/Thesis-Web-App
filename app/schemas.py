from pydantic import BaseModel
from typing import Optional, List

# Base properties for an enrollment
class EnrollmentBase(BaseModel):
    teacher_name: str
    section: str
    subject: str
    student_headcount: int

# Schema used when creating a new enrollment
class EnrollmentCreate(EnrollmentBase):
    pass

# Schema used when reading/returning an enrollment from the database
class Enrollment(EnrollmentBase):
    id: int

    class Config:
        from_attributes = True

class ReturnRequest(BaseModel):
    enrollment_id: int
    control_numbers: list[str]
    reason: str
    clerk_name: str = "System Clerk"

class DistributionRequest(BaseModel):
    enrollment_id: int
    scanned_pack_qr: str
    clerk_name: str = "System Clerk"
    clerk_department: str = "CS"
    selected_loose_controls: List[str] = []
    bridging_pack_qr: Optional[str] = None

class VerifySessionRequest(BaseModel):
    username: str
    session_token: str

class LogoutRequest(BaseModel):
    username: str

class SaveQRRequest(BaseModel):
    pack_id: str
    base64_image: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str
    department: str = "CS"


