from pydantic import BaseModel
from typing import Optional, List

# Base properties for an enrollment
class EnrollmentBase(BaseModel):
    teacher_name: str
    section: str
    subject: str
    student_headcount: int
    department: Optional[str] = None

# Schema used when creating a new enrollment
class EnrollmentCreate(EnrollmentBase):
    pass

class BulkEnrollmentImport(BaseModel):
    records: List[EnrollmentCreate]

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
    scanned_pack_qr: Optional[str] = None
    clerk_name: str = "System Clerk"
    clerk_department: str = "CS"
    selected_loose_controls: List[str] = []
    bridging_pack_qr: Optional[str] = None
    distribution_mode: Optional[str] = "auto"  # "auto", "manual", "hybrid"
    explicit_control_nums: Optional[List[str]] = []

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

class SettingsUpdate(BaseModel):
    current_semester_name: Optional[str] = None
    ream_price: float
    sheets_per_booklet: int
    booklet_price: float
    active_period: str
    department_ream_budget: Optional[float] = 0.0
    reorder_buffer_percent: Optional[float] = 15.0

class BookletTemplateUpdate(BaseModel):
    department: Optional[str] = "CS"
    slot_number: Optional[int] = 1
    template_name: Optional[str] = "Template 1"
    control_x: float
    control_y: float
    control_font_size: int
    control_enabled: bool = True
    srn_x: float
    srn_y: float
    srn_font_size: int
    srn_enabled: bool = True
    qr_x: float
    qr_y: float
    qr_size: float
    qr_enabled: bool = True
    is_official: Optional[bool] = False

class UpdateUserRequest(BaseModel):
    role: str
    department: str

class AdminResetPasswordRequest(BaseModel):
    new_password: str

class ChangePasswordRequest(BaseModel):
    username: str
    current_password: str
    new_password: str


