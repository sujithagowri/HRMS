"""
Models mapped 1:1 onto the existing MySQL tables created by the original
Node/Express server. Table and column names match exactly so the same
database can be used without migrating data.
"""
from decimal import Decimal
from django.db import models
from django.utils import timezone

from .timeutil import local_now


class JobPost(models.Model):
    title = models.CharField(max_length=255)
    dept = models.CharField(max_length=255)
    location = models.CharField(max_length=255, default='', blank=True)
    type = models.CharField(max_length=100, default='Full-time')
    salary = models.CharField(max_length=255, default='', blank=True)
    applicants = models.IntegerField(default=0)
    color = models.CharField(max_length=40, default='blue')
    description = models.TextField(null=True, blank=True)
    openings = models.IntegerField(default=1)
    is_remote = models.BooleanField(default=False)
    status = models.CharField(max_length=40, default='Active')
    status_comment = models.TextField(default='', blank=True)
    priority = models.CharField(max_length=20, default='Normal')
    # Values for any admin-defined custom fields from the Job Form Builder,
    # keyed by field `key`. Lets new fields be added with no schema change.
    custom_fields = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'job_posts'
        ordering = ['id']


class JobFormTemplate(models.Model):
    """A reusable job-posting form definition produced by the Form Builder.
    `schema` is the ordered list of field descriptors; exactly one row is the
    active form used to render "Post a Job"."""
    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=False)
    schema = models.JSONField(default=list, blank=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'job_form_templates'
        ordering = ['name']


class MasterDataSet(models.Model):
    """Global, reusable dropdown option lists (departments, locations, job
    types, …) referenced by Form Builder select fields via `masterKey`, so the
    same options can be managed in one place and reused across forms."""
    key = models.CharField(max_length=80, unique=True)
    label = models.CharField(max_length=120)
    options = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'master_data_sets'
        ordering = ['label']


class InterviewLink(models.Model):
    name = models.CharField(max_length=255)
    initials = models.CharField(max_length=10)
    role = models.CharField(max_length=255)
    email = models.CharField(max_length=255)
    phone = models.CharField(max_length=40, default='', blank=True)
    score = models.IntegerField(default=0)
    status = models.CharField(max_length=40, default='Pending')
    interview_date = models.CharField(max_length=60, null=True, blank=True)
    interview_time = models.CharField(max_length=30, null=True, blank=True)
    platform = models.CharField(max_length=100, null=True, blank=True)
    link = models.TextField(null=True, blank=True)
    outcome = models.CharField(max_length=40, null=True, blank=True)
    email_sent = models.BooleanField(default=False)
    interview_type = models.CharField(max_length=100, default='Technical')
    # Free text typed on the scheduling form — who will CONDUCT the interview
    # ("HR Team", "Eva AI", a panel name). It is a label for the invitation
    # email, not an identity, so it can never answer "whose numbers are these?".
    interviewer = models.CharField(max_length=255, default='', blank=True)
    # Who SCHEDULED it: the signed-in user, resolved server-side from the
    # X-User-Email header rather than the request body, so it cannot be spoofed
    # by a client and cannot disagree with the session that made the call. This
    # is what the KPI dashboard attributes an interview to. Indexed because
    # every per-person KPI query filters or groups on it.
    created_by_email = models.CharField(max_length=255, default='', blank=True, db_index=True)
    created_by_name = models.CharField(max_length=255, default='', blank=True)
    duration = models.CharField(max_length=50, default='45 min')
    notes = models.TextField(null=True, blank=True)
    # Who last edited the candidate note (any user may edit) + when — shown in
    # the note editor as "last modified by".
    notes_updated_by = models.CharField(max_length=255, default='', blank=True)
    notes_updated_by_email = models.CharField(max_length=255, default='', blank=True)
    notes_updated_at = models.DateTimeField(null=True, blank=True)
    # Stored as a JSON string (matches the original server's JSON.stringify).
    interview_questions = models.TextField(null=True, blank=True)
    # Per-round question counts and coding difficulties chosen when the
    # interview is scheduled. The AI interviewer reads these back to decide how
    # many questions of each type to ask, so they must round-trip through the
    # API. They exist in the legacy schema as NOT NULL columns.
    tech_question_count = models.IntegerField(default=3)
    hr_question_count = models.IntegerField(default=3)
    final_question_count = models.IntegerField(default=3)
    coding_difficulty = models.JSONField(null=True, blank=True)
    followup_sent = models.BooleanField(default=False)
    # Who sent the outcome email, resolved server-side from the caller's
    # identity. The mail goes out over a shared SMTP mailbox, so without this
    # the row records that a candidate was told the outcome but not by whom.
    followup_sent_by_email = models.CharField(max_length=255, default='', blank=True)
    followup_sent_by_name = models.CharField(max_length=255, default='', blank=True)
    followup_sent_at = models.DateTimeField(null=True, blank=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    # Separate access tokens for candidate and recruiter (with configurable expiry)
    candidate_token = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    recruiter_token = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    link_expires_at = models.DateTimeField(null=True, blank=True)
    # Set the first time the candidate finishes the session. Once set, the
    # candidate link is single-use: re-opening it is refused.
    completed_at = models.DateTimeField(null=True, blank=True)
    # Resume and JD text stored for AI-enhanced question generation
    resume_text = models.TextField(null=True, blank=True)
    jd_text = models.TextField(null=True, blank=True)
    # Stamped on the business clock, not the host's. auto_now_add uses
    # datetime.now(), which is the machine's local time — and the KPI dashboard
    # buckets these rows into days and custom windows using local_today(),
    # which is settings.TIME_ZONE. On a host whose clock differs from it (a
    # developer on IST against TIME_ZONE=UTC) the two disagree for hours every
    # night, and an interview scheduled in that gap was stamped "tomorrow"
    # while the Today filter still asked for "today" — so it simply did not
    # appear in its own report. See timeutil.local_now.
    created_at = models.DateTimeField(default=local_now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'interview_links'
        ordering = ['id']


class ResumeScore(models.Model):
    name = models.CharField(max_length=255)
    initials = models.CharField(max_length=10)
    role = models.CharField(max_length=255)
    score = models.IntegerField(default=0)
    technical = models.IntegerField(default=0)
    experience = models.IntegerField(default=0)
    domain = models.IntegerField(default=0)
    gap = models.TextField(null=True, blank=True)
    skills = models.JSONField(null=True, blank=True)
    missing = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=100, default='Upload')
    formatted = models.BooleanField(default=False)
    uploaded = models.BooleanField(default=True)
    file_name = models.CharField(max_length=255, null=True, blank=True)
    # The original uploaded resume, stored base64-in-row (house style — no file
    # storage backend is configured). resume_text is the extracted plain text;
    # file_data/file_mime keep the actual PDF/DOCX so it can be viewed as sent.
    file_mime = models.CharField(max_length=100, null=True, blank=True)
    file_data = models.TextField(null=True, blank=True)
    resume_text = models.TextField(null=True, blank=True)
    jd_text = models.TextField(null=True, blank=True)
    # Fingerprint of the resume content (normalised text, else the file bytes),
    # used to dedupe: re-uploading/re-scoring the same resume updates its one row
    # instead of creating another. Empty when there is nothing to fingerprint.
    content_hash = models.CharField(max_length=64, default='', blank=True, db_index=True)
    ai_summary = models.TextField(null=True, blank=True)
    ai_strengths = models.JSONField(null=True, blank=True)
    ai_gaps = models.JSONField(null=True, blank=True)
    ai_evaluated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'resume_scores'
        ordering = ['id']


class SourcingSearch(models.Model):
    """A recruiter-initiated candidate search for a job requirement."""
    job = models.ForeignKey(
        JobPost, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='sourcing_searches',
    )
    job_title = models.CharField(max_length=255, default='', blank=True)
    job_description = models.TextField()
    requirements = models.JSONField(default=dict, blank=True)
    sources = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=30, default='pending')
    requested_by_email = models.CharField(max_length=255, default='', blank=True, db_index=True)
    result_count = models.PositiveIntegerField(default=0)
    error = models.TextField(default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'sourcing_searches'
        ordering = ['-created_at']


class SourcingResult(models.Model):
    """A normalized, explainable match returned by a sourcing connector."""
    search = models.ForeignKey(
        SourcingSearch, on_delete=models.CASCADE, related_name='results'
    )
    resume = models.ForeignKey(
        ResumeScore, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='sourcing_results',
    )
    source = models.CharField(max_length=100, default='internal')
    external_id = models.CharField(max_length=255, default='', blank=True)
    candidate_name = models.CharField(max_length=255)
    candidate_role = models.CharField(max_length=255, default='', blank=True)
    profile_url = models.TextField(default='', blank=True)
    normalized_profile = models.JSONField(default=dict, blank=True)
    match_score = models.PositiveSmallIntegerField(default=0)
    explanation = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=30, default='new')
    feedback = models.TextField(default='', blank=True)
    reviewed_by_email = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sourcing_results'
        ordering = ['-match_score', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['search', 'source', 'external_id'],
                name='unique_sourcing_result_per_source',
            ),
        ]


class InterviewRecording(models.Model):
    candidate_name = models.CharField(max_length=255)
    candidate_email = models.CharField(max_length=255, default='', blank=True)
    role = models.CharField(max_length=255, default='', blank=True)
    duration = models.IntegerField(default=0)
    verdict = models.CharField(max_length=20, default='HOLD')
    total_score = models.IntegerField(default=0)
    tech_score = models.IntegerField(default=0)
    comm_score = models.IntegerField(default=0)
    integrity_score = models.IntegerField(default=0)
    recording_data = models.TextField(null=True, blank=True)   # base64 video
    video_buffer = models.BinaryField(null=True, blank=True)   # raw LONGBLOB
    video_mime = models.CharField(max_length=100, null=True, blank=True)
    transcript = models.TextField(null=True, blank=True)
    responses = models.JSONField(null=True, blank=True)
    ai_evaluation = models.JSONField(null=True, blank=True)
    executive_summary = models.TextField(null=True, blank=True)
    round2_questions = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'interview_recordings'
        ordering = ['id']


class QuestionSet(models.Model):
    id = models.CharField(max_length=40, primary_key=True)
    questions = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'question_sets'


class CompanyDetail(models.Model):
    """Organization Profile & General System Settings (Settings -> General)."""
    company_name = models.CharField(max_length=255, default='Eversoft Technologies')
    legal_name = models.CharField(max_length=255, default='', blank=True)
    brand_name = models.CharField(max_length=255, default='Eversoft', blank=True)
    company_logo = models.TextField(default='', blank=True)  # base64 or URL
    website = models.CharField(max_length=255, default='https://eversoftit.com', blank=True)
    contact_email = models.CharField(max_length=255, default='contact@eversoftit.com', blank=True)
    phone = models.CharField(max_length=40, default='', blank=True)
    tax_id = models.CharField(max_length=100, default='', blank=True)  # GST / EIN / PAN
    industry = models.CharField(max_length=120, default='Information Technology', blank=True)
    
    # Registered Address
    address_line1 = models.CharField(max_length=255, default='', blank=True)
    address_line2 = models.CharField(max_length=255, default='', blank=True)
    city = models.CharField(max_length=100, default='', blank=True)
    state = models.CharField(max_length=100, default='', blank=True)
    country = models.CharField(max_length=100, default='India', blank=True)
    pincode = models.CharField(max_length=20, default='', blank=True)
    
    # Localization Defaults
    timezone = models.CharField(max_length=100, default='Asia/Kolkata', blank=True)
    currency = models.CharField(max_length=10, default='INR', blank=True)
    date_format = models.CharField(max_length=30, default='DD/MM/YYYY', blank=True)
    work_week = models.JSONField(default=list, blank=True)  # ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    
    # Employee ID Auto-Generation Configuration
    emp_id_prefix = models.CharField(max_length=20, default='EV-', blank=True)
    emp_id_min_digits = models.IntegerField(default=4)  # e.g., 4 -> '0001'
    emp_id_start_number = models.IntegerField(default=1)
    emp_id_suffix = models.CharField(max_length=20, default='', blank=True)
    
    updated_by = models.CharField(max_length=255, default='', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'company_details'
        ordering = ['id']


class AppUser(models.Model):
    """Login accounts created in Settings -> User Access (and via Sign-up).
    Maps onto the existing `app_users` table so every login persists in MySQL
    instead of only the browser's localStorage. `password` is plain text to
    match the admin UI's "SHOW" reveal button (see hrms_system_schema.sql)."""
    employee_id = models.CharField(max_length=64, default='', blank=True, db_index=True)
    full_name = models.CharField(max_length=255)
    email = models.CharField(max_length=255, unique=True)
    password = models.CharField(max_length=255, default='', blank=True)
    initials = models.CharField(max_length=10, default='', blank=True)
    role = models.CharField(max_length=40, default='admin')      # admin | recruitment | hr (legacy string)
    status = models.CharField(max_length=20, default='active')   # active | disabled
    # RBAC links (see the roles / companies tables). Nullable + additive so the
    # legacy ``role`` string above keeps driving the current login role-gate.
    role_ref = models.ForeignKey(
        'Role', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='users', db_column='role_id',
    )
    company = models.ForeignKey(
        'Company', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='users', db_column='company_id',
    )
    # Social / Google login fields
    auth_provider = models.CharField(max_length=20, default='email')  # email | google
    google_id = models.CharField(max_length=128, null=True, blank=True, unique=True)
    profile_pic = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'app_users'
        ordering = ['id']


class UserProfile(models.Model):
    employee_id = models.CharField(max_length=64, default='', blank=True, db_index=True)
    email = models.CharField(max_length=255, primary_key=True)
    first_name = models.CharField(max_length=120, default='', blank=True)
    last_name = models.CharField(max_length=120, default='', blank=True)
    phone = models.CharField(max_length=40, default='', blank=True)
    alt_email = models.CharField(max_length=255, default='', blank=True)
    blood_group = models.CharField(max_length=10, default='', blank=True)
    department = models.CharField(max_length=120, default='', blank=True)
    designation = models.CharField(max_length=120, default='', blank=True)
    manager = models.CharField(max_length=255, default='', blank=True)
    level = models.CharField(max_length=20, default='L4', blank=True)
    employment_type = models.CharField(max_length=40, default='Full-time', blank=True)
    location = models.CharField(max_length=255, default='', blank=True)
    annual_ctc = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    profile_pic = models.TextField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_profiles'


class UserEmailConfig(models.Model):
    user_email = models.CharField(max_length=255, primary_key=True)
    smtp_host = models.CharField(max_length=255, default='', blank=True)
    smtp_port = models.CharField(max_length=10, default='', blank=True)
    smtp_user = models.CharField(max_length=255, default='', blank=True)
    smtp_password = models.CharField(max_length=255, default='', blank=True)
    smtp_secure = models.BooleanField(default=False)
    from_name = models.CharField(max_length=255, default='', blank=True)
    from_email = models.CharField(max_length=255, default='', blank=True)
    social = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_email_config'


class UserDocument(models.Model):
    user_email = models.CharField(max_length=255)
    doc_type = models.CharField(max_length=60)
    file_name = models.CharField(max_length=255, default='', blank=True)
    file_mime = models.CharField(max_length=100, default='', blank=True)
    file_data = models.TextField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_documents'
        unique_together = (('user_email', 'doc_type'),)


class LoginOtp(models.Model):
    """One-time login codes for two-step (OTP) authentication. The 6-digit code
    is stored salted+hashed, never in plain text."""
    email = models.CharField(max_length=255, db_index=True)
    code_hash = models.CharField(max_length=128)
    salt = models.CharField(max_length=32)
    attempts = models.IntegerField(default=0)
    consumed = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'login_otps'
        ordering = ['-id']


class Notification(models.Model):
    """Lightweight in-app notifications for employees and admins."""
    recipient = models.CharField(max_length=255, db_index=True)
    title = models.CharField(max_length=255, default='', blank=True)
    message = models.TextField(default='', blank=True)
    notification_type = models.CharField(max_length=20, default='info')
    is_read = models.BooleanField(default=False)
    link = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']


class PasswordReset(models.Model):
    """Single-use, time-limited tokens for the Forgot Password flow."""
    email = models.CharField(max_length=255, db_index=True)
    token = models.CharField(max_length=128, unique=True)
    used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'password_resets'
        ordering = ['-id']


class LiveSession(models.Model):
    """REST-based WebRTC signaling for recruiter live-viewing of an F2F
    interview. The candidate (publisher) posts an SDP offer + ICE candidates;
    the recruiter (viewer) posts an SDP answer + ICE candidates. Both sides
    poll this row to complete the peer-to-peer connection."""
    session_id = models.CharField(max_length=64, unique=True)
    candidate_name = models.CharField(max_length=255, default='', blank=True)
    role = models.CharField(max_length=255, default='', blank=True)
    interview_id = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, default='waiting')  # waiting|live|ended
    offer = models.TextField(null=True, blank=True)               # candidate SDP offer (JSON)
    answer = models.TextField(null=True, blank=True)              # recruiter SDP answer (JSON)
    candidate_ice = models.JSONField(default=list, blank=True)    # ICE from candidate
    recruiter_ice = models.JSONField(default=list, blank=True)    # ICE from recruiter
    transcript = models.TextField(null=True, blank=True)          # live transcript snapshot
    current_question = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'live_sessions'
        ordering = ['-id']


# ===========================================================================
# Employees module
# Attendance / Check-In-Out · Leave · Tasks · Work Submissions.
# Employees are identified by their login email (app_users / user_profiles);
# there is no separate roster table.
# ===========================================================================
class EmployeeAttendance(models.Model):
    """One row per employee per day. Check-in stamps ``check_in`` (and the
    device it came from); check-out stamps ``check_out`` and computes
    ``worked_minutes``. ``status`` is derived on check-in (present/late).

    Extended columns (attendance_migrations.sql):
      shift_id, is_wfh, break_minutes, overtime_minutes, late_minutes,
      early_exit_minutes, location_lat, location_lng, geo_verified
    """
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    date = models.DateField(db_index=True)
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    device = models.CharField(max_length=20, default='', blank=True)   # mobile | desktop
    status = models.CharField(max_length=20, default='present')        # present | late | absent | half-day
    # Live presence chosen from the STATUS picker (Available / Away / Busy /
    # Do not disturb / ...). Empty = fall back to location-derived team status.
    presence = models.CharField(max_length=40, default='', blank=True)
    presence_at = models.DateTimeField(null=True, blank=True)
    worked_minutes = models.IntegerField(default=0)
    note = models.CharField(max_length=255, default='', blank=True)
    # --- Advanced attendance fields ---
    shift_id = models.IntegerField(null=True, blank=True)
    is_wfh = models.BooleanField(default=False)
    break_minutes = models.IntegerField(default=0)
    overtime_minutes = models.IntegerField(default=0)
    late_minutes = models.IntegerField(default=0)
    early_exit_minutes = models.IntegerField(default=0)
    location_lat = models.FloatField(null=True, blank=True)
    location_lng = models.FloatField(null=True, blank=True)
    geo_verified = models.BooleanField(default=False)
    # --- Out-of-geofence check-in review ---------------------------------
    # An employee who is not working from home and checks in outside every
    # active fence is let through, but must give a reason and the check-in is
    # held for HR/admin review. Blank status = nothing to review (WFH, inside a
    # fence, or no fences configured).
    location_reason = models.TextField(null=True, blank=True)
    location_status = models.CharField(max_length=20, default='', blank=True)  # ''|Pending|Approved|Rejected
    location_reviewer = models.CharField(max_length=255, default='', blank=True)
    location_reviewed_at = models.DateTimeField(null=True, blank=True)
    # Set once the "you have passed N hours today" mail goes out, so the
    # reminder is sent at most once per employee per day.
    overtime_alert_sent_at = models.DateTimeField(null=True, blank=True)
    # Same idea for the "no check-in recorded" notice: the late sweep runs every
    # few minutes, and nobody should be mailed twice for the same day.
    late_alert_sent_at = models.DateTimeField(null=True, blank=True)
    # Set when the browser closed the session because the employee left the
    # geofence, so the day is distinguishable from a manual check-out.
    auto_checkout_at = models.DateTimeField(null=True, blank=True)
    is_auto_checked_out = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_attendance'
        ordering = ['-date', '-id']
        unique_together = (('email', 'date'),)


class LeaveRequest(models.Model):
    """A leave application with an approval workflow."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    type = models.CharField(max_length=60, default='Casual Leave')
    from_date = models.DateField()
    to_date = models.DateField()
    days = models.IntegerField(default=1)
    reason = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default='Pending')   # Pending | Approved | Rejected
    approver = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'leave_requests'
        ordering = ['-id']


class EmployeeTask(models.Model):
    """A task on the Task Tracker board."""
    title = models.CharField(max_length=255)
    assignee = models.CharField(max_length=255, default='', blank=True)
    assignee_email = models.CharField(max_length=255, default='', blank=True)
    due = models.CharField(max_length=60, default='', blank=True)   # date string as the UI sends it
    priority = models.CharField(max_length=20, default='medium')    # low | medium | high
    stage = models.CharField(max_length=20, default='todo')         # todo | inprogress | done
    description = models.TextField(null=True, blank=True)
    task_code = models.CharField(max_length=40, default='', blank=True)   # e.g. TASK-1788507958987
    team_lead = models.CharField(max_length=255, default='', blank=True)
    department = models.CharField(max_length=120, default='', blank=True)
    estimated_hours = models.FloatField(null=True, blank=True)
    progress = models.IntegerField(default=0)   # 0-100
    # Attachments are stored as full `data:<mime>;base64,<data>` URLs (house style — no file
    # storage backend configured; see UserDocument), capped client- and server-side at 50 KB.
    attachment_file_name = models.CharField(max_length=255, default='', blank=True)
    attachment_file_mime = models.CharField(max_length=100, default='', blank=True)
    attachment_file_data = models.TextField(default='', blank=True)
    image_file_name = models.CharField(max_length=255, default='', blank=True)
    image_file_mime = models.CharField(max_length=100, default='', blank=True)
    image_file_data = models.TextField(default='', blank=True)
    # Set when the assignee rejects the task from the To Do column; a non-empty
    # value is the "rejected" signal (no separate stage — the task stays put so
    # the reviewer can see and act on the reason).
    reject_reason = models.TextField(default='', blank=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_tasks'
        ordering = ['-id']


class WorkSubmission(models.Model):
    """A work item / deliverable submitted by an employee for review."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    title = models.CharField(max_length=255)
    type = models.CharField(max_length=60, default='Document', blank=True)
    date = models.DateField(null=True, blank=True)
    summary = models.TextField(null=True, blank=True)
    link = models.CharField(max_length=500, default='', blank=True)
    file_name = models.CharField(max_length=255, default='', blank=True)
    # The attachment itself, stored base64-in-row (house style — there is no
    # file storage backend configured; see CandidateDocument / UserDocument).
    # Without this the file never left the submitter's browser, so a reviewer
    # could see the name but never open what they were being asked to review.
    file_mime = models.CharField(max_length=100, default='', blank=True)
    file_size = models.IntegerField(default=0)           # decoded bytes
    file_data = models.TextField(null=True, blank=True)  # base64 data URL
    status = models.CharField(max_length=20, default='Pending')   # Pending | In Review | Approved | Rejected
    reviewer = models.CharField(max_length=255, default='', blank=True)
    # Why it was sent back, and anything the reviewer marked up. Kept here for
    # the same reason as the attachment above: written to the browser alone,
    # the reason existed only for the reviewer who typed it, and the employee
    # being asked to make the changes could never read them.
    reviewer_note = models.TextField(null=True, blank=True)
    reviewer_note_by = models.CharField(max_length=255, default='', blank=True)
    reviewer_note_at = models.DateTimeField(null=True, blank=True)
    review_file_name = models.CharField(max_length=255, default='', blank=True)
    review_file_mime = models.CharField(max_length=100, default='', blank=True)
    review_file_size = models.IntegerField(default=0)
    review_file_data = models.TextField(null=True, blank=True)
    ai_score = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'work_submissions'
        ordering = ['-id']


BENCH_SUBMISSION_STATUSES = [
    'Submitted', 'Interview Scheduled', 'Offered', 'Placed', 'Rejected', 'On Hold',
]


class BenchSubmission(models.Model):
    """A bench-sales submission: a consultant's profile submitted to a client
    through a vendor, tracked through to interview/placement.
    """
    name = models.CharField(max_length=255)
    client_name = models.CharField(max_length=255, default='', blank=True)
    vendor_prime_vendor = models.CharField(max_length=255, default='', blank=True)
    vendor_person_name = models.CharField(max_length=255, default='', blank=True)
    vendor_email = models.CharField(max_length=255, default='', blank=True)
    vendor_contact = models.CharField(max_length=60, default='', blank=True)
    implementation_partner = models.CharField(max_length=255, default='', blank=True)
    rate = models.CharField(max_length=60, default='', blank=True)
    role_responsibilities = models.TextField(default='', blank=True)
    status = models.CharField(max_length=40, default='Submitted')
    follow_up_vendor = models.DateField(null=True, blank=True)
    interview_schedule = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bench_submissions'
        ordering = ['-id']


class BenchSalesProfile(models.Model):
    """A bench-sales consultant profile — the marketable roster of consultants
    available to submit to clients/vendors (distinct from BenchSubmission,
    which tracks one specific submission of a consultant to a client).

    Resume / DL / State ID / I-94 are stored base64-in-row, house style — see
    UserDocument — since no file storage backend is configured.
    """
    name = models.CharField(max_length=255)
    tech_stack = models.CharField(max_length=500, default='', blank=True)
    experience = models.CharField(max_length=60, default='', blank=True)
    email = models.CharField(max_length=255, default='', blank=True)
    contact_no = models.CharField(max_length=60, default='', blank=True)
    work_authorization = models.CharField(max_length=40, default='', blank=True)

    resume_file_name = models.CharField(max_length=255, default='', blank=True)
    resume_file_mime = models.CharField(max_length=100, default='', blank=True)
    resume_file_data = models.TextField(default='', blank=True)  # base64

    dl_state_id_file_name = models.CharField(max_length=255, default='', blank=True)
    dl_state_id_file_mime = models.CharField(max_length=100, default='', blank=True)
    dl_state_id_file_data = models.TextField(default='', blank=True)  # base64

    i94_file_name = models.CharField(max_length=255, default='', blank=True)
    i94_file_mime = models.CharField(max_length=100, default='', blank=True)
    i94_file_data = models.TextField(default='', blank=True)  # base64

    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bench_sales_profiles'
        ordering = ['-id']


class BenchSalesCertificate(models.Model):
    """One uploaded certificate PDF for a Bench Sales profile — up to
    MAX_CERTIFICATES_PER_PROFILE (see views.py) per profile. Same
    base64-in-row storage as BenchSalesProfile's resume/DL/I-94 fields.
    """
    profile = models.ForeignKey(BenchSalesProfile, related_name='certificate_files', on_delete=models.CASCADE)
    file_name = models.CharField(max_length=255, default='', blank=True)
    file_mime = models.CharField(max_length=100, default='', blank=True)
    file_data = models.TextField(default='', blank=True)  # base64
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'bench_sales_certificates'
        ordering = ['id']


class AttendanceEvent(models.Model):
    """A single timeline event on an employee's day — check-in / check-out,
    break start / end, or a work-mode switch (office <-> remote). These rows
    drive the "Today's Activity Log" panel (per employee) and the
    "Team Status Now" panel (latest event per employee → live status).

    Extended columns (attendance_migrations.sql): latitude, longitude, geo_fence_id
    """
    # check-in | check-out | break-start | break-end | remote-switch | office-switch
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    date = models.DateField(db_index=True)
    event = models.CharField(max_length=30)
    location = models.CharField(max_length=120, default='', blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geo_fence_id = models.IntegerField(null=True, blank=True)
    at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'attendance_events'
        ordering = ['at', 'id']


# ===========================================================================
# Advanced Attendance Management — Models
# ===========================================================================

class Shift(models.Model):
    """A named work schedule with start/end times, break, grace and OT config."""
    name = models.CharField(max_length=100)
    start_time = models.TimeField(default='09:00')
    end_time = models.TimeField(default='18:00')
    break_minutes = models.IntegerField(default=60)
    grace_minutes = models.IntegerField(default=15)
    is_flexible = models.BooleanField(default=False)
    flex_hours_per_day = models.FloatField(default=8.0)
    overtime_after_minutes = models.IntegerField(default=540)
    is_night_shift = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'attendance_shifts'
        ordering = ['name']

    def total_work_minutes(self):
        """Net work minutes = shift duration - standard break."""
        from datetime import datetime, date
        start = datetime.combine(date.today(), self.start_time)
        end = datetime.combine(date.today(), self.end_time)
        if self.is_night_shift and end <= start:
            from datetime import timedelta
            end += timedelta(days=1)
        return int((end - start).total_seconds() / 60) - self.break_minutes


class ShiftAssignment(models.Model):
    """Maps an employee to a shift for a date range."""
    email = models.CharField(max_length=255, db_index=True)
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='assignments')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)  # NULL = open-ended
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'shift_assignments'
        ordering = ['-effective_from']


class AttendanceCorrection(models.Model):
    """Employee-submitted request to correct a past attendance record."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    attendance_date = models.DateField(db_index=True)
    requested_check_in = models.DateTimeField(null=True, blank=True)
    requested_check_out = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default='Pending')  # Pending|Approved|Rejected
    reviewer = models.CharField(max_length=255, default='', blank=True)
    reviewer_note = models.TextField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Forgot-punch ticket extension: which punch was missed and the proof photo
    # (attendance sheet) the employee attaches. Legacy rows keep kind='' so the
    # 3-per-month limit only counts these tickets, not older corrections.
    kind = models.CharField(max_length=20, default='', blank=True)  # ''|forgot_checkin|forgot_checkout
    proof_image = models.TextField(null=True, blank=True)           # base64 data URL

    class Meta:
        db_table = 'attendance_corrections'
        ordering = ['-id']


class AttendanceLocationPunch(models.Model):
    """A periodic location sample captured while an employee is checked in.

    Stored roughly once an hour by the web app (see the attendance
    location-punch endpoint, which throttles to hourly and only records while
    the employee is checked in). Powers the location log and the map preview on
    the employee attendance-detail popup."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    date = models.DateField(db_index=True)
    captured_at = models.DateTimeField(db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    accuracy = models.FloatField(null=True, blank=True)
    label = models.CharField(max_length=255, default='', blank=True)   # reverse-geocoded place
    source = models.CharField(max_length=20, default='web', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'attendance_location_punches'
        ordering = ['captured_at']


class GeoFence(models.Model):
    """A GPS circle zone. Check-ins inside are marked geo_verified=True.

    Two kinds of fence live here, told apart by ``owner_email``:

      * blank  — a company location. Any employee checking in inside it is
        verified. This is what the table originally held.
      * set    — one employee's registered home, used to verify their WFH
        check-ins. It belongs to that person and to nobody else.

    Keeping them in one table means home fences inherit the distance maths, the
    GPS-accuracy allowance and the map rendering that already exist. It also
    means every query has to be explicit about which kind it wants: an office
    check-in matched against an unfiltered fence list would let anyone check in
    "at the office" from a colleague's living room. See _office_fences() and
    home_fence_for().

    A home fence is only a verification basis once ``status`` is Approved. The
    employee captures it themselves — they are the one standing in the right
    place — but until someone confirms it, a person could register wherever
    they happened to be and self-certify every future check-in.
    """
    PENDING, APPROVED, REJECTED = 'Pending', 'Approved', 'Rejected'

    name = models.CharField(max_length=100)
    latitude = models.FloatField()
    longitude = models.FloatField()
    radius_meters = models.IntegerField(default=200)
    is_active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Blank for a company location; an employee's address makes it their home.
    owner_email = models.CharField(max_length=255, default='', blank=True, db_index=True)
    # Only meaningful for home fences. Company locations are created by someone
    # who already holds the permission, so they need no second approval.
    status = models.CharField(max_length=20, default='', blank=True)
    reviewer = models.CharField(max_length=255, default='', blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # The accuracy of the fix the employee captured, so a reviewer can see
    # whether the pin was worth trusting before confirming it.
    captured_accuracy = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = 'attendance_geofences'
        ordering = ['name']


class WfhRequest(models.Model):
    """Employee request to work from home for a date range."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    from_date = models.DateField()
    to_date = models.DateField()
    days = models.IntegerField(default=1)
    reason = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default='Pending')  # Pending|Approved|Rejected
    approver = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wfh_requests'
        ordering = ['-id']


class WorkArrangement(models.Model):
    """Where an employee works, effective-dated.

    This is a property of the person, not a permission and not a per-day
    request. It used to be neither: the only lever was the ``attendance.remote``
    permission, which is binary (so hybrid could not be expressed at all), hangs
    off a *role* (so making one person remote meant inventing a role for them),
    and carries no dates.

    Dates are the reason this is a table of rows rather than a column on the
    employee. Arrangements change — someone moves onsite to hybrid, a remote
    hire relocates to a city with an office — and a column would overwrite the
    past, so recomputing or auditing an old month would judge it by today's
    rule. Rows are never edited in place: closing one and opening the next
    keeps the history intact. Same shape as EmployeeCompensation, for the same
    reason.

    ``remote_weekdays`` are anchor days (Python weekday numbers, Mon=0), used
    when a team has fixed in-office days. When it is empty the employee picks
    their own days and ``remote_days_per_week`` is the cap. Both only apply to
    'hybrid': 'remote' is unrestricted and 'onsite' has no remote entitlement.
    """
    ONSITE, HYBRID, REMOTE = 'onsite', 'hybrid', 'remote'

    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    arrangement = models.CharField(max_length=20, default=ONSITE)  # onsite|hybrid|remote
    # Anchor days as a comma-separated list of weekday numbers, e.g. "0,4" for
    # Monday and Friday. Empty means "no fixed days, use the weekly quota".
    remote_weekdays = models.CharField(max_length=32, default='', blank=True)
    remote_days_per_week = models.IntegerField(default=0)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)  # null = still in force
    notes = models.TextField(default='', blank=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'work_arrangements'
        ordering = ['-effective_from', '-id']
        indexes = [models.Index(fields=['email', 'effective_from'])]

    def weekday_set(self):
        """Anchor days as a set of ints. Tolerates blanks and stray values."""
        out = set()
        for part in (self.remote_weekdays or '').split(','):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 6:
                out.add(int(part))
        return out


# ===========================================================================
# Break Management
# ===========================================================================
class BreakPolicy(models.Model):
    """Company break policy configuration."""
    name = models.CharField(max_length=100, default='Default')
    max_break_minutes_per_day = models.IntegerField(default=60)
    min_break_minutes = models.IntegerField(default=15)
    max_break_minutes = models.IntegerField(default=60)
    is_paid = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'break_policies'
        ordering = ['name']


class Break(models.Model):
    """Employee break record during work hours."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    date = models.DateField(db_index=True)
    break_start = models.DateTimeField()
    break_end = models.DateTimeField(null=True, blank=True)
    break_type = models.CharField(max_length=20, default='meal')  # meal|rest|personal|medical
    reason = models.CharField(max_length=255, default='', blank=True)
    is_paid = models.BooleanField(default=False)
    break_minutes = models.IntegerField(default=0)
    status = models.CharField(max_length=20, default='active')  # active|completed|cancelled
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_breaks'
        ordering = ['-date', '-break_start']


# ===========================================================================
# Late Check-In Management
# ===========================================================================
class LateCheckInPolicy(models.Model):
    """Policy for late check-in thresholds and actions."""
    name = models.CharField(max_length=100, default='Default')
    late_threshold_minutes = models.IntegerField(default=5)
    escalation_count = models.IntegerField(default=3)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'late_checkin_policies'
        ordering = ['name']


class LateCheckInAlert(models.Model):
    """Alert for late check-ins."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    date = models.DateField(db_index=True)
    late_minutes = models.IntegerField(default=0)
    check_in_time = models.DateTimeField()
    shift_start_time = models.DateTimeField()
    reason = models.CharField(max_length=255, default='', blank=True)
    is_excused = models.BooleanField(default=False)
    excused_by = models.CharField(max_length=255, default='', blank=True)
    excused_at = models.DateTimeField(null=True, blank=True)
    escalated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'late_checkin_alerts'
        ordering = ['-date', '-id']


# ===========================================================================
# Overtime Management
# ===========================================================================
class OvertimePolicy(models.Model):
    """Configuration for overtime calculations and approval."""
    name = models.CharField(max_length=100, default='Default')
    overtime_threshold_minutes = models.IntegerField(default=540)  # 9 hours
    daily_max_overtime_minutes = models.IntegerField(default=180)  # 3 hours
    weekly_max_overtime_minutes = models.IntegerField(default=600)  # 10 hours
    is_active = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'overtime_policies'
        ordering = ['name']


class Overtime(models.Model):
    """Tracked overtime for an employee on a specific date."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    date = models.DateField(db_index=True)
    shift_hours = models.FloatField(default=8.0)
    worked_hours = models.FloatField(default=0.0)
    overtime_hours = models.FloatField(default=0.0)
    overtime_type = models.CharField(max_length=30, default='regular')  # regular|weekend|holiday
    status = models.CharField(max_length=20, default='calculated')  # calculated|pending_approval|approved|rejected
    approver = models.CharField(max_length=255, default='', blank=True)
    approval_note = models.TextField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'overtimes'
        ordering = ['-date', '-id']
        unique_together = (('email', 'date'),)


class OvertimeBalance(models.Model):
    """Tracks cumulative overtime balance per employee per period."""
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    period = models.CharField(max_length=20, db_index=True)  # YYYY-MM
    total_overtime_hours = models.FloatField(default=0.0)
    comp_off_hours = models.FloatField(default=0.0)  # Compensatory off balance
    cash_payout_hours = models.FloatField(default=0.0)  # Cash payout balance
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'overtime_balances'
        ordering = ['-period', 'email']
        unique_together = (('email', 'period'),)


class WFHPolicy(models.Model):
    """Company policy for work-from-home arrangements."""
    name = models.CharField(max_length=100, default='Default')
    max_wfh_days_per_week = models.IntegerField(default=2)
    max_wfh_days_per_month = models.IntegerField(default=10)
    requires_approval = models.BooleanField(default=True)
    min_advance_notice_days = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wfh_policies'
        ordering = ['name']


# ===========================================================================
# Role-Based Access Control (RBAC)
# ---------------------------------------------------------------------------
# Super Admin creates Roles + Permission Groups, groups own Permissions, and a
# Role is granted Permissions (directly or by assigning whole Groups). Every
# table below is indexed on the columns the API filters/joins on so the list,
# assign and permission-check queries stay O(indexed-lookup), never N+1.
# ===========================================================================
class Company(models.Model):
    """Tenant / org that owns users. Referenced by ``app_users.company_id``."""
    name = models.CharField(max_length=200, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'companies'
        ordering = ['name']


class Module(models.Model):
    """A top-level feature area for grouping menus/permissions
    (Employee, Attendance, Leave, Payroll, ...)."""
    name = models.CharField(max_length=100, unique=True)
    icon = models.CharField(max_length=60, default='', blank=True)
    order = models.IntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'modules'
        ordering = ['order', 'id']


class Role(models.Model):
    """A named set of permissions (Super Admin, HR Manager, Employee, ...)."""
    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, default='', blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        AppUser, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='roles_created', db_column='created_by',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'roles'
        ordering = ['name']


class PermissionGroup(models.Model):
    """A bundle of permissions under a module (e.g. "Employee Group")."""
    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, default='', blank=True)
    module = models.ForeignKey(
        Module, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='groups', db_column='module_id',
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'permission_groups'
        ordering = ['name']


class Permission(models.Model):
    """A single capability, addressed by a stable ``code`` (e.g. employee.view)."""
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, default='', blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    group = models.ForeignKey(
        PermissionGroup, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='permissions', db_column='group_id',
    )

    class Meta:
        db_table = 'permissions'
        ordering = ['group_id', 'id']


class RolePermission(models.Model):
    """Join row granting one permission to one role. The unique index doubles as
    the fast lookup path for permission checks and prevents duplicate grants."""
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='role_permissions')
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name='role_permissions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'role_permissions'
        ordering = ['id']
        unique_together = (('role', 'permission'),)


# ---------------------------------------------------------------------------
# Onboarding — work authorization & employee onboarding
#
# The lifecycle between "candidate accepted an offer" and "employee exists in
# the system". A candidate walks a fixed set of stages (see ONBOARDING_STAGES);
# every stage transition writes an OnboardingActivityLog row, which serves as
# both the audit trail and the UI timeline.
#
# Candidates are NOT stored in interview_links: that table is the *recruiting*
# record (tokens, scores, transcripts) and has a different lifecycle. A hired
# interviewee links through via OnboardingCandidate.interview (nullable FK).
# ---------------------------------------------------------------------------

# Stage keys, in workflow order. Kept as plain strings (house style: no
# TextChoices) but centralised because the API seeds one OnboardingStatus row
# per stage and the UI renders one badge per stage.
ONBOARDING_STAGES = [
    'candidate_created',
    'work_authorization',
    'documents',
    'hr_verification',
    'manager_approval',
    'it_assets',
    'payroll',
    'activated',
]


class OnboardingCandidate(models.Model):
    """A person being onboarded. Soft-deleted so the audit trail survives."""
    # Human-friendly identifier (e.g. CAN0001). Server-generated on create by
    # onboarding_views._next_candidate_code from the highest CAN-code ever
    # assigned across ALL candidates — including soft-deleted ones — plus one, so
    # a code is never reused: the sequence only climbs and a deletion leaves a
    # permanent gap (it does NOT restart at CAN0001). Display-only label, not
    # unique-constrained.
    candidate_code = models.CharField(max_length=32, default='', blank=True, db_index=True)
    first_name = models.CharField(max_length=120, default='', blank=True)
    last_name = models.CharField(max_length=120, default='', blank=True)
    email = models.CharField(max_length=255, db_index=True)
    phone = models.CharField(max_length=40, default='', blank=True)
    # Personal information (optional — collected during registration).
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=32, default='', blank=True)
    address = models.CharField(max_length=500, default='', blank=True)
    client = models.CharField(max_length=255, default='', blank=True)
    vendor = models.CharField(max_length=255, default='', blank=True)
    recruiter = models.CharField(max_length=255, default='', blank=True)
    job_title = models.CharField(max_length=255, default='', blank=True)
    department = models.CharField(max_length=255, default='', blank=True)
    # Reporting manager and the site/office the candidate will work from.
    manager = models.CharField(max_length=255, default='', blank=True)
    work_location = models.CharField(max_length=255, default='', blank=True)
    joining_date = models.DateField(null=True, blank=True)
    # Draft | Pending Verification | Pending Approval | Approved | Rejected | Onboarded
    status = models.CharField(max_length=32, default='Draft', db_index=True)
    # Link back to the recruiting record, when this candidate came from an interview.
    interview = models.ForeignKey(
        InterviewLink, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='onboarding_candidates', db_column='interview_id',
    )
    # Admin-defined extra fields (see OnboardingSetting 'candidate_fields').
    # Values keyed by the field's ``key``; the schema lives in config, not here,
    # so adding a field never needs a migration.
    custom_fields = models.JSONField(default=dict, blank=True)
    portal_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    portal_token_expires_at = models.DateTimeField(null=True, blank=True)
    requested_docs = models.JSONField(default=list, blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.CharField(max_length=255, default='', blank=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'onboarding_candidates'
        ordering = ['-created_at']


class WorkAuthorization(models.Model):
    """The candidate's right to work. One row per candidate.

    ``expiry_date`` is deliberately a real indexed column rather than living in
    WorkAuthorizationDetail.details: the "expiring soon" dashboard card and the
    expiry alerts query it on every load, and JSON lookups cannot be indexed
    portably across MySQL/SQLite.
    """
    candidate = models.OneToOneField(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='work_authorization', db_column='candidate_id',
    )
    # F1 | H1B | GC EAD | US Citizen | H4 EAD | Other
    auth_type = models.CharField(max_length=32, default='', blank=True, db_index=True)
    # Active | Pending | Expired | Extension Filed | Transferred | Rejected
    status = models.CharField(max_length=32, default='Pending', db_index=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    receipt_number = models.CharField(max_length=120, default='', blank=True)
    sponsorship_required = models.BooleanField(default=False)
    # Values for the admin-built extra fields, keyed by field key. The schema
    # lives in OnboardingSetting['work_auth_form'] and applies to every
    # candidate, so it is kept apart from ``detail.details`` — that blob is
    # whitelisted per visa type and would reject these keys.
    custom_fields = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'work_authorizations'
        ordering = ['-created_at']


class WorkAuthorizationDetail(models.Model):
    """Type-specific fields for a work authorization.

    These vary per visa type (H1B: petition/LCA; F1: SEVIS/OPT/CPT; GC EAD: EAD
    number...) and would otherwise be ~20 mostly-null columns, so they live in a
    JSON blob. Anything the system *queries on* is normalised onto
    WorkAuthorization instead.
    """
    work_authorization = models.OneToOneField(
        WorkAuthorization, on_delete=models.CASCADE,
        related_name='detail', db_column='work_authorization_id',
    )
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'work_authorization_details'
        ordering = ['-created_at']


class CandidateDocument(models.Model):
    """An uploaded document, stored base64 in the row (house style — there is no
    file storage backend configured; see UserDocument).

    Replacing a document inserts a NEW row with version+1 and flips the previous
    row's is_active to False, so version history is free and nothing is lost.
    """
    candidate = models.ForeignKey(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='documents', db_column='candidate_id',
    )
    # ssn | driver_license | state_id | visa | i94 | passport, or ``custom_<slug>`` for a
    # document the uploader named themselves.
    doc_type = models.CharField(max_length=40, db_index=True)
    # Display name for a custom document. Empty for the fixed types, whose
    # labels are a frontend concern.
    label = models.CharField(max_length=120, default='', blank=True)
    is_custom = models.BooleanField(default=False)
    file_name = models.CharField(max_length=255, default='', blank=True)
    file_mime = models.CharField(max_length=100, default='', blank=True)
    file_size = models.IntegerField(default=0)  # decoded bytes
    file_data = models.TextField(default='', blank=True)  # base64
    version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    uploaded_by = models.CharField(max_length=255, default='', blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'candidate_documents'
        ordering = ['-uploaded_at']


class HrVerification(models.Model):
    """HR's document checklist. One row per candidate."""
    # The PK is exposed as ``verification_id`` in the table; the Python attribute
    # stays ``id`` so the rest of the code (and DRF) is unaffected.
    id = models.AutoField(primary_key=True, db_column='verification_id')
    candidate = models.OneToOneField(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='hr_verification', db_column='candidate_id',
    )
    # HR's employee code for the candidate once one has been issued. Employees
    # are keyed by email elsewhere in this schema, so this is a plain reference
    # rather than a foreign key.
    employee_id = models.CharField(max_length=64, default='', blank=True, db_index=True)
    # Background | Education | Employment | Criminal | ... — free text, the set
    # is an HR/vendor concern rather than something the app enforces.
    verification_type = models.CharField(max_length=60, default='', blank=True, db_index=True)
    # Third-party background-check agency running the check, if any.
    vendor_name = models.CharField(max_length=255, default='', blank=True)
    requested_on = models.DateTimeField(null=True, blank=True)
    completed_on = models.DateTimeField(null=True, blank=True)
    # Location of the vendor's report. A path/URL, not the file itself — unlike
    # CandidateDocument these reports are not held in the row.
    report_path = models.CharField(max_length=500, default='', blank=True)
    ssn_verified = models.BooleanField(default=False)
    driver_license_verified = models.BooleanField(default=False)
    state_id_verified = models.BooleanField(default=False)
    visa_verified = models.BooleanField(default=False)
    i94_verified = models.BooleanField(default=False)
    passport_verified = models.BooleanField(default=False)
    # The background-check areas the stage exists to cover. These are separate
    # from the document tick-boxes above: a document box means "the right file
    # arrived and matches", these mean "the check on that area came back clean".
    identity_verified = models.BooleanField(default=False)
    education_verified = models.BooleanField(default=False)
    employment_verified = models.BooleanField(default=False)
    address_verified = models.BooleanField(default=False)
    criminal_verified = models.BooleanField(default=False)
    reference_verified = models.BooleanField(default=False)
    # Tick-box state for custom documents, keyed by their ``custom_<slug>``
    # doc_type. The fixed types above get a column each; a custom document is
    # named at upload time, so there is no column to add and this JSON map
    # stands in for one.
    custom_verified = models.JSONField(default=dict, blank=True)
    # Pending | Approved | Rejected
    status = models.CharField(max_length=20, default='Pending', db_index=True)
    remarks = models.TextField(default='', blank=True)
    verified_by = models.CharField(max_length=255, default='', blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hr_verifications'
        ordering = ['-created_at']


class ManagerApproval(models.Model):
    """A manager's decision. Append-only: "return for correction" then re-approve
    produces two rows, so the timeline shows the full history."""
    candidate = models.ForeignKey(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='approvals', db_column='candidate_id',
    )
    # Approved | Rejected | Returned
    action = models.CharField(max_length=20, default='', blank=True, db_index=True)
    comments = models.TextField(default='', blank=True)
    approver = models.CharField(max_length=255, default='', blank=True)
    acted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'manager_approvals'
        ordering = ['-acted_at']


class ItAssetAllocation(models.Model):
    """Hardware handed to the candidate. One row per allocation, so a client
    laptop and an Eversoft laptop can coexist."""
    candidate = models.ForeignKey(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='asset_allocations', db_column='candidate_id',
    )
    # Client | Eversoft
    asset_source = models.CharField(max_length=20, default='Eversoft', db_index=True)
    client_name = models.CharField(max_length=255, default='', blank=True)
    # ['Laptop', 'Monitor', 'Mouse', ...]
    assets = models.JSONField(default=list, blank=True)
    asset_id = models.CharField(max_length=120, default='', blank=True)
    issued_date = models.DateField(null=True, blank=True)
    # Assigned | Returned | Lost | Damaged
    status = models.CharField(max_length=20, default='Assigned', db_index=True)
    allocated_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'it_asset_allocations'
        ordering = ['-created_at']


class PayrollInformation(models.Model):
    """Bank / tax details. One row per candidate."""
    candidate = models.OneToOneField(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='payroll', db_column='candidate_id',
    )
    bank_name = models.CharField(max_length=255, default='', blank=True)
    account_number = models.CharField(max_length=64, default='', blank=True)
    routing_number = models.CharField(max_length=64, default='', blank=True)
    tax_state = models.CharField(max_length=64, default='', blank=True)
    direct_deposit = models.BooleanField(default=False)
    # Pending | Completed
    status = models.CharField(max_length=20, default='Pending', db_index=True)
    completed_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payroll_information'
        ordering = ['-created_at']


class OnboardingStatus(models.Model):
    """One row per (candidate, stage) — drives the coloured stage badges and the
    progress bar. Seeded for every stage when the candidate is created."""
    candidate = models.ForeignKey(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='stages', db_column='candidate_id',
    )
    # see ONBOARDING_STAGES
    stage = models.CharField(max_length=40, db_index=True)
    # Pending | In Progress | Completed | Rejected
    status = models.CharField(max_length=20, default='Pending', db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=255, default='', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'onboarding_status'
        ordering = ['id']
        unique_together = (('candidate', 'stage'),)


class OnboardingActivityLog(models.Model):
    """Append-only audit trail. Every mutating onboarding endpoint writes one row.
    Doubles as the candidate timeline in the UI, so it carries human-readable
    ``event``/``comments`` alongside the machine-readable old/new values."""
    candidate = models.ForeignKey(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='activity_logs', db_column='candidate_id',
    )
    event = models.CharField(max_length=120, default='', blank=True)
    actor_email = models.CharField(max_length=255, default='', blank=True, db_index=True)
    actor_name = models.CharField(max_length=255, default='', blank=True)
    comments = models.TextField(default='', blank=True)
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'onboarding_activity_logs'
        ordering = ['-created_at']


class OnboardingSetting(models.Model):
    """Generic key/value store for onboarding module configuration.

    Currently holds ``candidate_fields`` — the admin-defined schema of extra
    fields shown on the New Candidate form (an ordered list of field
    descriptors: {key, label, type, required, options, width}). Kept as a
    key/value row rather than dedicated columns so new settings never need a
    migration.
    """
    key = models.CharField(max_length=80, primary_key=True)
    value = models.JSONField(default=dict, blank=True)
    updated_by = models.CharField(max_length=255, default='', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'onboarding_settings'
        ordering = ['key']


class OnboardingFormTemplate(models.Model):
    """A reusable New-Candidate form definition produced by the form builder —
    the onboarding twin of JobFormTemplate. ``schema`` is the ordered list of
    sections; the currently active row's schema is what the builder loads and
    publishes to OnboardingSetting['candidate_form']."""
    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=False)
    schema = models.JSONField(default=list, blank=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'onboarding_form_templates'
        ordering = ['name']


class PayrollForm(models.Model):
    """A blank template uploaded by the admin for the candidate to fill/sign.
    Includes the PDF data and field definitions."""
    name = models.CharField(max_length=120, unique=True)
    file_name = models.CharField(max_length=255, default='', blank=True)
    file_mime = models.CharField(max_length=100, default='', blank=True)
    file_data = models.TextField(default='', blank=True)  # base64
    # Optional field positions schema for overlaid fields (docu-sign style)
    # schema: [{'key': 'bankName', 'label': 'Bank Name', 'x': 100, 'y': 200, 'page': 1}, ...]
    schema = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payroll_forms'
        ordering = ['name']


class CandidateFormSubmission(models.Model):
    """Submission from the candidate for a specific PayrollForm.
    Stores JSON responses, signature base64, and the completed PDF."""
    candidate = models.ForeignKey(
        OnboardingCandidate, on_delete=models.CASCADE,
        related_name='form_submissions', db_column='candidate_id',
    )
    form = models.ForeignKey(
        PayrollForm, on_delete=models.CASCADE,
        related_name='submissions', db_column='form_id',
    )
    # Filled fields data, e.g., {'bankName': 'Chase', 'accountNumber': '1234'}
    filled_data = models.JSONField(default=dict, blank=True)
    signature_data = models.TextField(default='', blank=True)  # base64 signature image
    signed_at = models.DateTimeField(null=True, blank=True)
    # The generated/uploaded completed PDF document
    file_name = models.CharField(max_length=255, default='', blank=True)
    file_mime = models.CharField(max_length=100, default='application/pdf', blank=True)
    file_size = models.IntegerField(default=0)
    file_data = models.TextField(default='', blank=True)  # base64
    # digital | offline
    mode = models.CharField(max_length=20, default='digital', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'candidate_form_submissions'
        ordering = ['-created_at']




class EmailTemplate(models.Model):
    """A reusable candidate follow-up email, editable from the Email Preview
    drawer. `outcome` ties a template to Selected / Waitlisted / Rejected so the
    right ones surface first; blank means it suits any outcome.

    `subject` and `body` may contain {{name}}, {{role}}, {{company}} and
    {{outcome}} placeholders, filled in per candidate when the template loads.
    """
    name = models.CharField(max_length=140, unique=True)
    outcome = models.CharField(max_length=40, default='', blank=True, db_index=True)
    subject = models.CharField(max_length=300, default='', blank=True)
    body = models.TextField(default='', blank=True)          # HTML
    # True for the three shipped defaults, so they can be reset but not lost.
    is_builtin = models.BooleanField(default=False)
    created_by = models.CharField(max_length=255, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'email_templates'
        ordering = ['outcome', 'name']


# ===========================================================================
# Core Payroll Module Models
# ===========================================================================

class EmployeeCompensation(models.Model):
    """Effective-dated compensation records per employee (identified by email)."""
    email = models.CharField(max_length=255, db_index=True)
    pay_type = models.CharField(max_length=20, default='salaried')  # salaried | hourly
    pay_frequency = models.CharField(max_length=20, default='monthly')  # monthly | semimonthly | biweekly | weekly
    base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    annual_ctc = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=10, default='USD')
    effective_from = models.DateField(default=timezone.now)
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default='active')  # active | inactive
    notes = models.TextField(default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_compensation'
        ordering = ['-effective_from']


class PayComponent(models.Model):
    """Catalogue of earnings and deduction components."""
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    component_type = models.CharField(max_length=20, default='earning')  # earning | deduction
    calc_type = models.CharField(max_length=30, default='fixed')  # fixed | percent_of_base | formula
    rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    default_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    is_taxable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pay_components'
        ordering = ['component_type', 'code']


class EmployeePayComponent(models.Model):
    """Per-employee pay component overrides."""
    email = models.CharField(max_length=255, db_index=True)
    component = models.ForeignKey(PayComponent, on_delete=models.CASCADE, related_name='employee_components')
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    effective_from = models.DateField(default=timezone.now)
    effective_to = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_pay_components'
        ordering = ['-effective_from']


class PayrollRun(models.Model):
    """Batch pay period run engine record."""
    period_label = models.CharField(max_length=20, db_index=True)  # e.g., '2026-08'
    period_start = models.DateField()
    period_end = models.DateField()
    pay_date = models.DateField(null=True, blank=True)
    frequency = models.CharField(max_length=20, default='monthly')
    status = models.CharField(max_length=30, default='draft')  # draft | processing | pending_approval | approved | paid | cancelled
    created_by = models.CharField(max_length=255, default='', blank=True)
    approved_by = models.CharField(max_length=255, default='', blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    total_gross = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_net = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    employee_count = models.IntegerField(default=0)
    notes = models.TextField(default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payroll_runs'
        ordering = ['-period_start']


class Payslip(models.Model):
    """Individual employee payslip record for a pay run."""
    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name='payslips')
    email = models.CharField(max_length=255, db_index=True)
    employee_name = models.CharField(max_length=255, default='', blank=True)
    worked_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    paid_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    lop_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    overtime_hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    gross_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_deductions = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    net_pay = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    earnings_data = models.JSONField(default=list, blank=True)
    deductions_data = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, default='draft')  # draft | approved | paid
    file_name = models.CharField(max_length=255, default='', blank=True)
    file_mime = models.CharField(max_length=100, default='application/pdf', blank=True)
    file_size = models.IntegerField(default=0)
    file_data = models.TextField(default='', blank=True)  # base64 PDF
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payslips'
        unique_together = ['run', 'email']
        ordering = ['employee_name', 'email']


class PayrollSetting(models.Model):
    """Key/value configuration store for Payroll module."""
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(default='', blank=True)
    description = models.CharField(max_length=255, default='', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payroll_settings'
        ordering = ['key']


# ==========================================================================
# Employee Chat models (appended by chat-module integration)
# ==========================================================================
class ChatRoom(models.Model):
    name = models.CharField(max_length=255)
    is_group = models.BooleanField(default=False)
    created_by = models.CharField(max_length=255)
    created_at = models.DateTimeField()
    is_private = models.BooleanField(default=True)

    class Meta:
        db_table = "chat_rooms"
        managed = False


class ChatMember(models.Model):
    employee_email = models.CharField(max_length=255)

    room = models.ForeignKey(
        ChatRoom,
        db_column="room_id",
        on_delete=models.CASCADE,
    )

    joined_at = models.DateTimeField()
    # Channel admins can add/remove members and manage other admins.
    is_admin = models.BooleanField(default=False)
    # When this member last opened/read the room. Used to compute per-member
    # read receipts — a channel message is "Seen" only once EVERY other member's
    # last_read_at has passed the message's timestamp.
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "chat_members"
        managed = False


class ChatMessage(models.Model):
    sender_email = models.CharField(max_length=255)

    sender_name = models.CharField(max_length=255)

    room = models.ForeignKey(
        ChatRoom,
        db_column="room_id",
        on_delete=models.CASCADE,
    )

    message = models.TextField()

    is_read = models.BooleanField(default=False)

    created_at = models.DateTimeField()

    # Editing / deleting (soft delete keeps the row so the timeline stays intact)
    edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)

    # File sharing. New uploads are written to disk and referenced by
    # ``attachment_path`` (keeps large files — video, etc. — out of the DB).
    # ``attachment_data`` (base64) is retained for older messages / fallback.
    attachment_name = models.CharField(max_length=255, null=True, blank=True)
    attachment_type = models.CharField(max_length=100, null=True, blank=True)
    attachment_data = models.TextField(null=True, blank=True)
    attachment_path = models.CharField(max_length=512, null=True, blank=True)

    # Pinned messages (WhatsApp-style) + threaded replies.
    is_pinned = models.BooleanField(default=False)
    # Pin metadata — who pinned it, when, and when the pin auto-expires.
    # A pin lasts 30 days; after ``pin_expires_at`` it stops showing in the
    # pinned section (the underlying message is untouched). Only ``pinned_by``
    # may unpin — enforced in the view, not just the UI.
    pinned_by = models.CharField(max_length=255, null=True, blank=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    pin_expires_at = models.DateTimeField(null=True, blank=True)
    reply_to_id = models.IntegerField(null=True, blank=True)
    reply_to_sender = models.CharField(max_length=255, null=True, blank=True)
    reply_to_text = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        db_table = "chat_messages"
        managed = False
        ordering = ["created_at"]


class ChatMeeting(models.Model):
    """A scheduled meeting attached to a chat room (direct or channel).

    Stores the schedule plus a shareable ``join_url`` (e.g. a Jitsi Meet room
    link). No video server is run by the app — the link is what participants
    click to join. ``managed = False`` because the ``chat_meetings`` table is
    created by migration 0045_chat_tables, matching the
    other chat tables."""

    room = models.ForeignKey(
        ChatRoom,
        db_column="room_id",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    title = models.CharField(max_length=255)
    description = models.TextField(default="", blank=True)
    scheduled_at = models.DateTimeField()
    duration_minutes = models.IntegerField(default=30)

    created_by = models.CharField(max_length=255)
    created_by_name = models.CharField(max_length=255, default="", blank=True)

    join_url = models.CharField(max_length=512, default="", blank=True)
    # Comma-separated attendee emails (kept simple so the table needs no JSON
    # column on older MySQL/MariaDB installs).
    attendees = models.TextField(default="", blank=True)

    created_at = models.DateTimeField()

    class Meta:
        db_table = "chat_meetings"
        managed = False
        ordering = ["scheduled_at"]
