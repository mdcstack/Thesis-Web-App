import re
from pydantic import BaseModel, field_validator
from typing import Optional, List

def sanitize_clean_string(value: str, max_len: int = 100) -> str:
    """Strips dangerous control chars, HTML/script tags, and limits string length."""
    if value is None:
        return ""
    val = str(value).strip()
    # Strip null bytes and control chars
    val = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', val)
    # Strip script and style tags along with inner contents
    val = re.sub(r'<script\b[^>]*>[\s\S]*?<\/script>', '', val, flags=re.IGNORECASE)
    val = re.sub(r'<style\b[^>]*>[\s\S]*?<\/style>', '', val, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    val = re.sub(r'<[^>]*>', '', val)
    return val[:max_len].strip()

# Base properties for an enrollment
class EnrollmentBase(BaseModel):
    teacher_name: str
    section: str
    subject: str
    student_headcount: int
    department: Optional[str] = None

    @field_validator('teacher_name', mode='before')
    @classmethod
    def clean_teacher(cls, v):
        return sanitize_clean_string(v, max_len=100)

    @field_validator('section', mode='before')
    @classmethod
    def clean_section(cls, v):
        return sanitize_clean_string(v, max_len=30).upper()

    @field_validator('subject', mode='before')
    @classmethod
    def clean_subject(cls, v):
        return sanitize_clean_string(v, max_len=150)

    @field_validator('student_headcount', mode='before')
    @classmethod
    def validate_headcount(cls, v):
        try:
            cnt = int(v)
        except (ValueError, TypeError):
            cnt = 0
        if cnt < 1 or cnt > 500:
            raise ValueError("Student headcount must be between 1 and 500.")
        return cnt

    @field_validator('department', mode='before')
    @classmethod
    def clean_dept(cls, v):
        if not v:
            return None
        return sanitize_clean_string(v, max_len=20).upper()

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
    control_numbers: list[str] = []
    reason: str
    clerk_name: str = "System Clerk"
    cancel_defect_count: int = 0

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
    full_name: Optional[str] = None
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
    reorder_buffer_percent: Optional[float] = 0.0
    budget_is_manual: Optional[bool] = False
    configured_by: Optional[str] = "Administrator"
    staples_box_price: Optional[float] = 0.00
    staples_per_box: Optional[int] = 0
    staples_per_booklet: Optional[int] = 0

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
    updated_by: Optional[str] = None
    role: Optional[str] = None

class UpdateUserRequest(BaseModel):
    full_name: Optional[str] = None
    role: str
    department: str
    updated_by: Optional[str] = "Administrator" 

class AdminResetPasswordRequest(BaseModel):
    new_password: str

class ChangePasswordRequest(BaseModel):
    username: str
    current_password: str
    new_password: str

class ChangeUsernameRequest(BaseModel):
    username: str
    new_username: str
    password: str

class AcademicProgramBase(BaseModel):
    code: str
    name: str
    section_prefix: str
    color_hex: Optional[str] = "#3498db"
    is_active: Optional[bool] = True
    sheets_per_booklet: Optional[int] = None
    booklet_price: Optional[float] = None

class AcademicProgramCreate(AcademicProgramBase):
    pass

class AcademicProgramUpdate(BaseModel):
    name: Optional[str] = None
    section_prefix: Optional[str] = None
    color_hex: Optional[str] = None
    is_active: Optional[bool] = None
    sheets_per_booklet: Optional[int] = None
    booklet_price: Optional[float] = None

class AcademicProgramResponse(AcademicProgramBase):
    id: int

    class Config:
        from_attributes = True

class FacultyRosterItem(BaseModel):
    name: str
    department: Optional[str] = "CS"

    @field_validator('name', mode='before')
    @classmethod
    def clean_name(cls, v):
        return sanitize_clean_string(v, max_len=100)

    @field_validator('department', mode='before')
    @classmethod
    def clean_dept(cls, v):
        if not v:
            return "CS"
        return sanitize_clean_string(v, max_len=20).upper()

class BulkFacultyImport(BaseModel):
    records: List[FacultyRosterItem]

class FacultyClassItem(BaseModel):
    subject: str
    section: str
    student_headcount: int

    @field_validator('subject', mode='before')
    @classmethod
    def clean_subject(cls, v):
        return sanitize_clean_string(v, max_len=150)

    @field_validator('section', mode='before')
    @classmethod
    def clean_section(cls, v):
        return sanitize_clean_string(v, max_len=30).upper()

    @field_validator('student_headcount', mode='before')
    @classmethod
    def validate_headcount(cls, v):
        try:
            cnt = int(v)
        except (ValueError, TypeError):
            cnt = 0
        if cnt < 1 or cnt > 500:
            raise ValueError("Student headcount must be between 1 and 500.")
        return cnt

class FacultyUploadRequest(BaseModel):
    teacher_name: str
    classes: List[FacultyClassItem]

    @field_validator('teacher_name', mode='before')
    @classmethod
    def clean_teacher(cls, v):
        return sanitize_clean_string(v, max_len=100)

class FirstTimeSetupRequest(BaseModel):
    new_password: str

class AdminTriggerPasswordResetRequest(BaseModel):
    manual_only: bool = True

class ForgotPasswordRequest(BaseModel):
    username: Optional[str] = None

    @field_validator('username', mode='before')
    @classmethod
    def clean_id(cls, v):
        return sanitize_clean_string(v, max_len=100).strip() if v else None

class UnlockScheduleRequest(BaseModel):
    reason: Optional[str] = "Schedule update requested"

