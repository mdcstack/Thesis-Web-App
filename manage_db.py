import os
import sys
import json
import re
import datetime
from urllib.parse import urlparse
import pymysql
import bcrypt

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.database import SQLALCHEMY_DATABASE_URL
from app import models

# Configure UTF-8 stdout
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def parse_db_url(url_str):
    """Extracts connection parameters from sqlalchemy connection string."""
    clean_url = url_str.replace("mysql+pymysql://", "mysql://")
    parsed = urlparse(clean_url)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "db": parsed.path.lstrip("/") or "bookletlog"
    }

def get_server_connection(db_params):
    """Establishes connection directly to MySQL server without selecting a specific database."""
    return pymysql.connect(
        host=db_params["host"],
        port=db_params["port"],
        user=db_params["user"],
        password=db_params["password"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )

def drop_and_create_database(db_params):
    """Drops and recreates the target database schema."""
    conn = get_server_connection(db_params)
    try:
        with conn.cursor() as cursor:
            db_name = db_params["db"]
            print(f"[1/3] Dropping database `{db_name}` if it exists...")
            cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            print(f"[2/3] Creating fresh database `{db_name}`...")
            cursor.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            conn.commit()
            print(f"[3/3] Fresh database `{db_name}` successfully initialized.")
    finally:
        conn.close()

def create_schema_tables():
    """Initializes all SQLAlchemy models/tables using create_all."""
    from app.database import engine
    from app.main import ensure_schema_migrations
    print("Generating all database tables via SQLAlchemy Base...")
    models.Base.metadata.create_all(bind=engine)
    ensure_schema_migrations()
    print("All 7 tables generated successfully.")

def hash_pw(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def cleanup_workspace_files():
    """
    Cleans generated batch PDFs, sample proofs, QR labels, and staged template covers
    so the filesystem matches the Day-0 database state.
    Preserves base_template.pdf in the root directory.
    """
    print("\nCleaning workspace files and test artifacts...")
    cleaned_count = 0

    # 1. Front page PDFs (BATCH-*.pdf, template_preview_*.pdf, proof PNGs)
    front_pdf_dir = os.path.join(PROJECT_ROOT, "static", "front_page_pdfs")
    if os.path.exists(front_pdf_dir):
        for fname in os.listdir(front_pdf_dir):
            if fname.lower().endswith((".pdf", ".png")):
                fpath = os.path.join(front_pdf_dir, fname)
                try:
                    os.remove(fpath)
                    cleaned_count += 1
                except Exception as e:
                    print(f"  Notice: could not remove {fname}: {e}")

    # 2. QR code labels (PACK-*_LABEL.png, *.png)
    qr_dir = os.path.join(PROJECT_ROOT, "static", "qrcodes")
    if os.path.exists(qr_dir):
        for fname in os.listdir(qr_dir):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                fpath = os.path.join(qr_dir, fname)
                try:
                    os.remove(fpath)
                    cleaned_count += 1
                except Exception as e:
                    print(f"  Notice: could not remove {fname}: {e}")

    # 3. Uploaded template covers and preview thumbnails
    tmpl_dir = os.path.join(PROJECT_ROOT, "static", "templates")
    if os.path.exists(tmpl_dir):
        for fname in os.listdir(tmpl_dir):
            if fname.lower().endswith((".pdf", ".png")):
                fpath = os.path.join(tmpl_dir, fname)
                try:
                    os.remove(fpath)
                    cleaned_count += 1
                except Exception as e:
                    print(f"  Notice: could not remove {fname}: {e}")

    print(f"Workspace file sweep completed: {cleaned_count} test file(s) removed.")
    return cleaned_count

def perform_full_clean_reset(db_params):
    """
    Complete Full Database and Workspace Reset
    - Drops and recreates the target database (bookletlog)
    - Generates all tables via SQLAlchemy Base and runs schema migrations
    - Seeds single default Administrator account ('admin' / 'admin123')
    - Resets SystemSettings to Day-0 ('active_period'='UNSET', booklet_price=0.0)
    - Seeds 6 default unconfigured BookletTemplateConfig slots (CS 1-3, IT 1-3)
    - Sweeps workspace files (BATCH-*.pdf, PACK-*.png, active_template_*)
    """
    print("\n" + "=" * 62)
    print("   STARTING COMPLETE DATABASE & WORKSPACE RESET")
    print("=" * 62)

    # 1. Drop & Recreate Database
    drop_and_create_database(db_params)

    # 2. Create Tables & Migrations
    create_schema_tables()

    # 3. Clean Workspace Files
    removed_files = cleanup_workspace_files()

    # 4. Seed Minimal Cold-Start Records
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        print("\nSeeding clean Administrator account & Day-0 defaults...")
        
        # Admin account
        admin = models.User(
            username="admin",
            password=hash_pw("admin123"),
            role="Admin",
            department="All"
        )
        db.add(admin)

        # Unconfigured System Settings (Triggers Startup Setup Wizard)
        settings = models.SystemSettings(
            active_period="UNSET",
            current_semester_name="1ST26",
            ream_price=250.0,
            booklet_price=0.0,
            sheets_per_booklet=6,
            department_ream_budget=0.0,
            budget_is_manual=False,
            reorder_buffer_percent=15.0,
            data_version=1
        )
        db.add(settings)

        # 6 Default Booklet Template Slots: 3 slots per department (Unconfigured)
        for dept in ["CS", "IT"]:
            for slot in [1, 2, 3]:
                tpl = db.query(models.BookletTemplateConfig).filter_by(department=dept, slot_number=slot).first()
                if not tpl:
                    tpl = models.BookletTemplateConfig(
                        department=dept,
                        slot_number=slot
                    )
                    db.add(tpl)
                tpl.is_configured = False
                tpl.template_name = f"Template {slot}"
                tpl.pdf_filename = None
                tpl.control_x = 500.0
                tpl.control_y = 400.0
                tpl.control_font_size = 18
                tpl.control_enabled = True
                tpl.srn_x = 500.0
                tpl.srn_y = 375.0
                tpl.srn_font_size = 11
                tpl.srn_enabled = True
                tpl.qr_x = 680.0
                tpl.qr_y = 360.0
                tpl.qr_size = 65.0
                tpl.qr_enabled = True

        db.commit()

        # Summary Verification Query
        user_count = db.query(models.User).count()
        settings_count = db.query(models.SystemSettings).count()
        template_count = db.query(models.BookletTemplateConfig).count()
        enrollment_count = db.query(models.Enrollment).count()
        batch_count = db.query(models.MasterBatch).count()
        booklet_count = db.query(models.Booklet).count()
        period_count = db.query(models.PeriodConfiguration).count()

        print("\n" + "=" * 62)
        print(" [SUCCESS] FULL DATABASE & WORKSPACE RESET COMPLETE")
        print("=" * 62)
        print(" * Database Schema:    bookletlog (Fresh UTF8MB4)")
        print(" * Admin Account:      admin / admin123  (Role: Admin, Dept: All)")
        print(" * System Settings:    Active Period = UNSET (Day 0 Initialized)")
        print(" * Template Slots:     6 slots created (All [!] Not Set)")
        print(" * Table Record Counts:")
        print(f"     - users:                    {user_count} (Single Admin)")
        print(f"     - system_settings:          {settings_count} (UNSET)")
        print(f"     - booklet_template_configs: {template_count} (3 CS, 3 IT)")
        print(f"     - enrollments:              {enrollment_count}")
        print(f"     - master_batches:           {batch_count}")
        print(f"     - booklets:                 {booklet_count}")
        print(f"     - period_configurations:    {period_count}")
        print(f" * Workspace Files:    {removed_files} test file(s) swept")
        print("=" * 62)
    finally:
        db.close()

# Alias for backward compatibility
mode_cold_start = perform_full_clean_reset

def mode_pure_zero(db_params):
    """Mode 2: Literal Zero-Data (All 7 tables generated but completely empty)."""
    drop_and_create_database(db_params)
    create_schema_tables()
    print("\n========================================================")
    print(" [SUCCESS] Pure Zero-Data Database Ready!")
    print(" • All tables created with 0 rows.")
    print(" • Note: You will need to register an admin via the app or SQL.")
    print("========================================================")

def mode_demo_ready(db_params):
    """
    Mode 3: Demo-Ready Seed (Presentation & Thesis Defense Preset)
    - Sets up accounts for Admin, CS Staff, and IT Staff
    - Configures active examination period
    - Adds sample class sections for Computer Science and Information Technology
    - Calibrates and enables booklet templates for both departments
    """
    drop_and_create_database(db_params)
    create_schema_tables()

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        print("\nSeeding demo-ready presentation dataset...")
        
        # 1. Accounts
        users = [
            models.User(username="admin", password=hash_pw("admin123"), role="Admin", department="All"),
            models.User(username="cs_staff", password=hash_pw("staff123"), role="Staff", department="CS"),
            models.User(username="it_staff", password=hash_pw("staff123"), role="Staff", department="IT"),
        ]
        db.add_all(users)

        # 2. Period Configuration
        period_name = "1st Sem - Midterm Exam | AY 2026-2027"
        period = models.PeriodConfiguration(
            academic_period=period_name,
            semester_prefix="1ST26",
            ream_price=250.0,
            sheets_per_booklet=6,
            yield_per_ream=83,
            booklet_price=2.50,
            department_ream_budget=100.0,
            budget_is_manual=False,
            reorder_buffer_percent=15.0,
            configured_by="Administrator"
        )
        db.add(period)

        # 3. System Settings
        settings = models.SystemSettings(
            active_period=period_name,
            current_semester_name="1ST26",
            ream_price=250.0,
            booklet_price=10.0,
            sheets_per_booklet=6,
            department_ream_budget=100.0,
            reorder_buffer_percent=15.0,
            data_version=1
        )
        db.add(settings)

        # 4. Sample Class Enrollments (CS and IT)
        enrollments = [
            models.Enrollment(teacher_name="Prof. Santos", section="BSCS 3-1", subject="COSC 101 - Algorithms", student_headcount=45, amount_due=450.0, amount_paid=0.0, payment_status="Unpaid", department="CS"),
            models.Enrollment(teacher_name="Dr. Reyes", section="BSCS 4-1", subject="COSC 110 - Thesis Writing", student_headcount=38, amount_due=380.0, amount_paid=380.0, payment_status="Fully Paid", department="CS"),
            models.Enrollment(teacher_name="Prof. Dela Cruz", section="BSIT 2-2", subject="ITEC 50 - Web Systems", student_headcount=50, amount_due=500.0, amount_paid=250.0, payment_status="Partially Paid", department="IT"),
            models.Enrollment(teacher_name="Engr. Mendoza", section="BSIT 3-1", subject="ITEC 65 - Networking", student_headcount=42, amount_due=420.0, amount_paid=0.0, payment_status="Unpaid", department="IT"),
        ]
        db.add_all(enrollments)

        # 5. Booklet Template Configurations: 3 slots per department
        os.makedirs("static/templates", exist_ok=True)
        if os.path.exists("base_template.pdf"):
            import shutil
            # Copy base template for active slots
            shutil.copy("base_template.pdf", "static/templates/active_template_CS_slot1.pdf")
            shutil.copy("base_template.pdf", "static/templates/active_template_CS_slot2.pdf")
            shutil.copy("base_template.pdf", "static/templates/active_template_IT_slot1.pdf")
            # Legacy aliases for backward compatibility
            shutil.copy("base_template.pdf", "static/templates/active_template_CS.pdf")
            shutil.copy("base_template.pdf", "static/templates/active_template_IT.pdf")
            try:
                from app.utils import render_template_preview_image
                render_template_preview_image(pdf_path="static/templates/active_template_CS_slot1.pdf", output_png_path="static/templates/active_template_CS_slot1_preview.png", department="CS", slot=1)
                render_template_preview_image(pdf_path="static/templates/active_template_CS_slot2.pdf", output_png_path="static/templates/active_template_CS_slot2_preview.png", department="CS", slot=2)
                render_template_preview_image(pdf_path="static/templates/active_template_IT_slot1.pdf", output_png_path="static/templates/active_template_IT_slot1_preview.png", department="IT", slot=1)
                # Legacy preview fallbacks
                render_template_preview_image(pdf_path="static/templates/active_template_CS.pdf", output_png_path="static/templates/active_template_CS_preview.png", department="CS")
                render_template_preview_image(pdf_path="static/templates/active_template_IT.pdf", output_png_path="static/templates/active_template_IT_preview.png", department="IT")
            except Exception as e:
                print(f"Warning generating demo previews: {e}")

        demo_templates = [
            # CS Slots
            models.BookletTemplateConfig(
                department="CS",
                slot_number=1,
                is_configured=True,
                template_name="Template 1",
                pdf_filename="active_template_CS_slot1.pdf",
                control_x=510.0,
                control_y=415.0,
                control_font_size=18,
                control_enabled=True,
                srn_x=510.0,
                srn_y=380.0,
                srn_font_size=11,
                srn_enabled=True,
                qr_x=685.0,
                qr_y=365.0,
                qr_size=65.0,
                qr_enabled=True
            ),
            models.BookletTemplateConfig(
                department="CS",
                slot_number=2,
                is_configured=True,
                template_name="Template 2",
                pdf_filename="active_template_CS_slot2.pdf",
                control_x=530.0,
                control_y=430.0,
                control_font_size=18,
                control_enabled=True,
                srn_x=530.0,
                srn_y=395.0,
                srn_font_size=11,
                srn_enabled=True,
                qr_x=700.0,
                qr_y=380.0,
                qr_size=65.0,
                qr_enabled=True
            ),
            models.BookletTemplateConfig(
                department="CS",
                slot_number=3,
                is_configured=False,
                template_name="Template 3",
                pdf_filename=None,
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
            ),
            # IT Slots
            models.BookletTemplateConfig(
                department="IT",
                slot_number=1,
                is_configured=True,
                template_name="Template 1",
                pdf_filename="active_template_IT_slot1.pdf",
                control_x=490.0,
                control_y=395.0,
                control_font_size=16,
                control_enabled=True,
                srn_x=490.0,
                srn_y=360.0,
                srn_font_size=10,
                srn_enabled=True,
                qr_x=670.0,
                qr_y=350.0,
                qr_size=60.0,
                qr_enabled=True
            ),
            models.BookletTemplateConfig(
                department="IT",
                slot_number=2,
                is_configured=False,
                template_name="Template 2",
                pdf_filename=None,
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
            ),
            models.BookletTemplateConfig(
                department="IT",
                slot_number=3,
                is_configured=False,
                template_name="Template 3",
                pdf_filename=None,
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
        ]
        db.add_all(demo_templates)
        db.commit()
        print("\n========================================================")
        print(" [SUCCESS] Demo-Ready Database Seeded!")
        print(" • Logins:")
        print("   - Admin:      admin / admin123")
        print("   - CS Staff:   cs_staff / staff123")
        print("   - IT Staff:   it_staff / staff123")
        print(" • Active Period: 1st Sem - Midterm Exam | AY 2026-2027")
        print(" • 4 Sample Class Enrollments (BSCS 3-1, BSCS 4-1, BSIT 2-2, BSIT 3-1)")
        print(" • Dual Booklet Templates: CS & IT Calibrated and Ready")
        print("========================================================")
    finally:
        db.close()

def mode_backup(db_params):
    """Exports current database rows to backups/db_backup_<timestamp>.json"""
    os.makedirs("backups", exist_ok=True)
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"backups/db_backup_{timestamp}.json"
        
        backup_data = {
            "timestamp": timestamp,
            "system_settings": [s.__dict__ for s in db.query(models.SystemSettings).all()],
            "period_configurations": [p.__dict__ for p in db.query(models.PeriodConfiguration).all()],
            "users": [u.__dict__ for u in db.query(models.User).all()],
            "enrollments": [e.__dict__ for e in db.query(models.Enrollment).all()],
            "booklet_template_configs": [t.__dict__ for t in db.query(models.BookletTemplateConfig).all()],
            "master_batches": [b.__dict__ for b in db.query(models.MasterBatch).all()],
            "booklets": [b.__dict__ for b in db.query(models.Booklet).all()]
        }

        # Remove SQLAlchemy internal state keys
        for table, rows in backup_data.items():
            if isinstance(rows, list):
                for row in rows:
                    row.pop("_sa_instance_state", None)
                    for k, v in list(row.items()):
                        if isinstance(v, (datetime.datetime, datetime.date)):
                            row[k] = v.isoformat()

        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2)

        print(f"\n[SUCCESS] Backup saved successfully to: {backup_file}")
    finally:
        db.close()

def main_menu():
    db_params = parse_db_url(SQLALCHEMY_DATABASE_URL)
    
    print("\n" + "=" * 62)
    print("   BOOKLETLOG - DATABASE MANAGEMENT & SETUP UTILITY")
    print("=" * 62)
    print(f" Target Server:  {db_params['host']}:{db_params['port']}")
    print(f" Target DB:      {db_params['db']} (User: {db_params['user']})")
    print("-" * 62)
    print(" [1] Full Clean Reset (Single Admin, 0 Data, Unset Period, Clean Files)")
    print("     -> Best for testing first-time wizard & clean test runs")
    print(" [2] Pure Zero-Data (Empty Database, 0 Rows across all tables)")
    print(" [3] Demo-Ready Preset (Admin + Staff accounts, CS/IT classes)")
    print("     -> Best for thesis defense presentation & instant demo")
    print(" [4] Backup Database to JSON snapshot")
    print(" [5] Exit")
    print("=" * 62)

    choice = input("Enter choice [1-5]: ").strip()
    if choice == "1":
        confirm = input("[!] This will completely wipe all tables and files. Continue? (y/N): ").strip().lower()
        if confirm == 'y':
            perform_full_clean_reset(db_params)
        else:
            print("[CANCELLED] Reset aborted. No changes were made.")
    elif choice == "2":
        confirm = input("[!] This will wipe all tables to 0 rows. Continue? (y/N): ").strip().lower()
        if confirm == 'y':
            mode_pure_zero(db_params)
        else:
            print("[CANCELLED] Reset aborted.")
    elif choice == "3":
        confirm = input("[!] This will reset and inject presentation seed data. Continue? (y/N): ").strip().lower()
        if confirm == 'y':
            mode_demo_ready(db_params)
        else:
            print("[CANCELLED] Reset aborted.")
    elif choice == "4":
        mode_backup(db_params)
    elif choice == "5":
        print("Exiting.")
    else:
        print("Invalid choice.")

if __name__ == "__main__":
    params = parse_db_url(SQLALCHEMY_DATABASE_URL)
    raw_args = sys.argv[1:]
    args = [a.lower() for a in raw_args]
    has_yes = any(flag in args for flag in ["--yes", "-y", "/y", "yes"])

    if any(flag in args for flag in ["--full-reset", "--reset", "--cold-start", "reset", "cold"]):
        if not has_yes:
            confirm = input("[!] Are you sure you want to completely wipe all tables and reset to a clean Admin account? (y/N): ").strip().lower()
            if confirm != 'y':
                print("[ABORTED] Full reset cancelled. No changes were made.")
                sys.exit(0)
        perform_full_clean_reset(params)
    elif any(flag in args for flag in ["--zero", "--pure-zero", "zero"]):
        if not has_yes:
            confirm = input("[!] This will wipe all tables to 0 rows. Continue? (y/N): ").strip().lower()
            if confirm != 'y':
                print("[ABORTED] Reset cancelled. No changes were made.")
                sys.exit(0)
        mode_pure_zero(params)
    elif any(flag in args for flag in ["--demo", "demo"]):
        if not has_yes:
            confirm = input("[!] This will reset and inject presentation seed data. Continue? (y/N): ").strip().lower()
            if confirm != 'y':
                print("[ABORTED] Demo seed cancelled. No changes were made.")
                sys.exit(0)
        mode_demo_ready(params)
    elif any(flag in args for flag in ["--backup", "backup"]):
        mode_backup(params)
    elif len(args) > 0:
        print(f"Unknown argument(s): {' '.join(raw_args)}")
        print("Available flags: --full-reset [--yes], --zero, --demo, --backup")
    else:
        main_menu()
