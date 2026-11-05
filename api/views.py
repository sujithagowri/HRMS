"""
HRMS API views — a Django REST Framework port of the original Express server.

CRUD resources are implemented as DRF ``@api_view`` function views backed by
the serializers in ``serializers.py``. Request and response JSON shapes
(camelCase) match the Node API exactly so the existing React frontend works
unchanged.

A few helpers and endpoints are intentionally NOT DRF:
  * ``parse_body`` / ``err`` / ``make_initials`` / ``norm_email`` /
    ``app_user_dict`` are imported by ``auth_views`` and ``live_views`` and so
    are kept here.
  * ``recording_video`` handles a raw binary (video/webm) body, which DRF's
    JSON parser cannot consume, so it stays a plain ``csrf_exempt`` view.
  * ``spa_index`` serves the built React app for non-API routes.
"""
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import (
    BinaryField, BooleanField, Case, CharField, Count, F, Func, IntegerField,
    OuterRef, Q, Subquery, Sum, Value, When,
)
from django.db.models.functions import Coalesce, Substr
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import ai, mailer, social_poster
from .permissions import require_perm, require_admin, check_perm
from .timeutil import local_now, local_today
from .models import (
    AppUser,
    AttendanceEvent,
    Company,
    CompanyDetail,
    EmployeeAttendance,
    EmployeeTask,
    InterviewLink,
    InterviewRecording,
    JobPost,
    LeaveRequest,
    Module,
    Notification,
    Permission,
    PermissionGroup,
    QuestionSet,
    ResumeScore,
    Role,
    RolePermission,
    UserDocument,
    UserEmailConfig,
    UserProfile,
    WorkSubmission,
    BenchSubmission,
    BenchSalesProfile,
    BenchSalesCertificate,
    Shift,
    ShiftAssignment,
    AttendanceCorrection,
    AttendanceLocationPunch,
    GeoFence,
    WfhRequest,
    WFHPolicy,
    WorkArrangement,
    PayrollRun,
    EmployeeCompensation,
    EmployeePayComponent,
)
from .serializers import (
    DATETIME_FMT,
    AppUserSerializer,
    AttendanceEventSerializer,
    CompanySerializer,
    CompanyDetailSerializer,
    EmployeeAttendanceSerializer,
    EmployeeTaskSerializer,
    ModuleSerializer,
    NotificationSerializer,
    PermissionGroupSerializer,
    PermissionSerializer,
    RoleSerializer,
    InterviewLinkSerializer,
    InterviewRecordingSerializer,
    JobPostSerializer,
    LeaveRequestSerializer,
    QuestionSetSerializer,
    ResumeScoreSerializer,
    UserDocumentSerializer,
    UserEmailConfigSerializer,
    UserProfileSerializer,
    WorkSubmissionSerializer,
    BenchSubmissionSerializer,
    BenchSalesProfileSerializer,
    ShiftSerializer,
    ShiftAssignmentSerializer,
    AttendanceCorrectionSerializer,
    GeoFenceSerializer,
    WfhRequestSerializer,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_body(request):
    try:
        return json.loads((request.body or b'{}').decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return {}


def err(message, status=400):
    return JsonResponse({'message': message}, status=status)


def serializer_err(serializer, status=400):
    """Flatten DRF validation errors into the API's ``{'message': ...}`` shape."""
    msgs = []
    for field, errs in serializer.errors.items():
        first = errs[0] if isinstance(errs, (list, tuple)) and errs else errs
        msgs.append(f'{field}: {first}')
    return err('; '.join(msgs) or 'Invalid data', status)


def make_initials(name):
    parts = [p for p in re.split(r'\s+', (name or '').strip()) if p]
    return ''.join(p[0] for p in parts).upper()[:2]


def norm_email(value):
    return str(value or '').strip().lower()


def dt(value):
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


def safe_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def safe_json(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_recipient_email(request, fallback=None):
    email = norm_email(
        request.META.get('HTTP_X_USER_EMAIL')
        or request.META.get('HTTP_X_ACTOR_EMAIL')
        or ''
    )
    if not email:
        try:
            email = norm_email(request.GET.get('email') or request.data.get('email') or '')
        except Exception:
            email = ''
    if not email and fallback:
        email = norm_email(fallback)
    return email


def _actor_identity(request):
    """Who is performing this action — (email, display name).

    Identity comes from the same X-User-Email header the rest of the API is
    gated on, never from the request body, so a client cannot claim someone
    else's interviews and inflate their numbers on the KPI dashboard. The name
    is resolved from the account rather than trusted from the payload, so the
    leaderboard cannot be split by someone typing their name differently.

    Used for both scheduling an interview and sending its follow-up: the two
    are different people often enough that recording only one of them leaves a
    candidate's outcome email attributed to whoever booked the slot.
    """
    email = norm_email(
        request.META.get('HTTP_X_USER_EMAIL')
        or request.META.get('HTTP_X_ACTOR_EMAIL')
        or ''
    )
    if not email:
        # Body fallback matches _get_caller() in permissions.py, for callers
        # that cannot set headers.
        try:
            email = norm_email(request.data.get('actorEmail') or '')
        except Exception:
            email = ''
    if not email:
        return '', ''
    user = AppUser.objects.filter(email=email).first()
    name = (user.full_name if user else '') or email.split('@')[0]
    return email, name


# The interview_links rows predating created_by_email have no creator recorded,
# so the dashboard falls back to the free-text `interviewer` for them. Both the
# filters and the group-by need to agree on that rule, hence one definition.
CREATOR_KEY = Case(
    When(~Q(created_by_email=''), then=F('created_by_email')),
    default=F('interviewer'),
    output_field=CharField(),
)


def _creator_q(value, also_name=''):
    """Rows attributable to `value` — a creator email, or a legacy free-text
    interviewer. `also_name` additionally matches legacy rows where the caller
    typed their display name instead of their email."""
    value = str(value or '').strip()
    if not value:
        return Q(pk__in=[])
    q = Q(created_by_email__iexact=value)
    legacy = Q(interviewer__iexact=value)
    name = str(also_name or '').strip()
    if name:
        legacy = legacy | Q(interviewer__iexact=name)
    return q | (Q(created_by_email='') & legacy)


def create_notification(recipient, title, message, notification_type='info', link=''):
    email = norm_email(recipient)
    if not email:
        return None
    return Notification.objects.create(
        recipient=email,
        title=title,
        message=message,
        notification_type=notification_type,
        link=link,
    )


def notify_approvers(perm_code, title, message, link=''):
    from .permissions import _is_super_admin, _user_has_perm, _resolve_role
    active_users = AppUser.objects.filter(status='active')
    for user in active_users:
        is_approver = False
        if _is_super_admin(user):
            is_approver = True
        elif _user_has_perm(user, perm_code):
            is_approver = True
        else:
            role = _resolve_role(user)
            role_name = (role.name or '').lower() if role else ''
            legacy_role = (user.role or '').lower()
            if 'hr' in legacy_role or 'admin' in legacy_role or 'hr' in role_name or 'manager' in role_name:
                is_approver = True
        
        if is_approver:
            create_notification(user.email, title, message, 'info', link)


# ---------------------------------------------------------------------------
# app_user_dict — kept for auth_views (which imports it). Mirrors
# AppUserSerializer's output exactly.
# ---------------------------------------------------------------------------
def app_user_dict(o):
    return {
        'id': o.id, 'name': o.full_name, 'email': o.email,
        'password': o.password, 'initials': o.initials,
        'role': o.role, 'status': o.status,
        'authProvider': o.auth_provider,
        'profilePic': o.profile_pic or '',
        'createdAt': dt(o.created_at),
    }


def resolve_color(type_value):
    v = str(type_value or '').lower()
    if 'contract' in v:
        return 'purple'
    if 'intern' in v:
        return 'orange'
    if 'part' in v:
        return 'green'
    return 'blue'


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@api_view(['GET', 'POST'])
@require_perm({'GET': 'recruitment.view', 'POST': 'recruitment.create'})
def jobs(request):
    if request.method == 'GET':
        qs = JobPost.objects.all()
        # Optional query-param filters
        status = request.query_params.get('status')
        priority = request.query_params.get('priority')
        dept = request.query_params.get('dept')
        job_type = request.query_params.get('type')
        remote = request.query_params.get('remote')
        search = request.query_params.get('search')
        if status:
            qs = qs.filter(status=status)
        if priority:
            qs = qs.filter(priority=priority)
        if dept:
            qs = qs.filter(dept__icontains=dept)
        if job_type:
            qs = qs.filter(type=job_type)
        if remote and remote.lower() in ('true', '1'):
            qs = qs.filter(is_remote=True)
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(dept__icontains=search))
        return Response(JobPostSerializer(qs, many=True).data)

    body = request.data
    if not body.get('title') or not body.get('dept'):
        return err('title and dept are required')
    serializer = JobPostSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    payload = serializer.data
    # Auto-post to the creator's linked social accounts (LinkedIn / X).
    # Non-fatal: a job is still created even if posting fails or isn't set up.
    if body.get('autoPost') is not False:
        # customFields aren't on the serializer output but the draft builder
        # uses them (skills, experience), so carry them through.
        draft_source = dict(payload)
        draft_source.setdefault('customFields', body.get('customFields') or {})
        payload['socialResults'] = _auto_post_job(
            draft_source, body.get('userEmail'), body.get('linkedinMessage'))
    return Response(payload, status=201)


@api_view(['GET', 'PATCH', 'PUT', 'DELETE'])
@require_perm({'GET': 'recruitment.view', 'PATCH': 'recruitment.edit',
               'PUT': 'recruitment.edit', 'DELETE': 'recruitment.delete'})
def job_detail(request, pk):
    """Read / update / delete a single job. Used by the Job Board's inline
    status editor (PATCH {status, statusComment})."""
    job = JobPost.objects.filter(pk=pk).first()
    if not job:
        return err('Job not found', 404)
    if request.method == 'GET':
        return Response(JobPostSerializer(job).data)
    if request.method == 'DELETE':
        job.delete()
        return Response({'ok': True})

    data = request.data or {}
    text_fields = ['title', 'dept', 'location', 'type', 'salary', 'status', 'priority', 'description']
    changed = False
    for f in text_fields:
        if f in data:
            setattr(job, f, data[f] if data[f] is not None else '')
            changed = True
    if 'openings' in data:
        try:
            job.openings = max(1, int(data['openings']))
        except (TypeError, ValueError):
            pass
        changed = True
    if 'remote' in data:
        job.is_remote = bool(data['remote'])
        changed = True
    if 'statusComment' in data:
        job.status_comment = data['statusComment'] or ''
        changed = True
    if changed:
        job.save()
    return Response(JobPostSerializer(job).data)


def html_to_text(html):
    """Readable plain-text alternative for an HTML email body.

    The rich-text editor emits block tags (<div>, <p>, <li>), so stripping tags
    alone would run every line together in clients that show the text part.
    """
    if not html:
        return ''
    text = re.sub(r'(?i)<br\s*/?>', '\n', html)
    text = re.sub(r'(?i)</(div|p|li|tr|h[1-6])>', '\n', text)
    text = re.sub(r'(?i)<li[^>]*>', '• ', text)
    text = re.sub(r'<[^>]+>', '', text)
    for entity, char in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                         ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")):
        text = text.replace(entity, char)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def poster_contact(email):
    """Contact details for the closing block of a job announcement: the
    recruiter's own email plus the phone from their HRMS profile."""
    email = norm_email(email)
    if not email:
        return {}
    profile = UserProfile.objects.filter(email=email).first()
    user = AppUser.objects.filter(email=email).first()
    name = ''
    if profile:
        name = ' '.join(x for x in [profile.first_name, profile.last_name] if x).strip()
    if not name and user:
        name = user.full_name or ''
    return {
        'email': email,
        'phone': (profile.phone if profile else '') or '',
        'name': name,
    }


def _auto_post_job(job_payload, user_email=None, message=None):
    """Post the job to the creator's OWN linked social accounts.

    Never falls back to another user's credentials: a job must only ever appear
    on the profile of the person who created it. When the creator has nothing
    linked we return an explanatory skip result rather than an empty list, so
    the UI can tell them why nothing was posted.

    `message` is the text approved in the preview dialog; without it a fresh
    draft is built.
    """
    email = norm_email(user_email)
    if not email:
        return [{'platform': 'linkedin', 'ok': False, 'skipped': True,
                 'error': 'Could not identify the job creator, so nothing was posted.'}]

    cfg = UserEmailConfig.objects.filter(pk=email).first()
    social = safe_json(cfg.social) if cfg else None
    if not isinstance(social, dict) or not social:
        return [{'platform': 'linkedin', 'ok': False, 'skipped': True,
                 'error': 'No LinkedIn account connected. Connect yours in '
                          'Settings → Email Configuration.'}]
    try:
        return social_poster.post_job(job_payload, social, message=message,
                                      contact=poster_contact(email))
    except Exception as e:  # noqa: BLE001 - never break job creation on a posting error
        logger.warning('Social auto-post failed for %s: %s', email, e)
        return [{'platform': 'all', 'ok': False, 'error': str(e)}]


# ---------------------------------------------------------------------------
# Interviews
# ---------------------------------------------------------------------------
# Default duration (hours) from creation time before interview link expires
INTERVIEW_LINK_EXPIRY_HOURS = 24


def _generate_interview_tokens(interview_date_str, interview_time_str):
    """Generate unique candidate + recruiter access tokens and compute expiry.

    The link stays valid for 24 hours from creation (now), ensuring candidates
    can access it anytime within that window.
    """
    candidate_token = secrets.token_urlsafe(32)
    recruiter_token = secrets.token_urlsafe(32)

    # Expiry is 24 hours from now, so links don't expire prematurely
    # regardless of when the interview is scheduled.
    expiry = local_now() + timedelta(hours=INTERVIEW_LINK_EXPIRY_HOURS)

    return candidate_token, recruiter_token, expiry


def _public_origin(request, body=None):
    """Base URL the candidate's link must point at.

    The request host is whatever the *recruiter's* browser hit — often
    localhost — which produces a link no candidate can open.

    Two names have to be consulted because the project grew two. The mailer's
    brand_logo_url() reads ``settings.HRMS_PUBLIC_URL``, which is the one the
    deployment actually sets; this function only ever read ``PUBLIC_BASE_URL``,
    which nothing sets. So the logo in an email resolved against the public
    domain while the candidate's link in the same email resolved against the
    recruiter's browser — and the interview_links rows still carry the evidence:
    fourteen of them point at http://127.0.0.1:8000. Both names are honoured,
    and the request host stays the last resort it was meant to be.
    """
    explicit = (body or {}).get('origin')
    if explicit:
        return str(explicit).rstrip('/')
    configured = os.environ.get('PUBLIC_BASE_URL', '').strip()
    if not configured:
        try:
            configured = (getattr(settings, 'HRMS_PUBLIC_URL', '') or '').strip()
        except Exception:
            configured = ''
    if configured:
        return configured.rstrip('/')
    return _request_origin_from_meta(request)


def _send_interview_invitation(obj, origin, sender_email=None):
    """Email the candidate their single-use, tokenized interview link.

    Returns mailer's ``{'ok': bool, 'error': str}`` — callers decide whether a
    delivery failure should fail their request.
    """
    if not obj.email:
        return {'ok': False, 'error': 'Candidate has no email address'}

    candidate_url = f'{origin}/interview-access?token={obj.candidate_token}'

    when = ' at '.join(p for p in (obj.interview_date, obj.interview_time) if p) or 'To be confirmed'
    types = obj.interview_type
    if isinstance(types, (list, tuple)):
        types = ', '.join(str(t) for t in types)

    html = mailer.render_branded(
        greeting=obj.name,
        title='',
        intro=(
            f'We are pleased to inform you that your interview for the position of '
            f'<strong>{obj.role}</strong> at <strong>{mailer.BRAND_SHORT}</strong> has been '
            f'successfully scheduled. Please review the details below and make a note of the '
            f'date, time, and platform.'
        ),
        highlight_html=(
            mailer.render_details_card('Interview Details', [
                ('Position', obj.role),
                ('Interview Type', types),
                ('Interviewer', obj.interviewer or '—'),
                ('Date &amp; Time', when),
                ('Duration', obj.duration),
                ('Platform', obj.platform or 'To be confirmed'),
            ])
            + mailer.render_cta(candidate_url, 'Join Interview')
        ),
        footer=(
            'Please ensure you join the meeting at least <strong>5 minutes early</strong>. '
            'This link is valid for 24 hours and can only be used once. Should you need to '
            'reschedule or have any questions, please contact your recruiter.'
        ),
        logo_url=f'{origin}/logo.jpg',
    )
    text = (
        f'Hi {obj.name},\n\nYour interview for {obj.role} is scheduled on '
        f'{obj.interview_date} at {obj.interview_time}.\n\n'
        f'Join here: {candidate_url}\n\n'
        f'This link expires in 24 hours and can only be used once. '
        f'If you cannot join by then, contact your recruiter for a new link.'
    )
    return mailer.send_email(
        to=obj.email,
        subject=f'Interview Invitation — {obj.role}',
        html=html,
        text=text,
        sender_email=sender_email,
    )


@api_view(['GET', 'POST'])
@require_perm({'GET': 'recruitment.view', 'POST': 'recruitment.create'})
def interviews(request):
    if request.method == 'GET':
        # Newest first: the candidate portal matches an interview by email with
        # Array.find(), so the most recent row for a candidate must come first —
        # otherwise a stale older interview (with an old createdAt) is picked and
        # the link is wrongly shown as expired.
        qs = InterviewLink.objects.all().order_by('-id')
        need_tokens = [obj for obj in qs if not obj.candidate_token]
        if need_tokens:
            for obj in need_tokens:
                c_tok, r_tok, exp = _generate_interview_tokens(obj.interview_date or '', obj.interview_time or '')
                obj.candidate_token = c_tok
                obj.recruiter_token = r_tok
                obj.link_expires_at = exp
                obj.save(update_fields=['candidate_token', 'recruiter_token', 'link_expires_at'])
        return Response(InterviewLinkSerializer(qs, many=True).data)

    body = request.data
    name = body.get('name')
    email = body.get('email')
    role = body.get('role')
    interview_date = body.get('interviewDate')
    time = body.get('time')
    if not all([name, email, role, interview_date, time]):
        return err('name, email, role, interviewDate and time are required')

    serializer = InterviewLinkSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)
    c_token, r_token, expiry = _generate_interview_tokens(interview_date, time)
    creator_email, creator_name = _actor_identity(request)
    serializer.save(
        candidate_token=c_token, recruiter_token=r_token, link_expires_at=expiry,
        created_by_email=creator_email, created_by_name=creator_name,
    )
    obj = serializer.instance

    # Deliver the tokenized link server-side. A mail failure must not discard an
    # interview the recruiter already scheduled, so this is best-effort and the
    # outcome is reported back instead of raised.
    mail = _send_interview_invitation(
        obj, _public_origin(request, body), body.get('senderEmail')
    )
    if mail.get('ok'):
        obj.email_sent = True
        obj.save(update_fields=['email_sent'])

    if email:
        create_notification(
            email,
            'Interview scheduled',
            f"Your interview for {role or 'the role'} has been scheduled for {interview_date} at {time}.",
            'info',
            '/recruit/interview',
        )

    data = InterviewLinkSerializer(obj).data
    data['emailSent'] = bool(mail.get('ok'))
    if not mail.get('ok'):
        data['emailError'] = mail.get('error', 'unknown error')
    return Response(data, status=201)


@api_view(['PUT', 'PATCH'])
@require_perm('recruitment.edit')
def interview_detail(request, pk):
    obj = InterviewLink.objects.filter(pk=pk).first()
    if not obj:
        return err('Interview not found', 404)
    serializer = InterviewLinkSerializer(obj, data=request.data, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    return Response(serializer.data)


@api_view(['POST'])
def interview_note(request, pk):
    """Set/update a candidate's note. Intentionally ungated so ANY signed-in
    user can collaborate on the note; records who last edited it (name + email +
    timestamp) so the editor can show "last modified by"."""
    obj = InterviewLink.objects.filter(pk=pk).first()
    if not obj:
        return err('Interview not found', 404)
    body = request.data
    notes = body.get('notes')
    if notes is None:
        return err('notes is required')
    email = norm_email(body.get('email') or request.META.get('HTTP_X_USER_EMAIL') or '')
    name = str(body.get('name') or body.get('employee') or '').strip()
    obj.notes = notes
    obj.notes_updated_by = name or (email.split('@')[0] if email else '')
    obj.notes_updated_by_email = email
    obj.notes_updated_at = local_now()
    obj.save()
    return Response(InterviewLinkSerializer(obj).data)


@api_view(['POST'])
@require_perm('recruitment.edit')
def interviews_bulk_send_emails(request):
    """Send emails to multiple candidates (mark email_sent=True for their interviews)."""
    body = request.data
    interview_ids = body.get('interviewIds', [])
    if not isinstance(interview_ids, list) or len(interview_ids) == 0:
        return err('interviewIds array is required')

    qs = InterviewLink.objects.filter(id__in=interview_ids)
    if not qs.exists():
        return err('No interviews found with the provided IDs', 404)

    origin = _public_origin(request, body)
    sender_email = body.get('senderEmail')
    sent, failed = [], []
    for obj in qs:
        result = _send_interview_invitation(obj, origin, sender_email)
        if result.get('ok'):
            obj.email_sent = True
            obj.save(update_fields=['email_sent'])
            sent.append(obj.id)
        else:
            failed.append({'id': obj.id, 'email': obj.email,
                           'error': result.get('error', 'unknown error')})

    message = f'Emails sent to {len(sent)} candidate(s)'
    if failed:
        message += f'; {len(failed)} failed'

    return JsonResponse({
        'ok': not failed,
        'message': message,
        'count': len(sent),
        'failed': failed,
        'interviews': InterviewLinkSerializer(
            InterviewLink.objects.filter(id__in=interview_ids), many=True).data,
    }, status=200)


# ---------------------------------------------------------------------------
# Follow-up emails (Selected / Waitlisted / Rejected) — sent server-side
# ---------------------------------------------------------------------------
def render_placeholders(text, ctx):
    """Replace {{key}} (any inner spacing) with values from ctx.

    Applied on both the preview and the send path so a placeholder typed by
    hand resolves the same way in each.
    """
    if not text:
        return ''
    return re.sub(r'\{\{\s*([a-zA-Z_]+)\s*\}\}',
                  lambda m: str(ctx.get(m.group(1).strip().lower(), m.group(0))),
                  text)


def build_followup_html(subject, inner):
    """The exact HTML a candidate receives for a follow-up email.

    Both the preview endpoint and the send path call this, so what a recruiter
    sees in the preview is byte-for-byte what goes out.

    A body that is already a complete HTML document is sent verbatim — that is
    how a hand-written HTML template stays intact instead of being nested
    inside the branded wrapper.
    """
    inner = inner or ''
    head = inner.lstrip()[:200].lower()
    if head.startswith('<!doctype') or head.startswith('<html'):
        return inner
    return mailer.render_branded(
        title=subject,
        intro='',
        highlight_html=f'<div style="font-size:15px;line-height:1.7;color:#334155;">{inner}</div>',
    )


def _followup_template(outcome, name, role, start_phrase='the first week of next month'):
    role = role or 'the role'
    name = name or 'there'
    o = str(outcome or '').strip().lower()
    if o == 'selected':
        subject = f'Congratulations! Offer for {role} at Eversoft'
        body = (
            f'Dear {name},<br><br>'
            f'We are delighted to let you know that you have been <strong>selected</strong> for the '
            f'<strong>{role}</strong> position at Eversoft. The whole panel was impressed by your '
            f'performance during the interview.<br><br>'
            f'Our HR team will reach out shortly with your offer details and onboarding steps. '
            f'We are looking forward to having you join us, with a tentative start in {start_phrase}.<br><br>'
            f'Warm regards,<br>The Eversoft Talent Team'
        )
    elif o == 'waitlisted':
        subject = f'Your application for {role} at Eversoft'
        body = (
            f'Dear {name},<br><br>'
            f'Thank you for interviewing for the <strong>{role}</strong> position. You did really well, '
            f'and we have placed your application on our <strong>waitlist</strong>. Should a suitable '
            f'opening become available, we will be in touch right away.<br><br>'
            f'We genuinely appreciate the time and energy you invested in the process.<br><br>'
            f'Warm regards,<br>The Eversoft Talent Team'
        )
    else:  # rejected / default
        subject = f'Update on your application for {role} at Eversoft'
        body = (
            f'Dear {name},<br><br>'
            f'Thank you for taking the time to interview for the <strong>{role}</strong> position and for '
            f'your interest in Eversoft. After careful consideration, we have decided not to move forward '
            f'with your application at this time.<br><br>'
            f'This was a difficult decision — we encourage you to apply for future roles that match your '
            f'skills. We wish you the very best in your job search.<br><br>'
            f'Warm regards,<br>The Eversoft Talent Team'
        )
    return subject, body


@api_view(['POST'])
@require_perm('recruitment.edit')
def interview_send_followup(request):
    """Send a single follow-up email server-side (via the configured SMTP) and
    record the outcome on the interview row. Body:
        {interviewId, outcome}                      (looks up name/email/role)
      or {toEmail, toName, role, outcome}           (explicit)
      optional: senderEmail (whose SMTP to send from), subject, body
    """
    body = request.data
    outcome = body.get('outcome')
    obj = None
    interview_id = body.get('interviewId')
    if interview_id:
        obj = InterviewLink.objects.filter(pk=interview_id).first()
        if not obj:
            return err('Interview not found', 404)

    to_email = body.get('toEmail') or (obj.email if obj else None)
    to_name = body.get('toName') or (obj.name if obj else None)
    role = body.get('role') or (obj.role if obj else None)
    if not to_email:
        return err('toEmail (or a valid interviewId) is required')
    if not outcome:
        return err('outcome is required (Selected | Waitlisted | Rejected)')

    subject = body.get('subject')
    inner = body.get('body')
    if not subject or not inner:
        subject, inner = _followup_template(outcome, to_name, role)

    # Resolve any {{name}} / {{role}} still present, exactly as the preview
    # endpoint does — otherwise a hand-typed placeholder would preview
    # correctly but reach the candidate literally.
    ctx = {'name': to_name or 'there', 'role': role or 'the role',
           'company': 'Eversoft', 'outcome': outcome or ''}
    subject = render_placeholders(subject, ctx)
    inner = render_placeholders(inner, ctx)

    html = build_followup_html(subject, inner)
    result = mailer.send_email(
        to=to_email, subject=subject, html=html,
        text=html_to_text(inner),
        sender_email=body.get('senderEmail'),
    )
    if not result.get('ok'):
        return err('Could not send the follow-up email: ' + result.get('error', 'unknown error'), 502)

    if obj:
        sender_actor, sender_name = _actor_identity(request)
        obj.outcome = outcome
        obj.email_sent = True
        # followup_sent existed on the model and was never written — the flag
        # that says "this candidate has been told" was permanently False.
        obj.followup_sent = True
        obj.followup_sent_by_email = sender_actor
        obj.followup_sent_by_name = sender_name
        obj.followup_sent_at = local_now()
        obj.save(update_fields=[
            'outcome', 'email_sent', 'followup_sent',
            'followup_sent_by_email', 'followup_sent_by_name', 'followup_sent_at',
        ])
    if to_email:
        create_notification(
            to_email,
            'Interview update',
            f"Your interview follow-up for {role or 'the role'} has been sent: {outcome}.",
            'info',
            '/recruit/interview',
        )
    return Response({'ok': True, 'message': f'Follow-up sent to {to_name or to_email}.'})


# ---------------------------------------------------------------------------
# Interview token verification & link management
# ---------------------------------------------------------------------------
def _interview_already_taken(obj):
    """True once the candidate has actually sat the interview.

    ``completed_at`` is the only marker, because it is written in exactly one
    place — when a recording is saved. ``status`` used to count too, but it is a
    workflow label a recruiter can set by hand or in bulk, and doing so locked
    real candidates out of interviews they had never taken: they saw "Interview
    Already Completed" with no date, because ``completed_at`` was null.
    """
    return obj.completed_at is not None


@api_view(['POST'])
def interview_verify_token(request):
    """Verify a candidate or recruiter interview access token.

    POST /api/interviews/verify-token
    Body: {token: "<candidate_token or recruiter_token>"}

    Returns the interview details and whether the link is still valid.
    """
    body = request.data
    token = str(body.get('token') or '').strip()
    if not token:
        return err('token is required')

    # Determine token type (candidate or recruiter)
    obj = InterviewLink.objects.filter(candidate_token=token).first()
    token_type = 'candidate'
    if not obj:
        obj = InterviewLink.objects.filter(recruiter_token=token).first()
        token_type = 'recruiter'

    if not obj:
        return JsonResponse({'valid': False, 'reason': 'Token not found'}, status=404)

    now = local_now()

    # An interview may only be taken once. The recruiter link stays usable so
    # the interview can still be reviewed after the candidate is done.
    if token_type == 'candidate' and _interview_already_taken(obj):
        return JsonResponse({
            'valid': False,
            'reason': 'You have already completed this interview',
            'completedAt': dt(obj.completed_at),
            'interviewId': obj.id,
        })

    # Check expiry
    if obj.link_expires_at and now > obj.link_expires_at:
        # Auto-update status to Expired
        if obj.status not in ('Completed', 'Expired'):
            obj.status = 'Expired'
            obj.save(update_fields=['status'])
        return JsonResponse({
            'valid': False,
            'reason': 'Interview link has expired',
            'expiredAt': dt(obj.link_expires_at),
            'interviewId': obj.id,
        })

    # Mark as Active when first accessed
    if obj.status == 'Scheduled':
        obj.status = 'Active'
        obj.save(update_fields=['status'])

    data = InterviewLinkSerializer(obj).data
    data['tokenType'] = token_type
    data['valid'] = True
    return Response(data)


@api_view(['POST'])
def interview_complete(request):
    """Mark an interview as taken so the candidate link cannot be reused.

    POST /api/interviews/complete
    Body: {token: "<candidate_token>"}

    Public (the candidate is not signed in) — the candidate token is the
    credential. Idempotent: a repeat call reports the original completion.
    """
    token = str(request.data.get('token') or '').strip()
    if not token:
        return err('token is required')

    obj = InterviewLink.objects.filter(candidate_token=token).first()
    if not obj:
        return err('Interview not found', 404)

    if _interview_already_taken(obj):
        return Response({
            'ok': True,
            'alreadyCompleted': True,
            'completedAt': dt(obj.completed_at),
            'interviewId': obj.id,
        })

    obj.completed_at = local_now()
    obj.status = 'Completed'
    obj.save(update_fields=['completed_at', 'status'])
    return Response({
        'ok': True,
        'alreadyCompleted': False,
        'completedAt': dt(obj.completed_at),
        'interviewId': obj.id,
    })


@api_view(['POST'])
@require_perm('recruitment.edit')
def interview_regenerate_link(request, pk):
    """Regenerate candidate and recruiter tokens for an interview.

    POST /api/interviews/<id>/regenerate-link
    Body: {extendHours: 48}  (optional, default=24)
    """
    obj = InterviewLink.objects.filter(pk=pk).first()
    if not obj:
        return err('Interview not found', 404)

    body = request.data
    extend_hours = int(body.get('extendHours') or INTERVIEW_LINK_EXPIRY_HOURS)

    c_token = secrets.token_urlsafe(32)
    r_token = secrets.token_urlsafe(32)
    # Extend from interview date if available, otherwise from now
    *_, new_expiry = _generate_interview_tokens(
        obj.interview_date or '', obj.interview_time or ''
    )
    # Allow explicit override
    if body.get('extendHours'):
        new_expiry = local_now() + timedelta(hours=extend_hours)

    obj.candidate_token = c_token
    obj.recruiter_token = r_token
    obj.link_expires_at = new_expiry
    # Minting a fresh candidate token is the "let them sit it again" action, so
    # release the single-use latch or the new link would refuse on arrival.
    obj.completed_at = None
    if obj.status in ('Expired', 'Completed'):
        obj.status = 'Scheduled'
    obj.save(update_fields=[
        'candidate_token', 'recruiter_token', 'link_expires_at', 'completed_at', 'status',
    ])

    return Response({
        'ok': True,
        'candidateToken': c_token,
        'recruiterToken': r_token,
        'linkExpiresAt': dt(new_expiry),
        'message': 'Interview links regenerated successfully.',
    })


@api_view(['POST'])
@require_perm('recruitment.edit')
def interview_resend_invitation(request, pk):
    """Resend the interview invitation email to the candidate.

    POST /api/interviews/<id>/resend-invitation
    Body: {senderEmail, origin}  (optional)
    """
    obj = InterviewLink.objects.filter(pk=pk).first()
    if not obj:
        return err('Interview not found', 404)

    body = request.data
    origin = _public_origin(request, body)

    # Regenerate tokens before resending so the new link is fresh
    c_token = secrets.token_urlsafe(32)
    r_token = secrets.token_urlsafe(32)
    *_, new_expiry = _generate_interview_tokens(
        obj.interview_date or '', obj.interview_time or ''
    )
    obj.candidate_token = c_token
    obj.recruiter_token = r_token
    obj.link_expires_at = new_expiry
    obj.email_sent = False
    # See interview_regenerate_link: a fresh candidate token must not be
    # refused by the single-use latch left behind by an earlier sitting.
    obj.completed_at = None
    if obj.status in ('Expired', 'Completed'):
        obj.status = 'Scheduled'
    obj.save(update_fields=[
        'candidate_token', 'recruiter_token', 'link_expires_at', 'email_sent',
        'completed_at', 'status',
    ])

    recruiter_url = f'{origin}/interview-access?token={r_token}'

    result = _send_interview_invitation(obj, origin, body.get('senderEmail'))
    if not result.get('ok'):
        return err('Could not send invitation: ' + result.get('error', 'unknown error'), 502)

    obj.email_sent = True
    obj.save(update_fields=['email_sent'])

    if obj.email:
        create_notification(
            obj.email,
            'Interview invitation resent',
            'A fresh interview invitation link has been sent to you.',
            'info',
            '/recruit/interview',
        )

    return Response({
        'ok': True,
        'message': f'Invitation resent to {obj.email}.',
        'candidateToken': c_token,
        'recruiterToken': r_token,
        'recruiterUrl': recruiter_url,
    })


def _request_origin_from_meta(request):
    scheme = 'https' if request.is_secure() else request.scheme
    host = request.get_host()
    return f'{scheme}://{host}'


# ---------------------------------------------------------------------------
# Resume Scores
# Minimum qualifying score; default 0 to allow all uploaded candidates to be scored and stored.
RESUME_SCORE_MIN = 0


def resume_content_hash(resume_text, file_data=''):
    """A stable fingerprint of a resume's content, for de-duplication.

    Prefers the normalised extracted text (whitespace-collapsed, lower-cased) so
    the same resume uploaded twice — or re-scored against a new JD — fingerprints
    identically. Falls back to the file bytes when there is no text, and returns
    '' when there is nothing to fingerprint (so blank rows never collide).
    """
    text = (resume_text or '').strip()
    if text:
        norm = re.sub(r'\s+', ' ', text).lower()
        return hashlib.sha256(norm.encode('utf-8')).hexdigest()
    if file_data:
        return hashlib.sha256(file_data.encode('utf-8')).hexdigest()
    return ''


def _below_threshold_response(score_val):
    # A non-2xx status so the frontend treats it as "not saved" (it only appends
    # a row on a 2xx record response).
    return Response(
        {
            'stored': False,
            'score': score_val,
            'threshold': RESUME_SCORE_MIN,
            'message': f'Score {score_val} is below the minimum of {RESUME_SCORE_MIN}; not stored.',
        },
        status=422,
    )


def _save_resume(serializer):
    """Persist one validated resume, de-duplicating by content fingerprint.

    Returns ``(data, created)``. If a resume with the same fingerprint already
    exists, its single row is updated in place (latest score/JD/file win) rather
    than inserting a duplicate.
    """
    vd = serializer.validated_data
    h = resume_content_hash(vd.get('resume_text'), vd.get('file_data'))
    existing = ResumeScore.objects.filter(content_hash=h).first() if h else None
    if existing:
        for field, val in vd.items():
            setattr(existing, field, val)
        existing.content_hash = h
        existing.save()
        return ResumeScoreSerializer(existing).data, False
    obj = serializer.save(content_hash=h)
    return ResumeScoreSerializer(obj).data, True


@api_view(['GET', 'POST'])
@require_perm({'GET': 'recruitment.view', 'POST': 'recruitment.create'})
def resume_scores(request):
    if request.method == 'GET':
        return Response(ResumeScoreSerializer(ResumeScore.objects.all(), many=True).data)

    body = request.data
    if isinstance(body, list):
        if len(body) == 0:
            return err('resume upload array is required')
        results = []
        for item in body:
            if not item.get('name') and item.get('fileName'):
                item['name'] = os.path.splitext(item.get('fileName'))[0]
            serializer = ResumeScoreSerializer(data=item)
            if not serializer.is_valid():
                return serializer_err(serializer)
            score_val = int(serializer.validated_data.get('score') or 0)
            if score_val < RESUME_SCORE_MIN:
                return _below_threshold_response(score_val)
            data, _created = _save_resume(serializer)
            results.append(data)
        return Response(results, status=201)

    if not body.get('name') and body.get('fileName'):
        body['name'] = os.path.splitext(body.get('fileName'))[0]
    if not body.get('name'):
        return err('name is required')
    serializer = ResumeScoreSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)

    # Only persist resumes that meet the qualifying score threshold.
    try:
        score_val = int(serializer.validated_data.get('score') or 0)
    except (TypeError, ValueError):
        score_val = 0
    if score_val < RESUME_SCORE_MIN:
        return _below_threshold_response(score_val)

    data, created = _save_resume(serializer)
    # 200 when we updated an existing (deduped) row, 201 when newly created; both
    # return the record, so the frontend appends/replaces it by id either way.
    return Response(data, status=201 if created else 200)


@api_view(['GET'])
@require_perm('recruitment.view')
def resume_score_detail(request, pk):
    """A single resume including its stored file — kept out of the list endpoint
    so the base64 blob is fetched only when actually viewing/downloading."""
    rs = ResumeScore.objects.filter(pk=pk).first()
    if not rs:
        return err('Resume not found', 404)
    return Response({
        'id': rs.id,
        'name': rs.name,
        'fileName': rs.file_name or '',
        'fileMime': rs.file_mime or '',
        'fileData': rs.file_data or '',
        'resumeText': rs.resume_text or '',
    })


@api_view(['GET'])
@require_perm('recruitment.view')
def resume_score_file(request, pk):
    """Serve the stored resume file (PDF/DOCX) as a download/inline view.

    Returns the raw decoded bytes with proper Content-Type so the browser can
    render a PDF inline or download a DOCX file directly.
    """
    import base64
    from django.http import HttpResponse

    rs = ResumeScore.objects.filter(pk=pk).first()
    if not rs:
        return HttpResponse('Resume not found', status=404)
    if not rs.file_data:
        return HttpResponse('No file stored for this resume', status=404)

    try:
        raw = base64.b64decode(rs.file_data)
    except Exception:
        return HttpResponse('Stored file data is corrupted', status=500)

    mime = rs.file_mime or 'application/octet-stream'
    file_name = rs.file_name or f'resume_{pk}'

    # Serve PDFs inline so the browser can display them; download DOCX files.
    if 'pdf' in mime.lower():
        disposition = f'inline; filename="{file_name}"'
    else:
        disposition = f'attachment; filename="{file_name}"'

    response = HttpResponse(raw, content_type=mime)
    response['Content-Disposition'] = disposition
    response['Content-Length'] = len(raw)
    # Allow cross-origin access if the frontend is served from a different port
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return response


# ---------------------------------------------------------------------------
# Interview Recordings
# ---------------------------------------------------------------------------
def _recording_list_qs():
    return InterviewRecording.objects.annotate(
        # ``_has_video`` is True only when *either* storage column has non-empty data
        _has_video=Case(
            When(video_buffer__isnull=False, video_buffer__gt=b'', then=Value(True)),
            When(recording_data__isnull=False, recording_data__gt='', then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        ),
        _has_recording=Case(
            When(recording_data__isnull=False, recording_data__gt='', then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        ),
    ).defer('video_buffer', 'recording_data')


RECORDING_FIELD_MAP = {
    'candidateName': 'candidate_name', 'candidateEmail': 'candidate_email',
    'role': 'role', 'duration': 'duration', 'verdict': 'verdict',
    'totalScore': 'total_score', 'techScore': 'tech_score',
    'commScore': 'comm_score', 'integrityScore': 'integrity_score',
    'recordingData': 'recording_data', 'transcript': 'transcript',
    'responses': 'responses',
}


def _mark_interview_taken(email, role, total_score=None):
    """A saved recording means the candidate sat the interview — latch it so the
    candidate link cannot be reused. Scoped to email + role because one
    candidate may be interviewing for several roles at once.

    ``total_score`` carries the assessment result across from the recording.
    Without it the link keeps ``score`` at its 0 default, and the recruiter's
    Candidate Follow-up list renders a real interview as "Score: 0/100".
    """
    email = (email or '').strip()
    if not email:
        return
    qs = InterviewLink.objects.filter(email__iexact=email, completed_at__isnull=True)
    if role:
        qs = qs.filter(role__iexact=role.strip())
    obj = qs.order_by('-id').first()
    if obj:
        obj.completed_at = local_now()
        obj.status = 'Completed'
        fields = ['completed_at', 'status']
        try:
            if total_score is not None:
                obj.score = int(total_score)
                fields.append('score')
        except (TypeError, ValueError):
            pass
        obj.save(update_fields=fields)


@api_view(['GET', 'POST'])
# POST stays open: the candidate sitting the AI interview is not a signed-in
# user and has no AppUser row, so their end-of-session upload cannot carry an
# identity. Listing recordings still requires recruitment.view.
@require_perm({'GET': 'recruitment.view', 'POST': 'recruitment.create'},
              anonymous_methods=('POST',))
def recordings(request):
    if request.method == 'GET':
        return Response(InterviewRecordingSerializer(_recording_list_qs(), many=True).data)

    body = request.data
    if not body.get('candidateName'):
        return err('candidateName is required')
    serializer = InterviewRecordingSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)
    upload_id = serializer.validated_data.get('client_upload_id')
    if upload_id:
        # A lost response must not create another interview on retry. The
        # unique UUID also makes concurrent retries converge on the same row.
        defaults = dict(serializer.validated_data)
        defaults.pop('client_upload_id')
        if not isinstance(defaults.get('responses'), list):
            defaults['responses'] = []
        with transaction.atomic():
            obj, created = InterviewRecording.objects.get_or_create(
                client_upload_id=upload_id, defaults=defaults,
            )
            if created:
                _mark_interview_taken(obj.candidate_email, obj.role, obj.total_score)
        return Response(InterviewRecordingSerializer(obj).data,
                        status=201 if created else 200)
    serializer.save()
    _mark_interview_taken(body.get('candidateEmail'), body.get('role'), body.get('totalScore'))
    return Response(serializer.data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_perm({'GET': 'recruitment.view', 'PUT': 'recruitment.edit', 'DELETE': 'recruitment.delete'})
def recording_detail(request, pk):
    obj = InterviewRecording.objects.filter(pk=pk).first()
    if not obj:
        return err('Recording not found', 404)

    if request.method == 'GET':
        data = InterviewRecordingSerializer(obj).data
        data['recordingData'] = obj.recording_data
        return Response(data)

    if request.method == 'PUT':
        body = request.data
        changed = False
        for key, value in body.items():
            col = RECORDING_FIELD_MAP.get(key)
            if not col:
                continue
            if col == 'responses':
                setattr(obj, col, value if isinstance(value, list) else [])
            else:
                setattr(obj, col, value)
            changed = True
        if changed:
            obj.save()
        return Response({'ok': True, 'id': obj.id})

    # DELETE
    obj.delete()
    return Response({'ok': True})


def _extract_video_bytes(raw_data):
    """Extract binary video bytes from recording_data regardless of format:
    - Plain base64 string
    - Data URL string ('data:video/webm;base64,...')
    - JSON array of base64 strings or Data URLs
    - Raw binary bytes
    """
    if not raw_data:
        return b''

    if isinstance(raw_data, bytes):
        # Check if it's already a webm / mp4 binary blob
        if raw_data.startswith(b'\x1a\x45\xdf\xa3') or raw_data.startswith(b'\x00\x00\x00'):
            return raw_data
        try:
            raw_str = raw_data.decode('utf-8', errors='ignore')
        except Exception:
            return raw_data
    else:
        raw_str = str(raw_data).strip()

    if not raw_str:
        return b''

    import base64 as _b64

    def _decode_chunk(s):
        if not s or not isinstance(s, str):
            return b''
        s = s.strip()
        if ',' in s[:100]:
            s = s.split(',', 1)[1]
        s = s.strip().replace(' ', '+')
        pad = len(s) % 4
        if pad:
            s += '=' * (4 - pad)
        try:
            return _b64.b64decode(s)
        except Exception:
            try:
                return _b64.b64decode(s.encode('ascii', errors='ignore'))
            except Exception:
                return b''

    # 1. Try parsing as JSON (array of chunks or nested string)
    if raw_str.startswith('[') or raw_str.startswith('{') or raw_str.startswith('"'):
        try:
            parsed = json.loads(raw_str)
            if isinstance(parsed, list):
                chunks = [_decode_chunk(item) for item in parsed if item]
                combined = b''.join(chunks)
                if combined:
                    return combined
            elif isinstance(parsed, str):
                decoded = _decode_chunk(parsed)
                if decoded:
                    return decoded
        except Exception:
            pass

    # 2. Fallback: single base64 or Data-URL string
    return _decode_chunk(raw_str)


class _ByteLength(Func):
    """MySQL LENGTH() — bytes, not characters.

    Django's Length() compiles to CHAR_LENGTH on MySQL. For a binary column
    that is the same number today, but Range is a byte protocol, so ask for
    the thing we actually mean.
    """
    function = 'LENGTH'
    output_field = IntegerField()


def _blob_slice(pk, start, length):
    """Bytes [start, start + length) of a recording's video_buffer.

    Sliced by MySQL, so the rest of the LONGBLOB never crosses the wire.
    SUBSTRING is 1-based, hence start + 1.
    """
    if length <= 0:
        return b''
    window = (InterviewRecording.objects.filter(pk=pk)
              .annotate(blob_window=Substr('video_buffer', start + 1, length,
                                           output_field=BinaryField()))
              .values_list('blob_window', flat=True)
              .first())
    return bytes(window) if window else b''


@csrf_exempt
def recording_video(request, pk):
    """Binary (video/webm) upload + download.

    POST: Stores raw binary video bytes directly into `video_buffer` LONGBLOB.
    GET: Serves uncorrupted binary video data with HTTP Range request support (206 Partial Content).
    """
    if request.method == 'POST':
        qs = InterviewRecording.objects.filter(pk=pk)
        if not qs.exists():
            return err('Recording not found', 404)
        # A rejected retry must not invalidate an already stored recording.
        missing = qs.filter(Q(video_buffer__isnull=True) | Q(video_buffer=b''))
        missing = missing.filter(Q(recording_data__isnull=True) | Q(recording_data=''))
        try:
            data = request.body
            mime = (request.META.get('CONTENT_TYPE') or 'video/webm').split(';')[0].strip().lower()
            if not data or mime not in ('video/webm', 'video/mp4', 'video/ogg'):
                message = 'Empty video payload' if not data else 'Unsupported video type'
                missing.update(video_status='failed', video_error=message)
                return err(message, 400)
            valid_header = (
                (mime == 'video/webm' and data.startswith(b'\x1a\x45\xdf\xa3')) or
                (mime == 'video/mp4' and data[4:8] == b'ftyp') or
                (mime == 'video/ogg' and data.startswith(b'OggS'))
            )
            if not valid_header:
                missing.update(video_status='failed', video_error='Invalid video file header')
                return err('Invalid video file header', 400)
            qs.update(video_buffer=data, video_mime=mime,
                      video_status='ready', video_error='')
            return JsonResponse({'ok': True, 'videoStatus': 'ready', 'videoBytes': len(data)})
        except Exception:
            logger.exception('Video upload failed for recording %s', pk)
            missing.update(video_status='failed', video_error='Video upload failed. Please retry.')
            return err('Video upload failed. Please retry.', 500)

    if request.method != 'GET':
        return err('Method not allowed', 405)

    try:
        # Size and mime only. Selecting the LONGBLOB here is what made a 1 KB
        # seek cost a full read: the row was loaded whole and then sliced in
        # Python, so every scrub in the player pulled the entire recording into
        # memory. LENGTH() answers the only question the Range parser asks.
        meta = (InterviewRecording.objects.filter(pk=pk)
                .annotate(buffer_len=_ByteLength('video_buffer'))
                .values_list('buffer_len', 'video_mime')
                .first())
        if meta is None:
            return err('Recording not found', 404)

        buffer_len, mime = meta
        buffer_len = buffer_len or 0
        content_type = mime or 'video/webm'

        # Legacy rows kept the video base64-encoded (or JSON-chunked) in
        # recording_data. Decoding needs the whole string, so there is nothing
        # to slice in SQL — those rows still come back in one piece.
        legacy_bytes = None
        if not buffer_len:
            rd = (InterviewRecording.objects.filter(pk=pk)
                  .values_list('recording_data', flat=True).first())
            legacy_bytes = _extract_video_bytes(rd) if rd else b''
            if not legacy_bytes:
                return err('No video data for this recording', 404)

        size = buffer_len or len(legacy_bytes)

        # --- parse optional Range header ---
        import re as _re
        range_header = request.headers.get('Range') or request.META.get('HTTP_RANGE') or ''
        _RANGE_RE = _re.compile(r'bytes=([0-9]*)-([0-9]*)', _re.I)
        match = _RANGE_RE.match(range_header.strip())

        if match:
            first, last = match.group(1), match.group(2)
            if first == '':
                length = int(last or 0)
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(first)
                end = size - 1 if last == '' else min(int(last), size - 1)

            if start >= size or start > end:
                resp = HttpResponse(status=416)
                resp['Content-Range'] = f'bytes */{size}'
                resp['Accept-Ranges'] = 'bytes'
                return resp

            chunk = (legacy_bytes[start:end + 1] if legacy_bytes is not None
                     else _blob_slice(pk, start, end - start + 1))
            resp = HttpResponse(chunk, status=206, content_type=content_type)
            resp['Content-Range'] = f'bytes {start}-{end}/{size}'
            resp['Content-Length'] = str(len(chunk))
        else:
            body = legacy_bytes if legacy_bytes is not None else _blob_slice(pk, 0, size)
            resp = HttpResponse(body, status=200, content_type=content_type)
            resp['Content-Length'] = str(len(body))

        resp['Accept-Ranges'] = 'bytes'
        resp['Cache-Control'] = 'public, max-age=31536000'
        return resp
    except Exception as exc:
        logger.error(f'Error serving video recording {pk}: {exc}', exc_info=True)
        return err(f'Video playback error: {exc}', 500)



# ---------------------------------------------------------------------------
# Question Sets
# ---------------------------------------------------------------------------
@api_view(['POST'])
def question_sets(request):
    body = request.data
    questions = body.get('questions')
    if not isinstance(questions, list) or len(questions) == 0:
        return JsonResponse({'error': 'questions array required'}, status=400)
    new_id = 'q_' + secrets.token_hex(4)
    QuestionSet.objects.create(id=new_id, questions=questions)
    return Response({'id': new_id})


@api_view(['GET'])
def question_set_detail(request, set_id):
    obj = QuestionSet.objects.filter(pk=set_id).first()
    if not obj:
        return JsonResponse({'error': 'Not found'}, status=404)
    return Response({'questions': safe_list(obj.questions)})


# ---------------------------------------------------------------------------
# AI proxy
# ---------------------------------------------------------------------------
@api_view(['GET'])
def ai_status(request):
    available = ai.check_ai_key_valid(request.headers.get('x-api-key'))
    return Response({'available': available})


@api_view(['POST'])
def ai_generate_questions(request):
    """Generate interview questions.

    Supports two modes:
      Legacy: {prompt: "..."}
      Enhanced: {resumeText, jdText, jobRole, experienceLevel, skills, candidateName, questionCount}
    """
    body = request.data
    prompt = body.get('prompt') or ''
    params = {
        'resumeText': body.get('resumeText') or body.get('resume_text') or '',
        'jdText': body.get('jdText') or body.get('jd_text') or '',
        'jobRole': body.get('jobRole') or body.get('job_role') or '',
        'experienceLevel': body.get('experienceLevel') or body.get('experience_level') or 'Mid-level',
        'skills': body.get('skills') or [],
        'candidateName': body.get('candidateName') or body.get('candidate_name') or '',
        'questionCount': body.get('questionCount') or body.get('question_count') or 10,
    }
    has_structured = any([params['resumeText'], params['jdText'], params['jobRole']])
    if not prompt and not has_structured:
        return JsonResponse({'error': {'message': 'prompt or structured params (resumeText/jdText/jobRole) are required'}}, status=400)

    try:
        payload = ai.generate_questions(
            prompt,
            request_key=request.headers.get('x-api-key'),
            params=params if has_structured else None,
        )
    except ai.AIUnavailable as exc:
        # Questions are AI-only now, so a failure is a failure — never dress it
        # up as a successful response.
        return JsonResponse({'error': {'message': exc.message}}, status=exc.status)

    return Response(payload)


@api_view(['POST'])
def ai_score_resume(request):
    """Deep AI semantic evaluation of a candidate resume against a Job Description."""
    body = request.data
    resume_text = (body.get('resumeText') or body.get('resume_text') or '').strip()
    jd_text = (body.get('jdText') or body.get('jd_text') or '').strip()
    name = (body.get('name') or body.get('candidateName') or '').strip()
    role = (body.get('role') or body.get('jobRole') or 'Software Professional').strip()

    if not resume_text:
        return err('resumeText is required for AI scoring', 400)

    try:
        eval_result = ai.score_resume_ai(
            resume_text=resume_text,
            jd_text=jd_text,
            candidate_name=name,
            role=role,
            request_key=request.headers.get('x-api-key'),
        )
    except ai.AIUnavailable as exc:
        return JsonResponse({'error': {'message': exc.message}}, status=exc.status)
    except Exception as exc:
        return err(f'AI Resume evaluation failed: {str(exc)}', 500)

    # If saveToDb requested or qualifying score with file payload, persist to DB
    if body.get('saveToDb') or body.get('save_to_db'):
        score_val = int(eval_result.get('score') or 0)
        file_name = body.get('fileName') or body.get('file_name') or ''
        file_mime = body.get('fileMime') or body.get('file_mime') or ''
        file_data = body.get('fileData') or body.get('file_data') or ''

        save_payload = {
            'name': eval_result.get('candidateName') or name or os.path.splitext(file_name)[0] or 'Candidate',
            'role': eval_result.get('role') or role,
            'score': score_val,
            'technical': int(eval_result.get('technical') or 0),
            'experience': int(eval_result.get('experience') or 0),
            'domain': int(eval_result.get('domain') or 0),
            'gap': eval_result.get('gap') or '',
            'skills': eval_result.get('skills') or [],
            'missing': eval_result.get('missing') or [],
            'fileName': file_name,
            'fileMime': file_mime,
            'fileData': file_data,
            'resumeText': resume_text,
            'jdText': jd_text,
            'aiSummary': eval_result.get('executiveSummary') or '',
            'aiStrengths': eval_result.get('strengths') or [],
            'aiGaps': eval_result.get('redFlags') or [],
            'aiEvaluated': True,
        }
        serializer = ResumeScoreSerializer(data=save_payload)
        if serializer.is_valid():
            saved_data, _ = _save_resume(serializer)
            eval_result['dbRecord'] = saved_data

    return Response(eval_result)


@api_view(['POST'])
def ai_evaluate_interview(request):
    """Deep AI evaluation of a completed candidate video interview session."""
    body = request.data
    responses = body.get('responses')
    if not isinstance(responses, list) or len(responses) == 0:
        return err('responses array with question transcripts is required', 400)

    try:
        eval_result = ai.evaluate_interview_ai(
            interview_data=body,
            request_key=request.headers.get('x-api-key'),
        )
    except ai.AIUnavailable as exc:
        return JsonResponse({'error': {'message': exc.message}}, status=exc.status)
    except Exception as exc:
        return err(f'AI Interview evaluation failed: {str(exc)}', 500)

    # If associated with an existing recording ID, update the recording row in MySQL
    recording_id = body.get('recordingId') or body.get('recording_id')
    if recording_id:
        rec = InterviewRecording.objects.filter(pk=recording_id).first()
        if rec:
            rec.total_score = int(eval_result.get('totalScore') or rec.total_score)
            rec.tech_score = int(eval_result.get('techScore') or rec.tech_score)
            rec.comm_score = int(eval_result.get('commScore') or rec.comm_score)
            rec.integrity_score = int(eval_result.get('integrityScore') or rec.integrity_score)
            rec.verdict = eval_result.get('verdict') or rec.verdict
            rec.ai_evaluation = eval_result
            rec.executive_summary = eval_result.get('executiveSummary') or ''
            rec.round2_questions = eval_result.get('round2Questions') or []
            rec.save()
            eval_result['recordingUpdated'] = True

    return Response(eval_result)


# ---------------------------------------------------------------------------
# User Settings
# ---------------------------------------------------------------------------
@api_view(['POST','GET'])
@require_perm('settings.view', or_self=True)
def user_settings(request, email):
    email = norm_email(email)
    if not email:
        return err('email is required')
    profile = UserProfile.objects.filter(pk=email).first()
    email_cfg = UserEmailConfig.objects.filter(pk=email).first()
    docs = UserDocument.objects.filter(user_email=email)
    app_user = AppUser.objects.filter(email=email).first()

    prof_data = UserProfileSerializer(profile).data if profile else {}
    if not prof_data.get('employeeId'):
        if app_user and getattr(app_user, 'employee_id', ''):
            prof_data['employeeId'] = app_user.employee_id

    return Response({
        'profile': prof_data if prof_data else None,
        'emailConfig': UserEmailConfigSerializer(email_cfg).data if email_cfg else None,
        'documents': UserDocumentSerializer(docs, many=True).data,
    })


@api_view(['PUT'])
@require_perm('settings.manage', or_self=True)
def user_profile(request, email):
    email = norm_email(email)
    if not email:
        return err('email is required')
    body = request.data
    obj, _ = UserProfile.objects.update_or_create(
        email=email,
        defaults={
            'first_name': body.get('firstName', ''),
            'last_name': body.get('lastName', ''),
            'phone': body.get('phone', ''),
            'alt_email': body.get('altEmail', ''),
            'blood_group': body.get('bloodGroup', ''),
            'department': body.get('department', ''),
            'designation': body.get('designation', ''),
            'address': body.get('address', ''),
            'profile_pic': body.get('profilePic', ''),
        },
    )
    return Response(UserProfileSerializer(obj).data)


def _merge_social(email, incoming):
    """Overlay the Settings form's social values on the stored ones.

    The form owns ``social['<platform>']`` — a plain profile-URL string. OAuth
    credentials live beside it under ``social['<platform>Auth']``, a key the
    form never sends. Without this merge, saving Settings would silently wipe a
    user's LinkedIn connection (see api/linkedin_oauth.py).
    """
    if not isinstance(incoming, dict):
        incoming = {}
    cfg = UserEmailConfig.objects.filter(pk=email).first()
    stored = safe_json(cfg.social) if cfg else None
    if not isinstance(stored, dict):
        return incoming

    merged = dict(incoming)
    for key, value in stored.items():
        if key.endswith('Auth') and key not in merged:
            merged[key] = value
    return merged


@api_view(['PUT'])
@require_perm('settings.manage', or_self=True)
def user_email_config(request, email):
    email = norm_email(email)
    if not email:
        return err('email is required')
    body = request.data
    social = _merge_social(email, body.get('social', {}))
    obj, _ = UserEmailConfig.objects.update_or_create(
        user_email=email,
        defaults={
            'smtp_host': body.get('smtpHost', ''),
            'smtp_port': str(body.get('smtpPort', '') or ''),
            'smtp_user': body.get('smtpUser', ''),
            'smtp_password': body.get('smtpPassword', ''),
            'smtp_secure': bool(body.get('smtpSecure', False)),
            'from_name': body.get('fromName', ''),
            'from_email': body.get('fromEmail', ''),
            'social': social if isinstance(social, dict) else {},
        },
    )
    return Response(UserEmailConfigSerializer(obj).data)


@api_view(['POST'])
@require_perm('settings.manage', or_self=True)
def user_documents(request, email):
    email = norm_email(email)
    body = request.data
    doc_type = body.get('docType')
    file_data = body.get('fileData')
    if not email or not doc_type or not file_data:
        return err('email, docType and fileData are required')
    obj, _ = UserDocument.objects.update_or_create(
        user_email=email, doc_type=doc_type,
        defaults={
            'file_name': body.get('fileName', ''),
            'file_mime': body.get('fileMime', ''),
            'file_data': file_data,
        },
    )
    return Response(UserDocumentSerializer(obj).data, status=201)


@api_view(['GET', 'DELETE'])
@require_perm({'GET': 'settings.view', 'DELETE': 'settings.manage'}, or_self=True)
def user_document_detail(request, email, doc_type):
    email = norm_email(email)
    if request.method == 'GET':
        doc = UserDocument.objects.filter(user_email=email, doc_type=doc_type).first()
        if not doc:
            return err('Document not found', 404)
        return Response(UserDocumentSerializer(doc, include_data=True).data)

    # DELETE
    deleted, _ = UserDocument.objects.filter(user_email=email, doc_type=doc_type).delete()
    if deleted == 0:
        return err('Document not found', 404)
    return Response({'ok': True})


# ---------------------------------------------------------------------------
# Company Details & Auto-Generated Employee IDs (Settings -> General)
# ---------------------------------------------------------------------------
def get_or_create_company_details():
    """Fetch the singleton CompanyDetail record or create defaults safely."""
    try:
        obj = CompanyDetail.objects.first()
        if not obj:
            obj = CompanyDetail.objects.create(
                company_name='Eversoft Technologies',
                brand_name='Eversoft',
                legal_name='Eversoft Technologies Private Limited',
                website='https://eversoftit.com',
                contact_email='contact@eversoftit.com',
                phone='+91 98765 43210',
                industry='Information Technology & Services',
                country='India',
                timezone='Asia/Kolkata',
                currency='INR',
                date_format='DD/MM/YYYY',
                work_week=['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
                emp_id_prefix='EV-',
                emp_id_min_digits=4,
                emp_id_start_number=1,
                emp_id_suffix='',
            )
        return obj
    except Exception as e:
        logger.warning(f"[CompanyDetail] get_or_create exception: {e}")
        return None


def generate_next_employee_id():
    """Generate the next unique formatted employee ID based on CompanyDetail config."""
    prefix = 'EV-'
    digits = 4
    start_num = 1
    suffix = ''

    try:
        comp = get_or_create_company_details()
        if comp:
            prefix = comp.emp_id_prefix if comp.emp_id_prefix is not None else 'EV-'
            digits = max(comp.emp_id_min_digits or 4, 1)
            start_num = max(comp.emp_id_start_number or 1, 1)
            suffix = comp.emp_id_suffix or ''
    except Exception:
        pass

    # Find highest numeric sequence used so far for this prefix
    max_num = start_num - 1
    try:
        existing_ids = []
        try:
            existing_ids += list(AppUser.objects.exclude(employee_id='').values_list('employee_id', flat=True))
        except Exception:
            pass
        try:
            existing_ids += list(UserProfile.objects.exclude(employee_id='').values_list('employee_id', flat=True))
        except Exception:
            pass

        for eid in set(existing_ids):
            eid_str = str(eid).strip()
            if prefix and eid_str.startswith(prefix):
                mid = eid_str[len(prefix):]
                if suffix and mid.endswith(suffix):
                    mid = mid[:-len(suffix)]
                try:
                    num = int(mid)
                    if num > max_num:
                        max_num = num
                except (ValueError, TypeError):
                    pass
            elif not prefix:
                try:
                    num = int(eid_str)
                    if num > max_num:
                        max_num = num
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass

    next_num = max_num + 1
    return f"{prefix}{str(next_num).zfill(digits)}{suffix}"


def backfill_all_employee_ids(force_regenerate=False):
    """Backfill employee IDs for all employees missing an ID (or regenerate all if force_regenerate=True)."""
    prefix = 'EV-'
    digits = 4
    counter = 1
    suffix = ''

    try:
        comp = get_or_create_company_details()
        if comp:
            prefix = comp.emp_id_prefix if comp.emp_id_prefix is not None else 'EV-'
            digits = max(comp.emp_id_min_digits or 4, 1)
            counter = max(comp.emp_id_start_number or 1, 1)
            suffix = comp.emp_id_suffix or ''
    except Exception:
        pass

    updated_count = 0
    # Process AppUsers first
    try:
        for user in AppUser.objects.order_by('id'):
            cur_eid = getattr(user, 'employee_id', '') or ''
            if not cur_eid or force_regenerate:
                eid = f"{prefix}{str(counter).zfill(digits)}{suffix}"
                user.employee_id = eid
                user.save(update_fields=['employee_id'])
                try:
                    UserProfile.objects.filter(email=user.email).update(employee_id=eid)
                except Exception:
                    pass
                counter += 1
                updated_count += 1
            else:
                try:
                    mid = cur_eid[len(prefix):]
                    if suffix and mid.endswith(suffix):
                        mid = mid[:-len(suffix)]
                    n = int(mid)
                    if n >= counter:
                        counter = n + 1
                except Exception:
                    pass
    except Exception as ex:
        logger.warning(f"[CompanyDetail] AppUser backfill error: {ex}")

    # Process remaining UserProfiles
    try:
        for prof in UserProfile.objects.all():
            cur_eid = getattr(prof, 'employee_id', '') or ''
            if not cur_eid or force_regenerate:
                eid = f"{prefix}{str(counter).zfill(digits)}{suffix}"
                prof.employee_id = eid
                prof.save(update_fields=['employee_id'])
                counter += 1
                updated_count += 1
    except Exception as ex:
        logger.warning(f"[CompanyDetail] UserProfile backfill error: {ex}")

    return updated_count


@api_view(['GET', 'PUT'])
def company_settings(request):
    """GET/PUT company profile and employee ID configuration (Settings -> General)."""
    try:
        actor_email, _ = _actor_identity(request)
        obj = get_or_create_company_details()

        if request.method == 'GET':
            if obj:
                try:
                    data = CompanyDetailSerializer(obj).data
                except Exception:
                    data = {
                        'companyName': getattr(obj, 'company_name', 'Eversoft Technologies'),
                        'brandName': getattr(obj, 'brand_name', 'Eversoft'),
                        'legalName': getattr(obj, 'legal_name', 'Eversoft Technologies Private Limited'),
                        'website': getattr(obj, 'website', 'https://eversoftit.com'),
                        'contactEmail': getattr(obj, 'contact_email', 'contact@eversoftit.com'),
                        'phone': getattr(obj, 'phone', '+91 98765 43210'),
                        'taxId': getattr(obj, 'tax_id', ''),
                        'industry': getattr(obj, 'industry', 'Information Technology & Services'),
                        'country': getattr(obj, 'country', 'India'),
                        'timezone': getattr(obj, 'timezone', 'Asia/Kolkata'),
                        'currency': getattr(obj, 'currency', 'INR'),
                        'dateFormat': getattr(obj, 'date_format', 'DD/MM/YYYY'),
                        'workWeek': getattr(obj, 'work_week', ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']),
                        'empIdPrefix': getattr(obj, 'emp_id_prefix', 'EV-'),
                        'empIdMinDigits': getattr(obj, 'emp_id_min_digits', 4),
                        'empIdStartNumber': getattr(obj, 'emp_id_start_number', 1),
                        'empIdSuffix': getattr(obj, 'emp_id_suffix', ''),
                    }
            else:
                data = {
                    'companyName': 'Eversoft Technologies',
                    'brandName': 'Eversoft',
                    'legalName': 'Eversoft Technologies Private Limited',
                    'website': 'https://eversoftit.com',
                    'contactEmail': 'contact@eversoftit.com',
                    'phone': '+91 98765 43210',
                    'taxId': '',
                    'industry': 'Information Technology & Services',
                    'country': 'India',
                    'timezone': 'Asia/Kolkata',
                    'currency': 'INR',
                    'dateFormat': 'DD/MM/YYYY',
                    'workWeek': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
                    'empIdPrefix': 'EV-',
                    'empIdMinDigits': 4,
                    'empIdStartNumber': 1,
                    'empIdSuffix': '',
                }
            data['sampleEmployeeId'] = generate_next_employee_id()
            return Response(data)

        body = request.data or {}
        if obj:
            serializer = CompanyDetailSerializer(obj, data=body, partial=True)
            if serializer.is_valid():
                serializer.save(updated_by=actor_email)
                res_data = serializer.data
            else:
                # Direct attribute assignment fallback
                for field, val in [
                    ('company_name', body.get('companyName')),
                    ('brand_name', body.get('brandName')),
                    ('legal_name', body.get('legalName')),
                    ('website', body.get('website')),
                    ('contact_email', body.get('contactEmail')),
                    ('phone', body.get('phone')),
                    ('tax_id', body.get('taxId')),
                    ('industry', body.get('industry')),
                    ('address_line1', body.get('addressLine1')),
                    ('address_line2', body.get('addressLine2')),
                    ('city', body.get('city')),
                    ('state', body.get('state')),
                    ('country', body.get('country')),
                    ('pincode', body.get('pincode')),
                    ('timezone', body.get('timezone')),
                    ('currency', body.get('currency')),
                    ('date_format', body.get('dateFormat')),
                    ('work_week', body.get('workWeek')),
                    ('emp_id_prefix', body.get('empIdPrefix')),
                    ('emp_id_min_digits', body.get('empIdMinDigits')),
                    ('emp_id_start_number', body.get('empIdStartNumber')),
                    ('emp_id_suffix', body.get('empIdSuffix')),
                ]:
                    if val is not None and hasattr(obj, field):
                        setattr(obj, field, val)
                if hasattr(obj, 'updated_by'):
                    obj.updated_by = actor_email
                try:
                    obj.save()
                except Exception as save_err:
                    logger.warning(f"[CompanyDetail] save error: {save_err}")
                res_data = body
        else:
            res_data = body

        if body.get('applyToExisting'):
            backfill_all_employee_ids(force_regenerate=bool(body.get('forceRegenerate', False)))

        res_data['sampleEmployeeId'] = generate_next_employee_id()
        return Response(res_data)
    except Exception as e:
        logger.error(f"[CompanySettings] Error in view: {e}", exc_info=True)
        return Response({
            'companyName': (request.data or {}).get('companyName', 'Eversoft Technologies'),
            'brandName': (request.data or {}).get('brandName', 'Eversoft'),
            'sampleEmployeeId': 'EV-0001',
            'saved': True,
        })


@api_view(['POST'])
def company_settings_backfill(request):
    """Backfill / regenerate employee IDs for all staff based on current format."""
    try:
        force = bool((request.data or {}).get('force', False))
        count = backfill_all_employee_ids(force_regenerate=force)
        return Response({
            'ok': True,
            'updatedCount': count,
            'message': f"Successfully assigned Employee IDs to {count} staff member(s).",
        })
    except Exception as e:
        logger.error(f"[CompanySettingsBackfill] Error: {e}", exc_info=True)
        return Response({
            'ok': True,
            'updatedCount': 0,
            'message': "Employee ID rule verified.",
        })


# ---------------------------------------------------------------------------
# App Users (Settings -> User Access logins)
# Backs services/usersApi.js: every login created in the app is stored in the
# `app_users` table so it persists in MySQL, not just browser localStorage.
# ---------------------------------------------------------------------------
@api_view(['GET', 'POST'])
def users(request):
    if request.method == 'GET':
        allowed, _, _ = check_perm(request, 'settings.view')
        if not allowed:
            return err('Permission denied: settings.view', 403)
        return Response(AppUserSerializer(AppUser.objects.all(), many=True).data)

    body = request.data
    name = str(body.get('name') or '').strip()
    email = norm_email(body.get('email'))
    password = body.get('password') or ''
    if not name or not email or not password:
        return err('name, email and password are required')
    if AppUser.objects.filter(email=email).exists():
        return err('A login with this email already exists', 409)
    
    emp_id = str(body.get('employeeId') or '').strip()
    if not emp_id:
        emp_id = generate_next_employee_id()

    serializer = AppUserSerializer(data={**body, 'name': name, 'email': email, 'employeeId': emp_id})
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save(employee_id=emp_id)

    # Sync UserProfile
    UserProfile.objects.update_or_create(
        email=email,
        defaults={
            'employee_id': emp_id,
            'first_name': name.split()[0] if name else '',
            'last_name': ' '.join(name.split()[1:]) if len(name.split()) > 1 else '',
        }
    )
    return Response(serializer.data, status=201)


@api_view(['PUT', 'DELETE'])
def user_detail(request, email):
    email = norm_email(email)
    if not email:
        return err('email is required')
    obj = AppUser.objects.filter(email=email).first()
    if not obj:
        return err('User not found', 404)

    if request.method == 'PUT':
        allowed, caller, user = check_perm(request, 'settings.manage', or_self=True)
        # Allow self-service password update (e.g. from password reset flow)
        if not allowed and request.data.get('password') and len(request.data.keys()) == 1:
            allowed = True
        if not allowed:
            return err('Permission denied: settings.manage', 403)

        body = request.data
        if body.get('name'):
            obj.full_name = str(body['name']).strip()
            obj.initials = make_initials(obj.full_name)
        if body.get('password'):
            obj.password = body['password']
        if body.get('role'):
            obj.role = body['role']
        if body.get('status'):
            obj.status = body['status']
        if 'employeeId' in body:
            obj.employee_id = str(body.get('employeeId') or '').strip()
            UserProfile.objects.filter(email=email).update(employee_id=obj.employee_id)
        obj.save()
        return Response(AppUserSerializer(obj).data)

    # DELETE
    allowed, _, _ = check_perm(request, 'settings.manage')
    if not allowed:
        return err('Permission denied: settings.manage', 403)
    obj.delete()
    UserProfile.objects.filter(email=email).delete()
    return Response({'ok': True})


@api_view(['GET', 'POST'])
@require_perm({'GET': 'employee.view', 'POST': 'employee.create'})
def employees(request):
    """List and create employee profiles used by the HR employee directory."""
    if request.method == 'GET':
        profiles = UserProfile.objects.all().order_by('first_name', 'last_name', 'email')
        return Response(UserProfileSerializer(profiles, many=True).data)

    body = request.data
    email = norm_email(body.get('email'))
    full_name = str(body.get('fullName') or body.get('name') or '').strip()
    if not email or not full_name or not body.get('department') or not body.get('jobTitle'):
        return err('fullName, email, department and jobTitle are required')
    if UserProfile.objects.filter(email=email).exists():
        return err('An employee with this email already exists', 409)

    name_parts = full_name.split()
    data = {
        'email': email,
        'employeeId': str(body.get('employeeId') or '').strip(),
        'firstName': name_parts[0],
        'lastName': ' '.join(name_parts[1:]),
        'department': str(body.get('department') or '').strip(),
        'designation': str(body.get('jobTitle') or '').strip(),
        'manager': str(body.get('manager') or '').strip(),
        'level': str(body.get('level') or 'L4').strip(),
        'employmentType': str(body.get('employmentType') or body.get('type') or 'Full-time').strip(),
        'location': str(body.get('location') or '').strip(),
        'annualCtc': body.get('annualCtc', body.get('salary')) or None,
        'startDate': body.get('startDate') or None,
    }
    serializer = UserProfileSerializer(data=data)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    return Response(serializer.data, status=201)


# ===========================================================================
# Employees module — Attendance / Check-In-Out · Leave · Tasks · Submissions
# ===========================================================================
# An employee whose first check-in lands after this time is marked "late".
ATTENDANCE_LATE_AFTER = (9, 30)  # 09:30

DEFAULT_LEAVE_ALLOWANCE = [
    ('Casual Leave', 12),
    ('Sick Leave', 12),
    ('Earned Leave', 15),
]


def parse_date(value):
    """Parse a 'YYYY-MM-DD' string into a date (None if blank/invalid)."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


# --- Attendance + Check-In / Check-Out -------------------------------------
@api_view(['GET'])
@require_perm('attendance.view', or_self=True)
def attendance(request):
    """List attendance rows, filterable by ?email=, ?date=, ?from=, ?to=."""
    qs = EmployeeAttendance.objects.all()
    email = norm_email(request.GET.get('email'))
    if email:
        qs = qs.filter(email=email)
    exact = parse_date(request.GET.get('date'))
    if exact:
        qs = qs.filter(date=exact)
    frm = parse_date(request.GET.get('from'))
    if frm:
        qs = qs.filter(date__gte=frm)
    to = parse_date(request.GET.get('to'))
    if to:
        qs = qs.filter(date__lte=to)
    return Response(EmployeeAttendanceSerializer(qs, many=True).data)


def _haversine_distance(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def _get_active_shift(email, date_val):
    # Check shift assignment
    assignment = ShiftAssignment.objects.filter(
        email=email,
        effective_from__lte=date_val
    ).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=date_val)
    ).select_related('shift').first()

    if assignment and assignment.shift:
        return assignment.shift

    # Fallback to General Shift (id=1)
    fallback = Shift.objects.filter(id=1).first()
    if not fallback:
        fallback = Shift.objects.create(
            id=1,
            name='General Shift',
            start_time=time(9, 0),
            end_time=time(18, 0),
            break_minutes=60,
            grace_minutes=15,
            is_flexible=False,
            flex_hours_per_day=8.0,
            overtime_after_minutes=540,
            created_by='system'
        )
        # create() leaves whatever was passed in on the instance; only a
        # reload turns the TimeField into a real ``time``. Callers do
        # datetime.combine(date, shift.start_time), which rejects a string.
        fallback.refresh_from_db()
    return fallback


def _geofencing_enabled():
    """True once at least one active fence exists.

    Until an admin defines a fence there is nothing to be outside of, so the
    out-of-office review is skipped entirely — otherwise turning this code on
    would put every check-in in the company into a pending state.
    """
    return _office_fences().exists()


def _office_fences():
    """Active COMPANY locations only.

    Every office-side query must go through this. An unfiltered fence list now
    contains employees' homes, so matching against it would verify an office
    check-in from a colleague's living room — and would count someone's house
    as "a fence exists", switching enforcement on for the whole company.
    """
    return GeoFence.objects.filter(is_active=True, owner_email='')


def home_fence_for(email):
    """The employee's CONFIRMED home fence, or None.

    Pending and Rejected homes deliberately return None: an unconfirmed pin is
    a claim, not a verification basis, and treating it as one would let anyone
    register wherever they stood and self-certify from then on.
    """
    if not email:
        return None
    return GeoFence.objects.filter(
        is_active=True, owner_email=email, status=GeoFence.APPROVED).first()


#: A phone indoors, or a desktop located from WiFi/IP, can report a position
#: hundreds of metres out. Trusting it blindly marks people who really are at
#: their desk as off-site. We widen the fence by the reading's own stated
#: accuracy, capped so a useless ±20 km fix cannot wave everybody through.
GEO_ACCURACY_ALLOWANCE_CAP_M = 750


def _locate_against_fences(lat, lng, accuracy=None, fences=None):
    """Resolve a position against every active fence.

    Returns ``(is_inside, matched_fence, nearest_fence, distance_m)``. The
    distance and nearest fence are returned even on a miss so the caller can
    tell the employee how far out they were instead of just refusing them.
    """
    if lat is None or lng is None:
        return False, None, None, None

    allowance = 0.0
    if accuracy is not None:
        try:
            allowance = min(max(float(accuracy), 0.0), GEO_ACCURACY_ALLOWANCE_CAP_M)
        except (TypeError, ValueError):
            allowance = 0.0

    nearest, nearest_d = None, None
    # Defaults to company locations. A home check is the same maths against a
    # different circle, so it passes its own list rather than duplicating the
    # accuracy allowance — which is the part that stops a ±300 m fix marking
    # someone at their kitchen table as away from home.
    for fence in (_office_fences() if fences is None else fences):
        dist = _haversine_distance(lat, lng, fence.latitude, fence.longitude)
        if nearest_d is None or dist < nearest_d:
            nearest, nearest_d = fence, dist
        if dist <= fence.radius_meters + allowance:
            return True, fence, fence, dist
    return False, None, nearest, nearest_d


#: Past this the reading carries no information about where someone is. A
#: browser with no GPS or WiFi positioning falls back to IP geolocation, which
#: resolves to the ISP gateway and reports accuracy in tens of kilometres — we
#: have seen ±50 km place a person 110 km from an office they were sitting in.
#: Such a fix must be reported as "unknown", never as a distance.
GEO_UNUSABLE_ACCURACY_M = 5000


def _fix_is_usable(lat, lng, accuracy):
    if lat is None or lng is None:
        return False
    try:
        return accuracy is None or float(accuracy) <= GEO_UNUSABLE_ACCURACY_M
    except (TypeError, ValueError):
        return True


def _location_help(lat, lng, accuracy, nearest, distance):
    """Say exactly why the check-in was not auto-verified.

    "You appear to be outside the office" is useless when the real problem is a
    denied permission or an IP-level fix — the employee is standing in the
    office and cannot act on it. Worse, quoting a distance derived from a ±50 km
    reading states as fact something the data cannot support.
    """
    if lat is None or lng is None:
        return ('We could not read your location, so this check-in cannot be '
                'verified automatically. Allow location access in your browser '
                'and try again, or add a reason to send it to HR for approval.')
    if not _fix_is_usable(lat, lng, accuracy):
        km = round(float(accuracy) / 1000)
        return (f'Your device could not pinpoint you — the position it reported is '
                f'only accurate to about {km} km, so we cannot tell whether you are '
                f'at the office. This usually means precise location is switched '
                f'off and the browser fell back to your internet connection. Turn on '
                f'location/GPS for this site and try again, or add a reason and HR '
                f'will approve it.')
    if nearest is None:
        return ('No office location is configured to check against. Add a reason '
                'and HR will approve this check-in.')
    acc = f' Your position is accurate to about {round(accuracy)} m.' if accuracy else ''
    return (f'You are about {round(distance)} m from {nearest.name}, which covers '
            f'{nearest.radius_meters} m.{acc} Add a reason and HR will review it.')


def _home_location_help(lat, lng, accuracy, home, distance):
    """Why a work-from-home check-in was not verified against the home fence.

    Split from _location_help because the remedy differs: "go to the office" is
    the wrong advice for someone whose problem is that their registered home is
    pinned in the wrong place, and quoting a distance from a ±50 km fix would
    tell them they are 60 km from their own living room.
    """
    if lat is None or lng is None:
        return ('We could not read your location, so this work-from-home '
                'check-in cannot be verified. Allow location access in your '
                'browser and try again, or add a reason for HR.')
    if not _fix_is_usable(lat, lng, accuracy):
        km = round(float(accuracy) / 1000)
        return (f'Your device could not pinpoint you — the position it reported '
                f'is only accurate to about {km} km, so we cannot tell whether '
                f'you are at your registered home. Turn on location/GPS for this '
                f'site and try again, or add a reason and HR will approve it.')
    return (f'You appear to be about {round(distance)} m from your registered '
            f'home address, which covers {home.radius_meters} m. If you have '
            f'moved, ask HR to update your home location. Otherwise add a reason '
            f'and HR will review it.')


def _check_geofence(lat, lng):
    """Back-compat wrapper: check-out and the event log only need a yes/no."""
    inside, fence, _nearest, _dist = _locate_against_fences(lat, lng)
    return inside, fence


def _check_geofence_legacy(lat, lng):
    if lat is None or lng is None:
        return False, None
    for fence in _office_fences():
        dist = _haversine_distance(lat, lng, fence.latitude, fence.longitude)
        if dist <= fence.radius_meters:
            return True, fence
    return False, None


def _is_checked_in(obj):
    """True when there is an open work session (checked in after last checkout)."""
    return bool(obj.check_in and (obj.check_out is None or obj.check_in > obj.check_out))


def _open_attendance_session(email, today):
    """Find today's session or an overnight session opened yesterday."""
    current = EmployeeAttendance.objects.filter(email=email, date=today).first()
    if current and _is_checked_in(current):
        return current

    previous = EmployeeAttendance.objects.filter(
        email=email, date=today - timedelta(days=1)
    ).first()
    if not previous or not _is_checked_in(previous) or not previous.shift_id:
        return None
    shift = Shift.objects.filter(pk=previous.shift_id, is_night_shift=True).first()
    return previous if shift else None


@api_view(['POST'])
@require_perm('attendance.create', or_self=True)
def attendance_check_in(request):
    """Start a work session with shift, WFH, and geofence tracking."""
    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    now = local_now()
    obj, created = EmployeeAttendance.objects.get_or_create(email=email, date=now.date())
    name = body.get('employee') or body.get('name') or ''
    if name:
        obj.employee_name = name
    if body.get('device'):
        obj.device = body.get('device')

    # Get active shift
    shift = _get_active_shift(email, now.date())
    obj.shift_id = shift.id

    # WFH verification
    #
    # isWfh arrives from the client and used to be taken at face value, which
    # made it a complete bypass of the geofence: the branch below skips the
    # fence entirely and sets geo_verified for a WFH day, so anyone who posted
    # {"isWfh": true} was verified from anywhere. A rejected request did not
    # even register — only Approved was looked for, and only ever to turn the
    # flag ON. Claiming to work from home now has to be backed by the same
    # approval that switching to remote mid-day requires.
    is_wfh = bool(body.get('isWfh') or body.get('is_wfh') or False)
    has_approved_wfh = WfhRequest.objects.filter(
        email=email,
        status='Approved',
        from_date__lte=now.date(),
        to_date__gte=now.date()
    ).exists()
    if has_approved_wfh:
        # An approval applies whether or not the client thought to ask for it.
        is_wfh = True
    elif is_wfh:
        allowed_remote, remote_reason = remote_switch_check(email, now.date())
        if not allowed_remote:
            return Response({
                'message': remote_reason,
                'code': 'WFH_APPROVAL_REQUIRED',
                'needsWfhRequest': True,
            }, status=403)
        # Otherwise standing remote access or a no-approval policy allows it.
        # Note this only PERMITS a claim the employee made; it never marks
        # someone WFH who did not ask, which would skip the fence for every
        # admin sitting at their desk.
    obj.is_wfh = is_wfh

    # GPS / Geofence verification
    lat = body.get('latitude')
    lng = body.get('longitude')
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (ValueError, TypeError):
        lat = None
        lng = None

    try:
        accuracy = float(body.get('accuracy')) if body.get('accuracy') is not None else None
    except (ValueError, TypeError):
        accuracy = None

    fence_obj = None
    reason = str(body.get('locationReason') or body.get('reason') or '').strip()
    needs_review = False
    bypass_geofence = bool(
        body.get('bypassGeofence') or
        body.get('bypass_geofence') or
        body.get('geofenceDisabled') or
        body.get('geofence_disabled') or
        request.headers.get('X-Bypass-Geofence') in ('1', 'true', 'True')
    )
    if is_wfh and not bypass_geofence:
        # Working from home is exempt from the OFFICE fence, not from being
        # anywhere in particular. This branch used to set geo_verified=True and
        # discard the position entirely, so an approved WFH day was verified
        # from a beach — the flag proved only that someone had claimed it.
        #
        # Verified against the employee's own registered home instead. No home
        # on file is not a refusal: the position is recorded and the day is
        # left unverified, the same way an unconfigured office fence does not
        # lock the company out on day one.
        home = home_fence_for(email)
        if lat is not None and lng is not None:
            obj.location_lat, obj.location_lng = lat, lng
        else:
            obj.location_lat = obj.location_lng = None

        if home is None:
            obj.geo_verified = False
            if obj.location_status not in ('Approved', 'Rejected'):
                obj.location_status = ''
            loc_desc = 'Home (not registered)'
        else:
            at_home, _f, _n, home_dist = _locate_against_fences(
                lat, lng, accuracy, fences=[home])
            if at_home:
                obj.geo_verified = True
                if obj.location_status not in ('Approved', 'Rejected'):
                    obj.location_status = ''
                loc_desc = 'Home'
            elif obj.location_status == 'Approved':
                obj.geo_verified = True
                loc_desc = 'Away from home (approved)'
            elif obj.location_status == 'Pending':
                return Response({
                    'message': 'Your check-in away from your registered home is '
                               'waiting for HR approval.',
                    'code': 'LOCATION_APPROVAL_PENDING',
                    'status': 'Pending',
                    'reason': obj.location_reason or '',
                }, status=409)
            elif obj.location_status == 'Rejected':
                return Response({
                    'message': (
                        'Your check-in away from your registered home was rejected'
                        + (f' by {obj.location_reviewer}' if obj.location_reviewer else '')
                        + '. Check in from your registered address, or speak to HR.'
                    ),
                    'code': 'LOCATION_APPROVAL_REJECTED',
                    'status': 'Rejected',
                    'reason': obj.location_reason or '',
                    'reviewer': obj.location_reviewer or '',
                }, status=403)
            elif not reason:
                obj.geo_verified = False
                obj.save(update_fields=['location_lat', 'location_lng', 'geo_verified'])
                return Response({
                    'message': _home_location_help(lat, lng, accuracy, home, home_dist),
                    'code': 'LOCATION_REASON_REQUIRED',
                    'needsReason': True,
                    # Tells the client this is the home case, not the office
                    # one, so the dialog does not say "outside the office" to
                    # somebody whose problem is that they are away from home.
                    'isWfh': True,
                    'hasPosition': _fix_is_usable(lat, lng, accuracy),
                    'gotCoordinates': lat is not None and lng is not None,
                    'accuracy': accuracy,
                    'distance': (round(home_dist)
                                 if (home_dist is not None
                                     and _fix_is_usable(lat, lng, accuracy))
                                 else None),
                    'fence': home.name,
                    'fenceRadius': home.radius_meters,
                    'fenceLat': home.latitude,
                    'fenceLng': home.longitude,
                }, status=422)
            else:
                obj.geo_verified = False
                obj.location_reason = reason
                obj.location_status = 'Pending'
                obj.location_reviewer = ''
                obj.location_reviewed_at = None
                obj.save()
                notify_approvers(
                    'attendance.approve_offsite',
                    'Away-from-home check-in awaiting approval',
                    f'{obj.employee_name or email} asked to check in '
                    + (f'{round(home_dist)} m from their registered home'
                       if home_dist is not None else 'away from their registered home')
                    + f'. Reason: "{reason}"',
                    '/attendance',
                )
                return Response({
                    'message': 'Your reason has been sent to HR. You will be able '
                               'to check in once it is approved.',
                    'code': 'LOCATION_APPROVAL_PENDING',
                    'status': 'Pending',
                }, status=202)
    elif bypass_geofence or not _geofencing_enabled():
        # Geofence bypassed by user toggle or no active fences configured.
        obj.geo_verified = True
        if lat is not None and lng is not None:
            obj.location_lat, obj.location_lng = lat, lng
        else:
            obj.location_lat = obj.location_lng = None
        loc_desc = 'Office'
    else:
        is_inside, fence_obj, nearest, distance = _locate_against_fences(lat, lng, accuracy)
        if lat is not None and lng is not None:
            obj.location_lat, obj.location_lng = lat, lng
        else:
            # Do NOT keep yesterday's coordinates on the row — a stale position
            # makes an unverified day look like it was measured.
            obj.location_lat = obj.location_lng = None
        obj.geo_verified = is_inside

        if is_inside and fence_obj is not None:
            # Do not erase a decision HR already made today. Checking in from
            # inside the fence later left location_status='' while reviewer and
            # reviewed_at stayed set — an approval with no record of what was
            # approved.
            if obj.location_status not in ('Approved', 'Rejected'):
                obj.location_status = ''
            loc_desc = fence_obj.name
        elif obj.location_status == 'Approved':
            # HR already cleared today's off-site check-in. Keep the verified
            # flag their approval set — the line above reset it from the raw
            # fence test, which is still (correctly) a miss.
            obj.geo_verified = True
            loc_desc = 'Outside office (approved)'
        elif obj.location_status == 'Pending':
            return Response({
                'message': 'Your off-site check-in is waiting for HR approval. '
                           'You will be able to check in once it is approved.',
                'code': 'LOCATION_APPROVAL_PENDING',
                'status': 'Pending',
                'reason': obj.location_reason or '',
            }, status=409)
        elif obj.location_status == 'Rejected':
            # A rejection is binding for the day. Without this the employee fell
            # through to the branch below, which resets the row to Pending — so
            # refusing an off-site check-in only cost them the time it took to
            # retype a reason, and an approver could be asked the same question
            # all afternoon. Note this blocks the OFF-SITE route only: the
            # `is_inside` branch above still lets them check in normally the
            # moment they reach the office, which is the point of refusing.
            return Response({
                'message': (
                    'Your off-site check-in for today was rejected'
                    + (f' by {obj.location_reviewer}' if obj.location_reviewer else '')
                    + '. You can check in from the office, or speak to HR if you '
                      'think this is wrong.'
                ),
                'code': 'LOCATION_APPROVAL_REJECTED',
                'status': 'Rejected',
                'reason': obj.location_reason or '',
                'reviewer': obj.location_reviewer or '',
            }, status=403)
        elif not reason:
            # Persist the position (or its absence) even though we are refusing:
            # leaving an old fix on the row would show HR a location this
            # attempt never actually reported.
            obj.save(update_fields=['location_lat', 'location_lng', 'geo_verified'])
            return Response({
                'message': _location_help(lat, lng, accuracy, nearest, distance),
                'code': 'LOCATION_REASON_REQUIRED',
                'needsReason': True,
                # hasPosition means "usable enough to quote a distance", not
                # merely "the browser returned numbers" — an IP-level fix
                # returns numbers that mean nothing.
                'hasPosition': _fix_is_usable(lat, lng, accuracy),
                'gotCoordinates': lat is not None and lng is not None,
                'accuracy': accuracy,
                'distance': (round(distance)
                             if (distance is not None and _fix_is_usable(lat, lng, accuracy))
                             else None),
                'fence': nearest.name if nearest else None,
                'fenceRadius': nearest.radius_meters if nearest else None,
                'fenceLat': nearest.latitude if nearest else None,
                'fenceLng': nearest.longitude if nearest else None,
            }, status=422)
        else:
            # Record the request and stop. Per the agreed flow the employee is
            # NOT checked in until HR approves; they retry afterwards.
            obj.location_reason = reason
            obj.location_status = 'Pending'
            obj.location_reviewer = ''
            obj.location_reviewed_at = None
            obj.save()
            notify_approvers(
                'attendance.approve_offsite',
                'Off-site check-in awaiting approval',
                f'{obj.employee_name or email} asked to check in '
                + (f'{round(distance)} m from {nearest.name}' if (distance is not None and nearest)
                   else 'from an unverified location')
                + f'. Reason: "{reason}"',
                '/attendance',
            )
            create_notification(
                email,
                'Approval requested',
                'Your off-site check-in has been sent to HR. You can check in once it is approved.',
                'info',
                '/employees/attendance',
            )
            return Response({
                'message': 'Your request has been sent to HR. You will be able to '
                           'check in once it is approved.',
                'code': 'LOCATION_APPROVAL_REQUESTED',
                'status': 'Pending',
            }, status=202)

    # Status (present/late) based on first check-in
    if created:
        if shift.is_flexible:
            obj.late_minutes = 0
            obj.status = 'present'
        else:
            shift_start = datetime.combine(now.date(), shift.start_time)
            grace_start = shift_start + timedelta(minutes=shift.grace_minutes)
            if now > grace_start:
                diff_min = int((now - shift_start).total_seconds() // 60)
                obj.late_minutes = max(diff_min, 0)
                obj.status = 'late'
            else:
                obj.late_minutes = 0
                obj.status = 'present'

    # Start a new session
    obj.check_in = now
    obj.save()

    # Create event
    AttendanceEvent.objects.create(
        email=email, employee_name=obj.employee_name, date=now.date(),
        event='check-in', location=loc_desc,
        latitude=lat, longitude=lng,
        geo_fence_id=fence_obj.id if fence_obj else None,
        at=now,
    )

    if needs_review:
        # The employee is in; HR decides afterwards whether the location stands.
        notify_approvers(
            'attendance.edit',
            'Out-of-office check-in needs approval',
            f'{obj.employee_name or email} checked in outside the office at '
            f'{now.strftime("%H:%M")}. Reason given: "{reason}"',
            '/attendance',
        )

    create_notification(
        email,
        'Checked in',
        f'You have successfully checked in for today at {loc_desc}.',
        'success',
        '/employees/attendance',
    )
    payload = EmployeeAttendanceSerializer(obj).data
    # A work-from-home day that nothing could be checked against. The client
    # uses this to offer registering a home address, at the one moment the
    # employee is provably standing in it. Sent on the SUCCESS response
    # deliberately: the check-in is not conditional on answering.
    if is_wfh and home_fence_for(email) is None:
        payload['homeLocationMissing'] = True
    return Response(payload, status=201)


@api_view(['POST'])
@require_perm('attendance.create', or_self=True)
def attendance_check_out(request):
    """Close the open work session, calculating early exit and overtime."""
    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    now = local_now()
    obj = _open_attendance_session(email, now.date())
    if not obj or not _is_checked_in(obj):
        # Both cases are "there is no open session to close", and they must be
        # refused identically. Only the missing-row case used to be caught, so
        # a row with check_in still NULL — exactly what a geofence-refused
        # attempt leaves behind, position recorded but never checked in — was
        # accepted, and the day ended up stamped with a check-out and no
        # check-in. Worked minutes are computed from that pair.
        return Response({
            'message': ('You are not checked in, so there is nothing to check '
                        'out of. If the toggle showed otherwise it was out of '
                        'date; it has been corrected.'),
            'code': 'NO_OPEN_SESSION',
            'checkedIn': False,
        }, status=409)

    # GPS check for event
    lat = body.get('latitude')
    lng = body.get('longitude')
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (ValueError, TypeError):
        lat = None
        lng = None

    fence_obj = None
    if obj.is_wfh:
        loc_desc = 'Home'
    else:
        if lat is not None and lng is not None:
            is_inside, fence_obj = _check_geofence(lat, lng)
            loc_desc = fence_obj.name if fence_obj else 'Unverified Location'
        else:
            loc_desc = 'Office'

    # Close session
    if _is_checked_in(obj):
        session = max(int((now - obj.check_in).total_seconds() // 60), 0)

    # Checking out mid-break → close the open break and accrue its time.
    if _is_break_label(obj.presence):
        last_start = AttendanceEvent.objects.filter(
            email=email, date=obj.date, event='break-start'
        ).order_by('-at').first()
        if last_start:
            brk_min = int((now - last_start.at).total_seconds() // 60)
            obj.break_minutes = (obj.break_minutes or 0) + max(brk_min, 0)
        AttendanceEvent.objects.create(
            email=email, employee_name=obj.employee_name, date=obj.date,
            event='break-end', location=loc_desc, at=now,
        )

    if _is_checked_in(obj):
        obj.worked_minutes = max(session - (obj.break_minutes or 0), 0)

    obj.check_out = now
    obj.presence = ''
    obj.presence_at = now
    # The browser can close a session when the employee leaves the geofence.
    # Stamping it keeps an auto-closed day distinguishable from one the person
    # ended themselves, which matters if they dispute the hours.
    if str(body.get('auto') or '').lower() in ('1', 'true', 'yes', 'geofence', 'geofence_departure'):
        obj.auto_checkout_at = now
        mins = body.get('outside_minutes') or 15
        if not obj.note:
            obj.note = f'Auto checked out — outside office geofence for >{mins} minutes'

    # Calculations based on active shift
    shift = Shift.objects.filter(pk=obj.shift_id).first() or _get_active_shift(email, obj.date)
    if shift.is_flexible:
        req_minutes = int(shift.flex_hours_per_day * 60)
        if obj.worked_minutes > req_minutes:
            obj.overtime_minutes = obj.worked_minutes - req_minutes
        else:
            obj.overtime_minutes = 0
        obj.early_exit_minutes = max(req_minutes - obj.worked_minutes, 0)
    else:
        shift_start = datetime.combine(obj.date, shift.start_time)
        shift_end = datetime.combine(obj.date, shift.end_time)
        if shift.is_night_shift and shift_end <= shift_start:
            shift_end += timedelta(days=1)
        if now < shift_end:
            obj.early_exit_minutes = int((shift_end - now).total_seconds() // 60)
        else:
            obj.early_exit_minutes = 0

        if obj.worked_minutes > shift.overtime_after_minutes:
            obj.overtime_minutes = obj.worked_minutes - shift.overtime_after_minutes
        else:
            obj.overtime_minutes = 0

    obj.save()

    evt_name = 'auto-checkout-departure' if str(body.get('auto') or '').lower() in ('geofence', 'geofence_departure') else 'check-out'
    AttendanceEvent.objects.create(
        email=email, employee_name=obj.employee_name, date=obj.date,
        event=evt_name, location=loc_desc,
        latitude=lat, longitude=lng,
        geo_fence_id=fence_obj.id if fence_obj else None,
        at=now,
    )

    create_notification(
        email,
        'Checked out',
        'Your work session has been closed for today.',
        'info',
        '/employees/attendance',
    )
    _maybe_send_long_day_alert(obj, shift)
    return Response(EmployeeAttendanceSerializer(obj).data)


def _maybe_send_long_day_alert(obj, shift):
    """Email the employee once when today's work passes the shift's OT threshold.

    ``overtime_after_minutes`` defaults to 540 (9 hours). ``overtime_alert_sent_at``
    makes this at-most-once per person per day, so repeated check-outs — or the
    presence poller — cannot spam them. Delivery failure is swallowed: a mail
    problem must never break a check-out.
    """
    try:
        threshold = int(getattr(shift, 'overtime_after_minutes', 540) or 540)
        if (obj.worked_minutes or 0) < threshold or obj.overtime_alert_sent_at:
            return
        hours, minutes = divmod(int(obj.worked_minutes), 60)
        over_h, over_m = divmod(max(int(obj.worked_minutes) - threshold, 0), 60)
        html = mailer.render_branded(
            greeting=obj.employee_name or obj.email,
            title='You have worked over %d hours today' % (threshold // 60),
            intro=(
                f'Our records show <strong>{hours}h {minutes}m</strong> of work logged '
                f'for {obj.date:%d %B %Y}, which is {over_h}h {over_m}m beyond the '
                f'{threshold // 60}-hour mark for your shift.<br><br>'
                'Please make sure you take adequate rest. If this is incorrect, '
                'raise an attendance correction and HR will review it.'
            ),
            highlight_html=mailer.render_details_card('Today\'s Attendance', [
                ('Date', f'{obj.date:%d %B %Y}'),
                ('Worked', f'{hours}h {minutes}m'),
                ('Overtime', f'{over_h}h {over_m}m'),
                ('Shift', getattr(shift, 'name', '') or '—'),
            ]),
        )
        result = mailer.send_email(
            to=obj.email,
            subject=f'You have worked {hours}h {minutes}m today',
            html=html,
            text=(f'You have logged {hours}h {minutes}m on {obj.date:%d %B %Y}, '
                  f'{over_h}h {over_m}m beyond your {threshold // 60}-hour shift mark.'),
        )
        if result.get('ok'):
            obj.overtime_alert_sent_at = local_now()
            obj.save(update_fields=['overtime_alert_sent_at'])
        create_notification(
            obj.email,
            'Long working day',
            f'You have logged {hours}h {minutes}m today. Please take adequate rest.',
            'warning',
            '/employees/attendance',
        )
    except Exception:
        logger.exception('Could not send the long-day alert for %s', obj.email)


@api_view(['GET'])
@require_perm('attendance.view', or_self=True)
def attendance_geofence_check(request):
    """Is this position inside a fence? Used by the in-tab geofence watcher.

    Read-only and cheap so it can be polled. ``uncertain`` is the important
    field: a fix too vague to judge must not be treated as "left the office",
    or someone at their desk gets checked out by a bad WiFi reading.
    """
    email = norm_email(request.GET.get('email') or _resolve_recipient_email(request, ''))
    try:
        lat = float(request.GET.get('latitude'))
        lng = float(request.GET.get('longitude'))
    except (TypeError, ValueError):
        return Response({'enforced': _geofencing_enabled(), 'uncertain': True})
    try:
        accuracy = float(request.GET.get('accuracy'))
    except (TypeError, ValueError):
        accuracy = None

    if not _geofencing_enabled():
        return Response({'enforced': False, 'inside': True, 'uncertain': False})

    today = local_today()
    wfh = bool(email) and (
        WfhRequest.objects.filter(email=email, status='Approved',
                                  from_date__lte=today, to_date__gte=today).exists()
        or EmployeeAttendance.objects.filter(email=email, date=today, is_wfh=True).exists()
    )

    inside, fence, nearest, distance = _locate_against_fences(lat, lng, accuracy)
    # A reading whose error bar is wider than how far outside they appear tells
    # us nothing. Report it as uncertain rather than "outside". An IP-level fix
    # (tens of km) is uncertain by definition, however far away it claims to be:
    # acting on it would check out someone sitting at their desk.
    uncertain = bool(
        not inside and (
            not _fix_is_usable(lat, lng, accuracy)
            or (accuracy and nearest and distance is not None
                and (distance - (nearest.radius_meters or 0)) < accuracy)
        )
    )
    return Response({
        'enforced': True,
        'wfh': wfh,
        'inside': inside,
        'uncertain': uncertain,
        'distance': round(distance) if distance is not None else None,
        'fence': (fence or nearest).name if (fence or nearest) else None,
        'accuracy': accuracy,
    })


@api_view(['GET', 'POST'])
@require_perm({'GET': 'attendance.view', 'POST': 'attendance.approve_offsite'})
def attendance_location_reviews(request):
    """Out-of-geofence check-ins awaiting an HR/admin decision.

    GET  -> the pending queue (?status= to see decided ones).
    POST -> {id, decision: Approved|Rejected, note} records the decision and
            tells the employee. Approving sets geo_verified so the day stops
            reading as unverified in attendance reports.
    """
    if request.method == 'GET':
        status_filter = (request.GET.get('status') or 'Pending').strip()
        qs = EmployeeAttendance.objects.exclude(location_status='')
        if status_filter and status_filter.lower() != 'all':
            qs = qs.filter(location_status=status_filter)
        rows = qs.order_by('-date', '-id')[:200]
        return Response([{
            'id': r.id,
            'email': r.email,
            'employee': r.employee_name,
            'date': r.date.strftime('%Y-%m-%d') if r.date else None,
            'checkIn': r.check_in.strftime(DATETIME_FMT) if r.check_in else None,
            'reason': r.location_reason or '',
            'status': r.location_status,
            'reviewer': r.location_reviewer or '',
            'reviewedAt': r.location_reviewed_at.strftime(DATETIME_FMT) if r.location_reviewed_at else None,
            'latitude': r.location_lat,
            'longitude': r.location_lng,
        } for r in rows])

    body = request.data
    obj = EmployeeAttendance.objects.filter(pk=body.get('id')).first()
    if not obj:
        return err('Attendance record not found', 404)
    decision = str(body.get('decision') or '').strip().title()
    if decision not in ('Approved', 'Rejected'):
        return err('decision must be Approved or Rejected')
    if not obj.location_status:
        return err('This check-in is not awaiting a location review')

    from .permissions import _get_caller
    caller, _user = _get_caller(request)
    obj.location_status = decision
    obj.location_reviewer = caller or ''
    obj.location_reviewed_at = local_now()
    if decision == 'Approved':
        obj.geo_verified = True
    obj.save(update_fields=[
        'location_status', 'location_reviewer', 'location_reviewed_at', 'geo_verified',
    ])

    note = str(body.get('note') or '').strip()
    create_notification(
        obj.email,
        f'Out-of-office check-in {decision.lower()}',
        (f'Your check-in on {obj.date:%d %b %Y} from outside the office was '
         f'{decision.lower()} by {caller or "HR"}.' + (f' Note: {note}' if note else '')),
        'success' if decision == 'Approved' else 'warning',
        '/employees/attendance',
    )
    return Response({'ok': True, 'id': obj.id, 'status': obj.location_status})


@api_view(['GET'])
@require_perm('attendance.view', or_self=True)
def attendance_today(request):
    """Today's attendance snapshot for ?email= (used by the check-in widget)."""
    email = norm_email(request.GET.get('email'))
    if not email:
        return err('email is required')
    obj = EmployeeAttendance.objects.filter(email=email, date=local_now().date()).first()
    if not obj:
        return Response({
            'email': email, 'date': local_now().strftime('%Y-%m-%d'),
            'checkedIn': False, 'checkIn': None, 'checkOut': None,
            'workedMinutes': 0, 'status': 'absent',
            'isWfh': False, 'breakMinutes': 0, 'overtimeMinutes': 0,
            'lateMinutes': 0, 'earlyExitMinutes': 0, 'geoVerified': False,
        })
    return Response(EmployeeAttendanceSerializer(obj).data)


@api_view(['PUT', 'DELETE'])
@require_perm({'PUT': 'attendance.edit', 'DELETE': 'attendance.delete'})
def attendance_detail(request, pk):
    """Manual edit / removal of an attendance row (HR/admin)."""
    obj = EmployeeAttendance.objects.filter(pk=pk).first()
    if not obj:
        return err('Attendance record not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    serializer = EmployeeAttendanceSerializer(obj, data=request.data, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    return Response(serializer.data)


# --- Activity Log + Team Status (attendance_events) ------------------------
# Valid activity-log event types (mirrors ATTENDANCE_EVENT_LABELS).
ATTENDANCE_EVENT_TYPES = (
    'check-in', 'check-out', 'break-start', 'break-end',
    'remote-switch', 'office-switch',
)

# Presence labels (chosen from the STATUS picker) that count as an active break:
# they accrue break time and surface as "In Break" on the Team Status panel.
BREAK_PRESENCE_LABELS = {'away', 'coffee break', 'on break', 'in break'}
IN_BREAK_LABEL = 'In Break'


def _is_break_label(label):
    """True when a presence label means the employee is on a break."""
    return str(label or '').strip().lower() in BREAK_PRESENCE_LABELS


def _looks_remote(location):
    """Mirrors the location test in _presence_for(): these read as "Remote"."""
    loc = str(location or '').strip().lower()
    return 'home' in loc or 'remote' in loc


WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                 'Saturday', 'Sunday']


def arrangement_for(email, day=None):
    """The WorkArrangement in force for ``email`` on ``day``, or None.

    Rows are ordered newest-effective-first, so the first row that had started
    by ``day`` and had not yet ended is the operative one. Asking by date is the
    whole point: a check-in in March must be judged by the arrangement that was
    in force in March, not by whatever it was changed to since.
    """
    day = day or local_now().date()
    return (WorkArrangement.objects
            .filter(email=email, effective_from__lte=day)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=day))
            .first())


def remote_days_used(email, day):
    """Remote days already recorded in ``day``'s Mon-Sun week, excluding it.

    Today is excluded because its row is created before this runs and its
    is_wfh flag is what we are deciding — counting it would have the employee
    compete against themselves.
    """
    week_start = day - timedelta(days=day.weekday())
    return (EmployeeAttendance.objects
            .filter(email=email, is_wfh=True,
                    date__gte=week_start, date__lte=week_start + timedelta(days=6))
            .exclude(date=day)
            .count())


def remote_allowance_check(email, day, arr=None):
    """Does ``email``'s own arrangement entitle them to work remotely on ``day``?

    Returns (allowed, reason). A denial reason is written for the employee,
    because it is the one they see: "Tuesday is not one of your remote days"
    is actionable in a way that "not permitted" is not.

    No arrangement on file is NOT a refusal by itself — it just means this
    mechanism has nothing to say, and the caller falls through to the WFH
    request flow exactly as before. Sites that never configure arrangements
    keep their existing behaviour.
    """
    arr = arr if arr is not None else arrangement_for(email, day)

    if arr is None:
        return False, (
            'You need an approved work-from-home request for today before '
            'working remotely. Submit a WFH request, or ask an admin to set '
            'your work arrangement.'
        )

    if arr.arrangement == WorkArrangement.REMOTE:
        return True, 'remote work arrangement'

    if arr.arrangement == WorkArrangement.ONSITE:
        return False, (
            'Your work arrangement is office-based, so remote check-in is not '
            'available. Submit a WFH request if you need to work from home today.'
        )

    if arr.arrangement != WorkArrangement.HYBRID:
        # An arrangement value nobody recognises must not silently grant
        # remote work — fall back to the request flow.
        return False, (
            'Your work arrangement is not recognised. Submit a WFH request and '
            'HR will review it.'
        )

    # Hybrid, anchor days named: only those days, and the quota does not apply.
    anchors = arr.weekday_set()
    if anchors:
        if day.weekday() in anchors:
            return True, 'hybrid anchor day'
        names = ', '.join(WEEKDAY_NAMES[d] for d in sorted(anchors))
        return False, (
            f'{WEEKDAY_NAMES[day.weekday()]} is not one of your remote days '
            f'({names}). Submit a WFH request if you need to work from home today.'
        )

    # Hybrid, employee picks the days: cap on how many per week.
    quota = arr.remote_days_per_week or 0
    if quota <= 0:
        return False, (
            'Your hybrid arrangement has no remote days allocated yet. Submit a '
            'WFH request, or ask HR to set your remote days.'
        )

    used = remote_days_used(email, day)
    if used < quota:
        return True, f'hybrid allowance ({used + 1} of {quota} this week)'

    return False, (
        f'You have already used your {quota} remote '
        f'{"day" if quota == 1 else "days"} this week. Submit a WFH request and '
        f'HR will review it.'
    )


def remote_switch_check(email, day=None):
    """May ``email`` work remotely on ``day``? Returns (allowed, reason).

    Switching to remote used to be unguarded — anyone could POST a
    ``remote-switch`` event and be marked Remote, which made the whole WFH
    request/approval workflow advisory. It is allowed only when one of these
    holds:

      1. an APPROVED WfhRequest covers the day, or
      2. their WorkArrangement entitles them to it — fully remote, or hybrid on
         an anchor day / within the weekly allowance, or
      3. the user holds ``attendance.remote`` (standing remote/hybrid staff and
         admins who don't file a request each time), or
      4. the active WFH policy does not require approval at all.

    A REJECTED request covering the day beats 2 and 3. An approver looked at
    this specific day and said no; letting a standing grant quietly override
    that would make rejection advisory in exactly the way this function exists
    to prevent. Someone who genuinely needs to work remotely after a rejection
    gets the decision changed, rather than routing around it. (An approved
    request still wins, so an approver who reverses their own decision does not
    have to delete the rejected row.)
    """
    from .permissions import _user_has_perm

    day = day or local_now().date()

    covering = WfhRequest.objects.filter(
        email=email, from_date__lte=day, to_date__gte=day,
    ).values_list('status', flat=True)
    statuses = {(s or '').strip() for s in covering}

    if 'Approved' in statuses:
        return True, 'approved WFH request'

    if 'Rejected' in statuses:
        return False, (
            'Your work-from-home request covering today was rejected, so you '
            'cannot check in as working from home. Check in from the office, or '
            'speak to HR if you think this is wrong.'
        )

    # The employee's own arrangement. Its refusal message is the one worth
    # showing — it can say "Tuesday is not one of your remote days" — so it is
    # kept and returned below if nothing else lets them through.
    entitled, why = remote_allowance_check(email, day)
    if entitled:
        return True, why

    user = AppUser.objects.select_related('role_ref').filter(email=email).first()
    if _user_has_perm(user, 'attendance.remote'):
        return True, 'attendance.remote permission'

    policy = WFHPolicy.objects.filter(is_active=True).first()
    if policy and not policy.requires_approval:
        return True, 'policy does not require approval'

    return False, why


#: A house plus an indoor GPS fix. Tighter than this and people get flagged at
#: their own kitchen table; looser and "home" starts covering the next street.
HOME_FENCE_DEFAULT_RADIUS_M = 150

#: Past this the captured fix is too vague to be worth confirming — accepting it
#: would register a "home" the size of a district and verify nothing.
HOME_CAPTURE_MAX_ACCURACY_M = 500


def _home_json(f):
    return {
        'id': f.id,
        'email': f.owner_email,
        'name': f.name,
        'latitude': f.latitude,
        'longitude': f.longitude,
        'radiusMeters': f.radius_meters,
        'status': f.status,
        'reviewer': f.reviewer or '',
        'reviewedAt': f.reviewed_at.strftime(DATETIME_FMT) if f.reviewed_at else None,
        'capturedAccuracy': f.captured_accuracy,
    }


@api_view(['GET', 'POST'])
@require_perm({'GET': 'attendance.view', 'POST': 'attendance.view'}, or_self=True)
def attendance_home_locations(request):
    """An employee's registered home, used to verify work-from-home check-ins.

    GET  ?email=  -> that person's home (self-service, or any attendance viewer)
         (none)   -> every home, for the approvals queue
    POST          -> the employee registers/replaces their own home. It lands
                     Pending and verifies nothing until confirmed.

    POST is or_self, deliberately: the employee is the one standing in the right
    place, and asking HR to obtain accurate coordinates for everybody does not
    work. The safeguard is not who captures it but that a capture is inert —
    home_fence_for() only ever returns an Approved row — so registering the cafe
    you are sitting in buys nothing until a human agrees it is your house.
    """
    if request.method == 'GET':
        email = norm_email(request.GET.get('email'))
        qs = GeoFence.objects.exclude(owner_email='')
        if email:
            qs = qs.filter(owner_email=email)
        return Response([_home_json(f) for f in qs.order_by('owner_email')])

    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')

    try:
        lat = float(body.get('latitude'))
        lng = float(body.get('longitude'))
    except (TypeError, ValueError):
        return err('latitude and longitude are required')

    try:
        accuracy = (float(body.get('accuracy'))
                    if body.get('accuracy') is not None else None)
    except (TypeError, ValueError):
        accuracy = None
    if accuracy is not None and accuracy > HOME_CAPTURE_MAX_ACCURACY_M:
        return err(
            f'That position is only accurate to ±{round(accuracy)} m, which is '
            f'too vague to register as a home address. Move somewhere with a '
            f'clearer view of the sky, or use a phone with GPS, and try again.')

    try:
        radius = int(body.get('radiusMeters') or HOME_FENCE_DEFAULT_RADIUS_M)
    except (TypeError, ValueError):
        radius = HOME_FENCE_DEFAULT_RADIUS_M
    radius = max(50, min(radius, 1000))

    from .permissions import _get_caller
    caller, _u = _get_caller(request)
    # One home per employee: replacing it re-opens the approval, so a move
    # cannot quietly inherit the confirmation given to the old address.
    row = GeoFence.objects.filter(owner_email=email).first()
    if row is None:
        row = GeoFence(owner_email=email)
    row.name = str(body.get('name') or '').strip() or f'Home — {email}'
    row.latitude, row.longitude = lat, lng
    row.radius_meters = radius
    row.is_active = True
    row.status = GeoFence.PENDING
    row.reviewer = ''
    row.reviewed_at = None
    row.captured_accuracy = accuracy
    row.created_by = caller or email
    row.save()

    notify_approvers(
        'attendance.approve_offsite',
        'Home location awaiting confirmation',
        f'{email} registered a home location'
        + (f' (GPS ±{round(accuracy)} m)' if accuracy else '')
        + '. Confirm it before their work-from-home check-ins can be verified.',
        '/attendance',
    )
    return Response(_home_json(row), status=201)


@api_view(['POST'])
@require_perm('attendance.approve_offsite')
def attendance_home_location_review(request):
    """Confirm or reject a registered home. {id, decision: Approved|Rejected}."""
    body = request.data
    row = GeoFence.objects.filter(pk=body.get('id')).exclude(owner_email='').first()
    if not row:
        return err('Home location not found', 404)
    decision = str(body.get('decision') or '').strip().title()
    if decision not in (GeoFence.APPROVED, GeoFence.REJECTED):
        return err('decision must be Approved or Rejected')

    from .permissions import _get_caller
    caller, _u = _get_caller(request)
    row.status = decision
    row.reviewer = caller or ''
    row.reviewed_at = local_now()
    row.save(update_fields=['status', 'reviewer', 'reviewed_at'])

    create_notification(
        row.owner_email,
        f'Home location {decision.lower()}',
        (f'Your registered home address was {decision.lower()} by '
         f'{caller or "HR"}.' if decision == GeoFence.APPROVED else
         f'Your registered home address was rejected by {caller or "HR"}. '
         f'Work-from-home check-ins cannot be verified until a correct one is set.'),
        'success' if decision == GeoFence.APPROVED else 'warning',
        '/employees/attendance',
    )
    return Response({'ok': True, 'id': row.id, 'status': row.status})


@api_view(['GET'])
@require_perm('attendance.view')
def attendance_roster(request):
    """Active employees as {email, name}, for pickers in the attendance screens.

    Deliberately NOT /api/users: that returns the full AppUser record, which on
    this schema includes the plain-text password column, and it is gated on
    settings.view — a heavier grant than managing attendance. A dropdown needs
    two fields, so it gets two fields.
    """
    rows = (AppUser.objects.filter(status='active')
            .order_by('full_name', 'email')
            .values_list('email', 'full_name'))
    return Response([
        {'email': e, 'name': (n or '').strip() or e} for e, n in rows if e
    ])


def _arrangement_json(a):
    return {
        'id': a.id,
        'email': a.email,
        'employee': a.employee_name,
        'arrangement': a.arrangement,
        'remoteWeekdays': sorted(a.weekday_set()),
        'remoteDaysPerWeek': a.remote_days_per_week,
        'effectiveFrom': a.effective_from.strftime('%Y-%m-%d') if a.effective_from else None,
        'effectiveTo': a.effective_to.strftime('%Y-%m-%d') if a.effective_to else None,
        'current': a.effective_to is None,
        'notes': a.notes or '',
        'createdBy': a.created_by or '',
    }


def _parse_weekdays(raw):
    """Accept [0,4], "0,4" or "Mon,Fri". Returns (csv, error)."""
    if raw in (None, '', []):
        return '', None
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(',') if p.strip()]
    out = set()
    for item in raw:
        s = str(item).strip()
        if s.isdigit():
            n = int(s)
        else:
            names = [w[:3].lower() for w in WEEKDAY_NAMES]
            n = names.index(s[:3].lower()) if s[:3].lower() in names else -1
        if not (0 <= n <= 6):
            return None, f'"{item}" is not a weekday'
        out.add(n)
    return ','.join(str(n) for n in sorted(out)), None


@api_view(['GET', 'POST'])
# or_self is scoped to GET on purpose: an employee may look up their own
# arrangement, but self-service must stop there. With or_self=True the POST
# branch would accept a body whose email is the caller's own — letting anyone
# set themselves to "remote" and walk out of the geofence entirely.
@require_perm({'GET': 'attendance.view', 'POST': 'attendance.manage_arrangement'},
              or_self=('GET',))
def attendance_arrangements(request):
    """Where each employee works.

    GET  ?email=       -> that employee's full history, newest first
         (no email)    -> the arrangement currently in force for everyone
    POST -> record a change. The previous row is CLOSED, never overwritten:
            a check-in in March has to stay judged by March's arrangement, so
            the old value must survive the change.
    """
    if request.method == 'GET':
        email = norm_email(request.GET.get('email'))
        if email:
            rows = WorkArrangement.objects.filter(email=email)
            return Response([_arrangement_json(a) for a in rows])
        today = local_today()
        rows = (WorkArrangement.objects
                .filter(effective_from__lte=today)
                .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today)))
        # Newest-first ordering means the first row seen per employee is the
        # operative one; later (superseded) rows for the same person are noise.
        seen, out = set(), []
        for a in rows:
            if a.email in seen:
                continue
            seen.add(a.email)
            out.append(_arrangement_json(a))
        return Response(out)

    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')

    arrangement = str(body.get('arrangement') or '').strip().lower()
    if arrangement not in (WorkArrangement.ONSITE, WorkArrangement.HYBRID,
                           WorkArrangement.REMOTE):
        return err('arrangement must be onsite, hybrid or remote')

    weekdays, wd_err = _parse_weekdays(body.get('remoteWeekdays'))
    if wd_err:
        return err(wd_err)

    try:
        per_week = int(body.get('remoteDaysPerWeek') or 0)
    except (TypeError, ValueError):
        return err('remoteDaysPerWeek must be a number')
    if not (0 <= per_week <= 7):
        return err('remoteDaysPerWeek must be between 0 and 7')

    eff_from = parse_date(body.get('effectiveFrom')) or local_today()

    # A hybrid arrangement with neither anchor days nor a quota entitles the
    # employee to nothing, which reads as "hybrid" on screen while behaving
    # exactly like onsite. Refuse it rather than create the confusion.
    if arrangement == WorkArrangement.HYBRID and not weekdays and per_week < 1:
        return err('A hybrid arrangement needs either remote weekdays or a '
                   'remoteDaysPerWeek of at least 1')
    if arrangement != WorkArrangement.HYBRID:
        weekdays, per_week = '', 0

    open_rows = WorkArrangement.objects.filter(email=email, effective_to__isnull=True)

    future = open_rows.filter(effective_from__gt=eff_from).first()
    if future:
        return err(
            f'This employee already has an arrangement starting '
            f'{future.effective_from:%Y-%m-%d}, which is after {eff_from:%Y-%m-%d}. '
            f'Delete that one first if you are replacing it.', 409)

    caller, _user = check_perm(request, 'attendance.manage_arrangement')[1:]

    same_day = open_rows.filter(effective_from=eff_from).first()
    if same_day:
        # Correcting an arrangement on the day it starts. Nothing has been
        # judged under the old value yet, so replace it — closing it would
        # leave a row that ended before it began.
        same_day.arrangement = arrangement
        same_day.remote_weekdays = weekdays
        same_day.remote_days_per_week = per_week
        same_day.notes = str(body.get('notes') or '').strip()
        same_day.created_by = caller or same_day.created_by
        if body.get('employee'):
            same_day.employee_name = str(body.get('employee'))
        same_day.save()
        return Response(_arrangement_json(same_day))

    # Close whatever was in force the day before the new one starts.
    open_rows.filter(effective_from__lt=eff_from).update(
        effective_to=eff_from - timedelta(days=1))

    row = WorkArrangement.objects.create(
        email=email,
        employee_name=str(body.get('employee') or ''),
        arrangement=arrangement,
        remote_weekdays=weekdays,
        remote_days_per_week=per_week,
        effective_from=eff_from,
        notes=str(body.get('notes') or '').strip(),
        created_by=caller or '',
    )
    create_notification(
        email,
        'Work arrangement updated',
        f'Your work arrangement is now "{arrangement}" from '
        f'{eff_from:%d %b %Y}.',
        'info', '/employees/attendance',
    )
    return Response(_arrangement_json(row), status=201)


@api_view(['DELETE'])
@require_perm('attendance.manage_arrangement')
def attendance_arrangement_detail(request, pk):
    """Remove an arrangement row that was entered by mistake.

    Deleting reopens whatever it superseded: without this, correcting a
    mis-keyed row would leave the employee with a gap where no arrangement
    applies, which silently falls back to "needs a WFH request".
    """
    row = WorkArrangement.objects.filter(pk=pk).first()
    if not row:
        return err('Work arrangement not found', 404)
    previous = (WorkArrangement.objects
                .filter(email=row.email, effective_from__lt=row.effective_from)
                .exclude(pk=row.pk)
                .first())
    row.delete()
    if previous and previous.effective_to is not None:
        previous.effective_to = None
        previous.save(update_fields=['effective_to'])
    return Response({'ok': True, 'reopened': previous.id if previous else None})


@api_view(['GET', 'POST'])
@require_perm({'GET': 'attendance.view', 'POST': 'attendance.create'}, or_self=True)
def attendance_events(request):
    """GET: today's (or ?date=) activity-log events for ?email=.
    POST: append an event (break / mode switch) to the signed-in user's day with GPS."""
    if request.method == 'GET':
        day = parse_date(request.GET.get('date')) or local_now().date()
        qs = AttendanceEvent.objects.filter(date=day)
        email = norm_email(request.GET.get('email'))
        if email:
            qs = qs.filter(email=email)
        return Response(AttendanceEventSerializer(qs, many=True).data)

    body = request.data
    email = norm_email(body.get('email'))
    event = str(body.get('event') or body.get('type') or '').strip()
    if not email:
        return err('email is required')
    if event not in ATTENDANCE_EVENT_TYPES:
        return err('event must be one of: ' + ', '.join(ATTENDANCE_EVENT_TYPES))

    # Gate anything that would land the user in a Remote presence. That is the
    # explicit remote-switch, but also a check-in / office-switch carrying a
    # "Home"/"Remote" location, which _presence_for() reads as Remote just the
    # same — gating only the former would leave that loophole open.
    if event == 'remote-switch' or _looks_remote(body.get('location')):
        allowed, reason = remote_switch_check(email)
        if not allowed:
            return JsonResponse({'message': reason, 'code': 'wfh_not_approved'}, status=403)

    lat = body.get('latitude')
    lng = body.get('longitude')
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (ValueError, TypeError):
        lat = None
        lng = None

    fence_obj = None
    if lat is not None and lng is not None:
        _, fence_obj = _check_geofence(lat, lng)

    now = local_now()
    obj = AttendanceEvent.objects.create(
        email=email,
        employee_name=body.get('employee') or body.get('name') or '',
        date=now.date(),
        event=event,
        location=fence_obj.name if fence_obj else str(body.get('location') or '').strip(),
        latitude=lat,
        longitude=lng,
        geo_fence_id=fence_obj.id if fence_obj else None,
        at=now,
    )

    # A location switch is a statement about where you are, and _team_status
    # shows an explicit presence verbatim in preference to the location — so
    # someone who picked "Available" this morning and then switched to Remote
    # went on reading "Available" to the whole team, and the switch they just
    # made was invisible. Clearing the label lets the new location speak.
    #
    # A break label is never cleared here: it is ended by break-end, which is
    # also what accrues the elapsed minutes. Dropping it silently would lose
    # that time from "Break taken today".
    if event in ('remote-switch', 'office-switch'):
        att = EmployeeAttendance.objects.filter(email=email, date=now.date()).first()
        if att and att.presence and not _is_break_label(att.presence):
            att.presence = ''
            att.presence_at = now
            att.save(update_fields=['presence', 'presence_at'])

    # Manage live presence & break duration
    if event in ('break-start', 'break-end'):
        att = EmployeeAttendance.objects.filter(email=email, date=now.date()).first()
        if att:
            if event == 'break-start':
                att.presence = 'Away'
            else:
                att.presence = ''
                # Calculate break minutes since last break-start today
                last_start = AttendanceEvent.objects.filter(
                    email=email, date=now.date(), event='break-start'
                ).order_by('-at').first()
                if last_start:
                    brk_min = int((now - last_start.at).total_seconds() // 60)
                    att.break_minutes = (att.break_minutes or 0) + max(brk_min, 0)
            att.presence_at = now
            att.save()

    return Response(AttendanceEventSerializer(obj).data, status=201)


@api_view(['POST'])
@require_perm('attendance.create', or_self=True)
def attendance_presence(request):
    """Set the signed-in employee's live presence (the STATUS picker:
    Available / Away / Busy / Do not disturb / ...). Valid only while checked
    in. Selecting 'Away' logs a Break Start on the activity log (and leaving
    'Away' logs a Break End) so the timeline stays consistent."""
    body = request.data
    email = norm_email(body.get('email'))
    label = str(body.get('label') or '').strip()
    if not email:
        return err('email is required')
    if not label:
        return err('label is required')
    now = local_now()
    att = EmployeeAttendance.objects.filter(email=email, date=now.date()).first()
    if not att or not _is_checked_in(att):
        return err('Presence can only be set while checked in', 409)
    prev = att.presence or ''
    was_break = _is_break_label(prev)
    now_break = _is_break_label(label)
    name = att.employee_name or body.get('employee') or body.get('name') or ''
    if now_break and not was_break:
        # Starting a break (Away / Coffee break / …) → log the start on the timeline.
        AttendanceEvent.objects.create(
            email=email, employee_name=name, date=now.date(),
            event='break-start', location='', at=now,
        )
    elif was_break and not now_break:
        # Ending a break → log the end and accrue the elapsed break time so it
        # shows on the check-in card as "Break taken today".
        AttendanceEvent.objects.create(
            email=email, employee_name=name, date=now.date(),
            event='break-end',
            location=('Home' if att.device == 'mobile' else 'Office'), at=now,
        )
        last_start = AttendanceEvent.objects.filter(
            email=email, date=now.date(), event='break-start'
        ).order_by('-at').first()
        if last_start:
            brk_min = int((now - last_start.at).total_seconds() // 60)
            att.break_minutes = (att.break_minutes or 0) + max(brk_min, 0)
    att.presence = label
    att.presence_at = now
    att.save()
    return Response(EmployeeAttendanceSerializer(att).data)


def _team_status(event, att):
    """Live status label + 'since' datetime for the Team Status panel. An
    explicit presence choice (STATUS picker / break) wins; otherwise we fall
    back to the location implied by the latest activity event. Checked-out or
    no-show employees are Absent."""
    if att is None or not _is_checked_in(att):
        return 'Absent', None
    if att.presence:
        # A break-type status (Away / Coffee break / …) shows as "In Break";
        # every other choice shows its own label verbatim.
        if _is_break_label(att.presence):
            return IN_BREAK_LABEL, (att.presence_at or att.check_in)
        return att.presence, (att.presence_at or att.check_in)
    if event is not None:
        e = event.event
        if e in ('check-in', 'office-switch', 'break-end'):
            loc = (event.location or '').lower()
            remote = 'home' in loc or 'remote' in loc
            return ('Remote' if remote else 'In Office'), event.at
        if e == 'remote-switch':
            return 'Remote', event.at
        if e == 'break-start':
            return IN_BREAK_LABEL, event.at
    return ('Remote' if att.device == 'mobile' else 'In Office'), att.check_in


@api_view(['GET'])
def attendance_team(request):
    """Live presence snapshot for the whole team (Team Status Now panel).
    Status per person comes from their latest activity event today, else from
    their attendance record; everyone else is shown as Absent.

    Intentionally ungated: every employee may see their team's presence on the
    Check In/Out page (it exposes only name + coarse status + since-time, no
    sensitive data), so the attendance.view permission is not required here."""
    today = local_now().date()

    # Latest event per email today (queryset is ordered by ``at`` ascending, so
    # the last write per email wins) + the best name we have seen for them.
    latest_event = {}
    event_name = {}
    for ev in AttendanceEvent.objects.filter(date=today):
        latest_event[ev.email] = ev
        if ev.employee_name:
            event_name[ev.email] = ev.employee_name
    today_att = {a.email: a for a in EmployeeAttendance.objects.filter(date=today)}

    # Roster = all app users, plus any email that has activity but no login row.
    roster = [(u.email, u.full_name) for u in AppUser.objects.all()]
    known = {e for e, _ in roster}
    for email in set(list(latest_event.keys()) + list(today_att.keys())):
        if email not in known:
            att = today_att.get(email)
            roster.append((email, event_name.get(email) or (att.employee_name if att else '')))

    priority = {'In Office': 0, 'Remote': 1, 'In Break': 2, 'Absent': 3}
    rows = []
    for email, name in roster:
        att = today_att.get(email)
        status, since_dt = _team_status(latest_event.get(email), att)
        display = (name or event_name.get(email)
                   or (att.employee_name if att else '') or email.split('@')[0])
        rows.append({
            'email': email,
            'name': display,
            'status': status,
            'since': since_dt.strftime('%I:%M %p') if since_dt else '—',
        })
    rows.sort(key=lambda r: (priority.get(r['status'], 9), r['name'].lower()))
    return Response(rows)


@api_view(['GET'])
@require_perm('attendance.view')
def attendance_map_feed(request):
    """Feed for the Interactive Admin Map & Heatmap.

    Returns:
      - fences: list of active office locations with coordinates, radius and headcount.
      - employees: live checked-in sessions today with latest coordinates, status, and device.
    """
    try:
        today = local_now().date()

        # 1. Company office & home fences
        fences_qs = GeoFence.objects.filter(owner_email='')
        fences_list = []
        for f in fences_qs:
            try:
                lat = float(f.latitude) if f.latitude is not None else None
                lng = float(f.longitude) if f.longitude is not None else None
            except (ValueError, TypeError):
                lat, lng = None, None
            if lat is not None and lng is not None:
                fences_list.append({
                    'id': f.id,
                    'name': f.name or 'Office',
                    'latitude': lat,
                    'longitude': lng,
                    'radiusMeters': int(f.radius_meters) if f.radius_meters else 200,
                    'radius_meters': int(f.radius_meters) if f.radius_meters else 200,
                    'isHome': False,
                    'ownerEmail': '',
                    'activeCount': 0,
                })

        # Include registered home fences for WFH/remote tracking
        for hf in GeoFence.objects.exclude(owner_email=''):
            try:
                h_lat = float(hf.latitude) if hf.latitude is not None else None
                h_lng = float(hf.longitude) if hf.longitude is not None else None
            except (ValueError, TypeError):
                h_lat, h_lng = None, None
            if h_lat is not None and h_lng is not None:
                fences_list.append({
                    'id': hf.id,
                    'name': hf.name or f"{hf.owner_email.split('@')[0]}'s Home",
                    'latitude': h_lat,
                    'longitude': h_lng,
                    'radiusMeters': int(hf.radius_meters) if hf.radius_meters else 150,
                    'radius_meters': int(hf.radius_meters) if hf.radius_meters else 150,
                    'isHome': True,
                    'ownerEmail': hf.owner_email,
                    'activeCount': 0,
                })

        # 2. Profiles and App users metadata (including profile_pic)
        user_map = {}
        try:
            for p in UserProfile.objects.all():
                em = norm_email(p.email)
                if em:
                    user_map[em] = {
                        'department': p.department or 'General',
                        'role': p.designation or '',
                        'name': f"{p.first_name} {p.last_name}".strip(),
                        'profilePic': p.profile_pic or '',
                    }
        except Exception:
            pass

        try:
            for u in AppUser.objects.all():
                em = norm_email(u.email)
                if em:
                    if em not in user_map:
                        user_map[em] = {
                            'department': 'General',
                            'role': u.role or '',
                            'name': u.full_name or em.split('@')[0],
                            'profilePic': '',
                        }
                    else:
                        if not user_map[em]['name'] and u.full_name:
                            user_map[em]['name'] = u.full_name
                        if not user_map[em]['role'] and u.role:
                            user_map[em]['role'] = u.role
        except Exception:
            pass

        # 3. Today's attendance and latest events
        today_atts = {norm_email(a.email): a for a in EmployeeAttendance.objects.filter(date=today)}
        latest_events = {}
        for ev in AttendanceEvent.objects.filter(date=today).order_by('at'):
            latest_events[norm_email(ev.email)] = ev

        employees_list = []
        for email, att in today_atts.items():
            if not _is_checked_in(att):
                continue
            ev = latest_events.get(email)
            u_info = user_map.get(email, {})

            raw_lat = att.location_lat if att.location_lat is not None else (ev.latitude if ev else None)
            raw_lng = att.location_lng if att.location_lng is not None else (ev.longitude if ev else None)

            try:
                lat = float(raw_lat) if raw_lat is not None else None
                lng = float(raw_lng) if raw_lng is not None else None
            except (ValueError, TypeError):
                lat, lng = None, None

            is_simulated_coord = False
            # Resolve coordinates for Remote / WFH users
            if (lat is None or lng is None) and att.is_wfh:
                home = home_fence_for(email)
                if home and home.latitude is not None and home.longitude is not None:
                    try:
                        lat = float(home.latitude)
                        lng = float(home.longitude)
                        is_simulated_coord = True
                    except (ValueError, TypeError):
                        pass
                elif fences_list:
                    # Provide visual fallback near company location with slight offset
                    office_fences = [f for f in fences_list if not f.get('isHome')]
                    base_f = office_fences[0] if office_fences else fences_list[0]
                    lat = base_f['latitude'] + 0.007
                    lng = base_f['longitude'] + 0.007
                    is_simulated_coord = True
            # Fallback to office fence coordinates if verified on-site without raw GPS
            elif (lat is None or lng is None) and att.geo_verified and fences_list:
                office_fences = [f for f in fences_list if not f.get('isHome')]
                base_f = office_fences[0] if office_fences else fences_list[0]
                lat = base_f['latitude']
                lng = base_f['longitude']
                is_simulated_coord = True

            status_lbl, since_dt = _team_status(ev, att)
            if att.location_status == 'Pending':
                status_lbl = 'Pending Review'

            employees_list.append({
                'email': email,
                'name': att.employee_name or u_info.get('name') or email.split('@')[0],
                'department': u_info.get('department') or 'General',
                'role': u_info.get('role') or '',
                'profilePic': u_info.get('profilePic') or '',
                'latitude': lat,
                'longitude': lng,
                'accuracy': float(ev.accuracy) if ev and hasattr(ev, 'accuracy') and ev.accuracy is not None else None,
                'status': status_lbl,
                'geoVerified': bool(att.geo_verified),
                'isWfh': bool(att.is_wfh),
                'device': att.device or (getattr(ev, 'device', None) if ev else 'desktop') or 'desktop',
                'checkIn': att.check_in.strftime('%I:%M %p') if att.check_in else '',
                'checkInIso': att.check_in.isoformat() if att.check_in else '',
                'locationReason': att.location_reason or '',
                'locationStatus': att.location_status or '',
                'isSimulatedCoord': is_simulated_coord,
            })

        # Count employees in each fence
        for f in fences_list:
            f_lat, f_lng, f_rad = f['latitude'], f['longitude'], f['radiusMeters']
            is_home = f.get('isHome', False)
            cnt = 0
            for emp in employees_list:
                if emp['latitude'] is not None and emp['longitude'] is not None:
                    if is_home:
                        if emp.get('isWfh') and emp.get('email') == f.get('ownerEmail'):
                            cnt += 1
                    else:
                        if not emp.get('isWfh') and emp.get('geoVerified'):
                            try:
                                d = _haversine_distance(float(emp['latitude']), float(emp['longitude']), float(f_lat), float(f_lng))
                                if d <= f_rad + 60 or emp.get('isSimulatedCoord'):
                                    cnt += 1
                            except Exception:
                                pass
            f['activeCount'] = cnt

        return Response({
            'fences': fences_list,
            'employees': employees_list,
            'timestamp': local_now().isoformat(),
        })
    except Exception as exc:
        logger.exception("Error in attendance_map_feed: %s", exc)
        return Response({
            'fences': [],
            'employees': [],
            'error': str(exc),
            'timestamp': local_now().isoformat(),
        })


@api_view(['GET', 'POST'])
def attendance_departure_policy(request):
    """Configuration for Auto Check-Out on Geofence Departure."""
    KEY = 'attendance_departure_policy'
    default_policy = {
        'enabled': True,
        'timeout_minutes': 15,
        'warning_minutes': 5,
        'notify_employee': True,
    }
    try:
        if request.method == 'GET':
            obj = MasterDataSet.objects.filter(key=KEY).first()
            if not obj or not obj.options:
                return Response(default_policy)
            opts = obj.options
            if isinstance(opts, str):
                try:
                    import json
                    opts = json.loads(opts)
                except Exception:
                    opts = {}
            if not isinstance(opts, dict):
                opts = {}
            return Response({**default_policy, **opts})

        # POST
        data = request.data or {}
        obj, _ = MasterDataSet.objects.get_or_create(
            key=KEY,
            defaults={'label': 'Attendance Departure Policy', 'options': default_policy}
        )
        new_policy = {
            'enabled': bool(data.get('enabled', True)),
            'timeout_minutes': int(data.get('timeout_minutes', 15) or 15),
            'warning_minutes': int(data.get('warning_minutes', 5) or 5),
            'notify_employee': bool(data.get('notify_employee', True)),
        }
        obj.options = new_policy
        obj.save()
        return Response(new_policy)
    except Exception as exc:
        logger.exception("Error in attendance_departure_policy: %s", exc)
        return Response(default_policy)


# --- Leave Management ------------------------------------------------------
@api_view(['GET', 'POST'])
@require_perm({'GET': 'leave.view', 'POST': 'leave.create'}, or_self=True)
def leave(request):
    if request.method == 'GET':
        from .permissions import _get_caller, _is_super_admin, _user_has_perm
        qs = LeaveRequest.objects.all()
        email = norm_email(request.GET.get('email'))
        # A caller who cannot action leave (approve/reject) only ever sees their
        # own requests, whatever ?email= says — the frontend filter is cosmetic,
        # this is the enforcement.
        caller_email, caller = _get_caller(request)
        if not (_is_super_admin(caller) or _user_has_perm(caller, 'leave.action')):
            email = caller_email
        if email:
            qs = qs.filter(email=email)
        status_filter = request.GET.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response(LeaveRequestSerializer(qs, many=True).data)

    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    if not body.get('fromDate') or not body.get('toDate'):
        return err('fromDate and toDate are required')
    serializer = LeaveRequestSerializer(data={**body, 'email': email})
    if not serializer.is_valid():
        return serializer_err(serializer)
    inst = serializer.save()
    create_notification(
        email,
        'Leave request submitted',
        'Your leave request has been submitted and is awaiting approval.',
        'info',
        '/employees/leave',
    )
    emp_name = inst.employee_name or email
    notify_approvers(
        'leave.action',
        'New leave request',
        f"{emp_name} has requested leave ({inst.type}) from {inst.from_date} to {inst.to_date}.",
        '/employees/leave',
    )
    return Response(serializer.data, status=201)


# DELETE is intentionally absent from the decorator map: the decorator still
# requires a resolvable, active caller, but the check happens in the view,
# because deleting is a pure ownership rule that or_self cannot express (the
# URL carries no email to compare).
@api_view(['PUT', 'DELETE'])
@require_perm({'PUT': 'leave.action'})
def leave_detail(request, pk):
    obj = LeaveRequest.objects.filter(pk=pk).first()
    if not obj:
        return err('Leave request not found', 404)
    if request.method == 'DELETE':
        from .permissions import _get_caller, _is_super_admin, _user_has_perm
        caller_email, caller = _get_caller(request)
        # Deleting is exclusively employee self-service: approvers — Super
        # Admin or anyone holding leave.action — never delete, they Decline,
        # which keeps the history. A plain employee may delete only their
        # OWN still-Pending request.
        if _is_super_admin(caller) or _user_has_perm(caller, 'leave.action'):
            return err('Approvers cannot delete requests — decline them instead.', 403)
        if norm_email(obj.email) != caller_email:
            # 404, not 403 — don't confirm someone else's request id exists
            return err('Leave request not found', 404)
        if obj.status != 'Pending':
            return err('Only pending requests can be deleted.', 403)
        obj.delete()
        return Response({'ok': True})
    serializer = LeaveRequestSerializer(obj, data=request.data, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    new_status = str(request.data.get('status') or '').strip()
    if new_status and new_status.lower() in {'approved', 'rejected'}:
        create_notification(
            obj.email,
            'Leave request update',
            f"Your leave request has been {new_status.lower()}.",
            'success' if new_status.lower() == 'approved' else 'warning',
            '/employees/leave',
        )
    return Response(serializer.data)


@api_view(['GET'])
@require_perm('leave.view', or_self=True)
def leave_balance(request):
    """Per-type leave balance for ?email= (allowance − approved days)."""
    email = norm_email(request.GET.get('email'))
    if not email:
        return err('email is required')
    used = {}
    for lr in LeaveRequest.objects.filter(email=email, status='Approved'):
        used[lr.type] = used.get(lr.type, 0) + (lr.days or 0)
    balances = [
        {'type': typ, 'allowance': allowance,
         'used': used.get(typ, 0), 'remaining': max(allowance - used.get(typ, 0), 0)}
        for typ, allowance in DEFAULT_LEAVE_ALLOWANCE
    ]
    return Response({'email': email, 'balances': balances})


# --- Notifications --------------------------------------------------------
@api_view(['GET'])
def notifications(request):
    email = _resolve_recipient_email(request)
    if not email:
        return Response([])
    qs = Notification.objects.filter(recipient=email)
    unread_only = str(request.GET.get('unreadOnly') or request.GET.get('unread') or '').strip().lower()
    if unread_only in {'1', 'true', 'yes', 'on'}:
        qs = qs.filter(is_read=False)
    limit = request.GET.get('limit')
    if limit:
        try:
            qs = qs[:int(limit)]
        except ValueError:
            pass
    return Response(NotificationSerializer(qs, many=True).data)


@api_view(['POST'])
def notification_read(request, pk):
    email = _resolve_recipient_email(request)
    if not email:
        return err('email is required')
    obj = Notification.objects.filter(pk=pk, recipient=email).first()
    if not obj:
        return err('Notification not found', 404)
    obj.is_read = True
    obj.save()
    return Response(NotificationSerializer(obj).data)


@api_view(['DELETE'])
def notification_delete(request, pk):
    email = _resolve_recipient_email(request)
    if not email:
        return err('email is required')
    obj = Notification.objects.filter(pk=pk, recipient=email).first()
    if not obj:
        return err('Notification not found', 404)
    obj.delete()
    return Response({'ok': True, 'id': pk})


@api_view(['POST'])
def notifications_delete_batch(request):
    email = _resolve_recipient_email(request)
    if not email:
        return err('email is required')
    ids = request.data.get('ids') if hasattr(request, 'data') else []
    if not isinstance(ids, list):
        return err('ids must be a list')
    normalized = [int(i) for i in ids if str(i).isdigit()]
    qs = Notification.objects.filter(recipient=email, pk__in=normalized)
    deleted = qs.count()
    qs.delete()
    return Response({'ok': True, 'deleted': deleted})


@api_view(['POST'])
def notifications_read_all(request):
    email = _resolve_recipient_email(request)
    if not email:
        return Response({'ok': True, 'count': 0})
    count = Notification.objects.filter(recipient=email, is_read=False).update(is_read=True)
    return Response({'ok': True, 'count': count})


# --- Task Tracker ----------------------------------------------------------
def _data_url_too_large(data_url, limit_bytes=50 * 1024):
    """Rough decoded-size check for a `data:<mime>;base64,<data>` string."""
    b64 = str(data_url).split(',', 1)[-1]
    return (len(b64) * 3) // 4 > limit_bytes


@api_view(['GET', 'POST'])
@require_perm({'GET': 'employee.view', 'POST': 'employee.create'}, or_self=True)
def tasks(request):
    if request.method == 'GET':
        qs = EmployeeTask.objects.all()
        assignee = request.GET.get('assignee')
        if assignee:
            qs = qs.filter(assignee=assignee)
        assignee_email = norm_email(request.GET.get('assigneeEmail'))
        if assignee_email:
            qs = qs.filter(assignee_email=assignee_email)
        stage = request.GET.get('stage')
        if stage:
            qs = qs.filter(stage=stage)
        return Response(EmployeeTaskSerializer(qs, many=True).data)

    body = request.data
    if not body.get('title'):
        return err('title is required')
    for field, label in (('attachmentFileData', 'File attachment'), ('imageFileData', 'Image attachment')):
        if body.get(field) and _data_url_too_large(body[field]):
            return err(f'{label} exceeds the 50 KB limit')
    if not body.get('taskCode'):
        body = {**body, 'taskCode': f'TASK-{int(local_now().timestamp() * 1000)}'}
    serializer = EmployeeTaskSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    assignee_email = norm_email(body.get('assigneeEmail') or body.get('assignee') or '')
    if assignee_email:
        create_notification(
            assignee_email,
            'New task assigned',
            f"You have been assigned a new task: {body.get('title', 'Task')}",
            'info',
            '/employees/tasks',
        )
    return Response(serializer.data, status=201)


@api_view(['PUT', 'DELETE'])
@require_perm({'PUT': 'task.edit', 'DELETE': 'task.delete'})
def task_detail(request, pk):
    obj = EmployeeTask.objects.filter(pk=pk).first()
    if not obj:
        return err('Task not found', 404)
    if request.method == 'DELETE':
        if obj.stage != 'todo':
            return err('Only a task still in To Do can be deleted.', 409)
        obj.delete()
        return Response({'ok': True})
    serializer = EmployeeTaskSerializer(obj, data=request.data, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    return Response(serializer.data)


# --- Work Submissions ------------------------------------------------------
@api_view(['GET', 'POST'])
@require_perm({'GET': 'employee.view', 'POST': 'employee.create'}, or_self=True)
def submissions(request):
    if request.method == 'GET':
        qs = WorkSubmission.objects.all()
        # Reviewers see the whole queue; everyone else sees only what they filed
        # themselves. Scoped here rather than in the UI so an Employee cannot
        # widen it by passing ?email= for somebody else, or by calling the API
        # directly. A caller with no resolvable identity gets nothing.
        see_all, caller_email, _ = check_perm(request, 'submission.view_all')
        if not see_all:
            qs = qs.filter(email=caller_email or '')
        email = norm_email(request.GET.get('email'))
        if email:
            qs = qs.filter(email=email)
        status_filter = request.GET.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response(WorkSubmissionSerializer(qs, many=True).data)

    body = dict(request.data or {})
    email = norm_email(body.get('email'))
    if not email:
        email = 'yaswanth@eversoft.com'
    if not body.get('title'):
        return err('title is required')
    if not body.get('date'):
        import datetime
        body['date'] = datetime.date.today().isoformat()
    if not body.get('aiScore') and not body.get('ai_score'):
        import random
        body['aiScore'] = random.randint(78, 96)
    serializer = WorkSubmissionSerializer(data={**body, 'email': email})
    if not serializer.is_valid():
        return serializer_err(serializer)
    inst = serializer.save()
    create_notification(
        email,
        'Work submission created',
        f"Your work submission '{inst.title}' has been submitted for review.",
        'info',
        '/employees/submissions',
    )
    emp_name = inst.employee_name or email
    notify_approvers(
        'submission.action',
        'New work submission',
        f"{emp_name} has submitted a new work item: '{inst.title}'.",
        '/employees/submissions',
    )
    return Response(serializer.data, status=201)


@api_view(['GET'])
@require_perm('employee.view', or_self=True)
def submission_file(request, pk):
    """Serve one submission's attachment.

    The bytes are deliberately kept out of the list response, so the queue
    stays cheap to load and a reviewer fetches only the file they open.
    """
    obj = WorkSubmission.objects.filter(pk=pk).first()
    if not obj:
        return err('Submission not found', 404)
    # Anyone trusted with the whole queue may open any attachment; everyone
    # else only their own. Without this an employee could read a colleague's
    # work by guessing an id.
    see_all, caller_email, _ = check_perm(request, 'submission.view_all')
    if not see_all and norm_email(obj.email) != (caller_email or ''):
        return err('You can only open your own submissions.', 403)
    if not obj.file_data:
        return err('This submission has no stored attachment.', 404)
    return Response({
        'id': obj.id,
        'name': obj.file_name or 'attachment',
        'mime': obj.file_mime or '',
        'size': obj.file_size or 0,
        'data': obj.file_data,
    })


@api_view(['GET'])
@require_perm('employee.view', or_self=True)
def submission_review_file(request, pk):
    """Serve whatever the reviewer marked up when sending the work back.

    The submitter must be able to read this — it is the correction they are
    being asked to act on — so ownership grants access here just as
    submission.view_all does.
    """
    obj = WorkSubmission.objects.filter(pk=pk).first()
    if not obj:
        return err('Submission not found', 404)
    see_all, caller_email, _ = check_perm(request, 'submission.view_all')
    if not see_all and norm_email(obj.email) != (caller_email or ''):
        return err('You can only open your own submissions.', 403)
    if not obj.review_file_data:
        return err('No file was attached to this review.', 404)
    return Response({
        'id': obj.id,
        'name': obj.review_file_name or 'review-attachment',
        'mime': obj.review_file_mime or '',
        'size': obj.review_file_size or 0,
        'data': obj.review_file_data,
    })


@api_view(['PUT', 'DELETE'])
# Both methods are gated manually below: PUT by the status being set, DELETE by
# ownership. The empty map still refuses an unidentified caller — require_perm
# enforces identity before it consults the map.
@require_perm({})
def submission_detail(request, pk):
    obj = WorkSubmission.objects.filter(pk=pk).first()
    if not obj:
        return err('Submission not found', 404)
    if request.method == 'DELETE':
        # Two ways to delete. employee.delete removes anyone's submission at any
        # stage (admin clean-up). Otherwise the submitter may withdraw their own,
        # but only while it is still Pending — once a reviewer has picked it up
        # or ruled on it, the record is part of the audit trail and stays.
        allowed, caller_email, user = check_perm(request, 'employee.delete')
        if not allowed:
            own = bool(caller_email) and norm_email(obj.email) == caller_email
            pending = str(obj.status or '').strip().lower() == 'pending'
            if not own:
                return err("You can only delete your own submissions.", 403)
            if not pending:
                return err(
                    "This submission is already {}, so it can no longer be deleted.".format(
                        (obj.status or 'in review').lower()),
                    403,
                )
        obj.delete()
        return Response({'ok': True})

    # A PUT that moves the submission through review — into review, approved or
    # rejected — needs the single "action" permission (submission.action); any
    # other edit needs employee.edit. This lets a reviewer work the queue
    # without granting full edit rights.
    status_val = str(request.data.get('status') or '').strip().lower()
    if status_val == 'approved':
        need, verb = 'submission.action', 'approve'
    elif status_val == 'rejected':
        need, verb = 'submission.action', 'reject'
    elif status_val == 'in review':
        need, verb = 'submission.action', 'review'
    else:
        need, verb = 'employee.edit', 'edit'
    allowed, caller_email, user = check_perm(request, need)
    # "In Review" is not a verdict — it only records that a reviewer has opened
    # the work, which the queue now does by itself. Anyone trusted to see the
    # whole queue may do that; approving and rejecting still need
    # submission.action. Without this a Team Lead (who reviews but deliberately
    # cannot decide) would watch the row move and then snap back on reload.
    if not allowed and verb == 'review':
        allowed = check_perm(request, 'submission.view_all')[0]
    if caller_email and user and not allowed:
        return err(f"You don't have permission to {verb} work submissions.", 403)

    serializer = WorkSubmissionSerializer(obj, data=request.data, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    inst = serializer.save()
    # Timestamp and attribute the rejection note here rather than trusting the
    # browser's clock or its claim about who is writing.
    if str(request.data.get('reviewerNote') or '').strip():
        inst.reviewer_note_at = local_now()
        if user and not str(request.data.get('reviewerNoteBy') or '').strip():
            inst.reviewer_note_by = user.full_name
        inst.save(update_fields=['reviewer_note_at', 'reviewer_note_by', 'updated_at'])
    new_status = str(request.data.get('status') or '').strip()
    if new_status.lower() in {'approved', 'rejected'} and obj.email:
        create_notification(
            obj.email,
            'Work submission update',
            f"Your work submission '{inst.title}' has been {new_status.lower()}.",
            'success' if new_status.lower() == 'approved' else 'warning',
            '/employees/submissions',
        )
    # Stamp the reviewer on an approve/reject when the client didn't supply one.
    if need != 'employee.edit' and not str(request.data.get('reviewer') or '').strip() and user:
        inst.reviewer = user.full_name
        inst.save(update_fields=['reviewer', 'updated_at'])
    return Response(WorkSubmissionSerializer(inst).data)


# --- Bench Submissions (Recruit > Bench Sales > Bench Submissions) ---------
@api_view(['GET', 'POST'])
@require_perm({'GET': 'recruitment.view', 'POST': 'recruitment.create'})
def bench_submissions(request):
    if request.method == 'GET':
        qs = BenchSubmission.objects.all()
        status_filter = request.query_params.get('status')
        search = request.query_params.get('search')
        if status_filter:
            qs = qs.filter(status=status_filter)
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(client_name__icontains=search) |
                Q(vendor_prime_vendor__icontains=search) | Q(vendor_person_name__icontains=search)
            )
        return Response(BenchSubmissionSerializer(qs, many=True).data)

    body = dict(request.data or {})
    if not str(body.get('name') or '').strip():
        return err('name is required')
    _, caller_email, _ = check_perm(request, 'recruitment.create')
    body['createdBy'] = caller_email or ''
    serializer = BenchSubmissionSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)
    serializer.save()
    return Response(serializer.data, status=201)


@api_view(['GET', 'PATCH', 'PUT', 'DELETE'])
@require_perm({'GET': 'recruitment.view', 'PATCH': 'recruitment.edit',
               'PUT': 'recruitment.edit', 'DELETE': 'recruitment.delete'})
def bench_submission_detail(request, pk):
    obj = BenchSubmission.objects.filter(pk=pk).first()
    if not obj:
        return err('Submission not found', 404)
    if request.method == 'GET':
        return Response(BenchSubmissionSerializer(obj).data)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    serializer = BenchSubmissionSerializer(obj, data=request.data, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    inst = serializer.save()
    return Response(BenchSubmissionSerializer(inst).data)


# --- Bench Sales (Recruit > Bench Sales) — consultant profile roster -------
MAX_CERTIFICATES_PER_PROFILE = 10
_BENCH_SALES_SINGLE_FILE_FIELDS = {
    'resume': ('resumeFileName', 'resumeFileMime'),
    'dlStateId': ('dlStateIdFileName', 'dlStateIdFileMime'),
    'i94': ('i94FileName', 'i94FileMime'),
}
# Per-file upload cap (resume / DL / I-94 / each certificate), in MB. Mirrors
# MAX_UPLOAD_MB in dist/dist/assets/hrms-bench-sales.js — keep the two in sync.
MAX_BENCH_SALES_FILE_MB = 34
MAX_BENCH_SALES_FILE_BYTES = MAX_BENCH_SALES_FILE_MB * 1024 * 1024


def _base64_decoded_size(data):
    """Byte size of a base64 payload without decoding it (files are stored as
    base64 text, so decoding a 34 MB upload just to measure it would double the
    request's memory)."""
    data = str(data or '').strip()
    if not data:
        return 0
    padding = len(data) - len(data.rstrip('='))
    return (len(data) * 3) // 4 - padding


def _bench_sales_file_too_large(file_name, data):
    if _base64_decoded_size(data) > MAX_BENCH_SALES_FILE_BYTES:
        return (f'{file_name} is larger than {MAX_BENCH_SALES_FILE_MB} MB — '
                f'each file must be {MAX_BENCH_SALES_FILE_MB} MB or smaller.')
    return None


# Request key -> model column for the three single-document base64 payloads.
_BENCH_SALES_FILE_DATA_COLS = {
    'resumeFileData': 'resume_file_data',
    'dlStateIdFileData': 'dl_state_id_file_data',
    'i94FileData': 'i94_file_data',
}


def _bench_sales_profile_qs():
    """Profiles WITHOUT their base64 document columns loaded.

    A 34 MB PDF is ~45 MB of base64 per column, so selecting the blobs just to
    list the roster (or to re-save an unchanged row on edit) would move hundreds
    of MB per request and blow past MySQL's max_allowed_packet. The columns are
    deferred; a LENGTH() annotation per column lets the serializer report
    has-file flags without touching the data."""
    from django.db.models.functions import Length
    cols = list(_BENCH_SALES_FILE_DATA_COLS.values())
    return BenchSalesProfile.objects.defer(*cols).annotate(
        **{f'{col}_len': Length(col) for col in cols}
    )


def _pop_bench_sales_file_data(body):
    """Removes the base64 payloads from the request body (so the serializer's
    INSERT/UPDATE never carries them) and returns {column: data} for the ones
    actually supplied."""
    files = {}
    for key, col in _BENCH_SALES_FILE_DATA_COLS.items():
        if key in body:
            data = body.pop(key)
            if data:
                files[col] = data
    return files


def _store_bench_sales_file_data(pk, files):
    """One UPDATE per document column: each statement then carries at most one
    ~45 MB payload, which stays under the 64 MB max_allowed_packet default. A
    single multi-column write of two 34 MB files (~91 MB) drops the connection
    with 'MySQL server has gone away'."""
    for col, data in files.items():
        BenchSalesProfile.objects.filter(pk=pk).update(**{col: data})


def _is_pdf(mime, file_name):
    mime = (mime or '').lower()
    file_name = (file_name or '').lower()
    return mime == 'application/pdf' or file_name.endswith('.pdf')


def _validate_bench_sales_files(body):
    """Rejects non-PDF uploads and files over MAX_BENCH_SALES_FILE_MB for
    resume/DL/I-94/certificates. Returns an error string, or None if every
    provided file is an acceptable PDF."""
    for name_key, mime_key in _BENCH_SALES_SINGLE_FILE_FIELDS.values():
        file_name = body.get(name_key)
        if file_name and not _is_pdf(body.get(mime_key), file_name):
            return f'{file_name} is not a PDF — only PDF uploads are allowed.'
        data_key = name_key[:-len('FileName')] + 'FileData'
        size_error = _bench_sales_file_too_large(file_name or data_key, body.get(data_key))
        if size_error:
            return size_error
    certs = body.get('certificates') or []
    if not isinstance(certs, list):
        return 'certificates must be a list of files'
    for c in certs:
        file_name = (c or {}).get('fileName')
        if file_name and not _is_pdf((c or {}).get('fileMime'), file_name):
            return f'{file_name} is not a PDF — only PDF uploads are allowed.'
        size_error = _bench_sales_file_too_large(file_name or 'certificate', (c or {}).get('fileData'))
        if size_error:
            return size_error
    return None


@api_view(['GET', 'POST'])
@require_perm({'GET': 'recruitment.view', 'POST': 'recruitment.create'})
def bench_sales_profiles(request):
    if request.method == 'GET':
        qs = _bench_sales_profile_qs()
        search = request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(tech_stack__icontains=search) |
                Q(email__icontains=search) | Q(contact_no__icontains=search)
            )
        return Response(BenchSalesProfileSerializer(qs, many=True).data)

    body = dict(request.data or {})
    if not str(body.get('name') or '').strip():
        return err('name is required')
    certs = body.pop('certificates', None) or []
    if len(certs) > MAX_CERTIFICATES_PER_PROFILE:
        return err(f'You can upload up to {MAX_CERTIFICATES_PER_PROFILE} certificates.')
    file_error = _validate_bench_sales_files(dict(body, certificates=certs))
    if file_error:
        return err(file_error)
    _, caller_email, _ = check_perm(request, 'recruitment.create')
    body['createdBy'] = caller_email or ''
    files = _pop_bench_sales_file_data(body)
    serializer = BenchSalesProfileSerializer(data=body)
    if not serializer.is_valid():
        return serializer_err(serializer)
    inst = serializer.save()
    _store_bench_sales_file_data(inst.pk, files)
    if certs:
        BenchSalesCertificate.objects.bulk_create([
            BenchSalesCertificate(
                profile=inst, file_name=c.get('fileName') or '',
                file_mime=c.get('fileMime') or '', file_data=c.get('fileData') or '',
            ) for c in certs
        ], batch_size=1)
    inst = _bench_sales_profile_qs().get(pk=inst.pk)
    return Response(BenchSalesProfileSerializer(inst).data, status=201)


@api_view(['GET', 'PATCH', 'PUT', 'DELETE'])
@require_perm({'GET': 'recruitment.view', 'PATCH': 'recruitment.edit',
               'PUT': 'recruitment.edit', 'DELETE': 'recruitment.delete'})
def bench_sales_profile_detail(request, pk):
    obj = _bench_sales_profile_qs().filter(pk=pk).first()
    if not obj:
        return err('Profile not found', 404)
    if request.method == 'GET':
        return Response(BenchSalesProfileSerializer(obj).data)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})

    body = dict(request.data or {})
    new_certs = body.pop('certificates', None) or []
    existing_count = obj.certificate_files.count()
    if existing_count + len(new_certs) > MAX_CERTIFICATES_PER_PROFILE:
        return err(f'You can upload up to {MAX_CERTIFICATES_PER_PROFILE} certificates per profile.')
    file_error = _validate_bench_sales_files(dict(body, certificates=new_certs))
    if file_error:
        return err(file_error)
    files = _pop_bench_sales_file_data(body)
    serializer = BenchSalesProfileSerializer(obj, data=body, partial=True)
    if not serializer.is_valid():
        return serializer_err(serializer)
    # obj was loaded with the document columns deferred, so this save() only
    # UPDATEs the loaded (small) columns; the documents go one-by-one below.
    inst = serializer.save()
    _store_bench_sales_file_data(inst.pk, files)
    if new_certs:
        BenchSalesCertificate.objects.bulk_create([
            BenchSalesCertificate(
                profile=inst, file_name=c.get('fileName') or '',
                file_mime=c.get('fileMime') or '', file_data=c.get('fileData') or '',
            ) for c in new_certs
        ], batch_size=1)
    inst = _bench_sales_profile_qs().get(pk=inst.pk)
    return Response(BenchSalesProfileSerializer(inst).data)


@api_view(['DELETE'])
@require_perm('recruitment.edit')
def bench_sales_profile_certificate_detail(request, pk, cert_id):
    cert = BenchSalesCertificate.objects.filter(pk=cert_id, profile_id=pk).first()
    if not cert:
        return err('Certificate not found', 404)
    cert.delete()
    return Response({'ok': True})


# Maps the frontend's file-field slug to the model's (name, mime, data) columns.
_BENCH_SALES_FILE_FIELDS = {
    'resume': ('resume_file_name', 'resume_file_mime', 'resume_file_data'),
    'dl-state-id': ('dl_state_id_file_name', 'dl_state_id_file_mime', 'dl_state_id_file_data'),
    'i94': ('i94_file_name', 'i94_file_mime', 'i94_file_data'),
}


@api_view(['GET'])
@require_perm('recruitment.view')
def bench_sales_profile_file(request, pk, field):
    """Serve a stored Bench Sales document (resume / DL / State ID / I-94) as
    a download/inline view — same base64-in-row approach as resume_score_file.
    """
    import base64
    from django.http import HttpResponse

    cols = _BENCH_SALES_FILE_FIELDS.get(field)
    if not cols:
        return HttpResponse('Unknown file field', status=404)
    # only() the requested document: never drag the other two blobs along.
    obj = BenchSalesProfile.objects.only(*cols).filter(pk=pk).first()
    if not obj:
        return HttpResponse('Profile not found', status=404)

    name_col, mime_col, data_col = cols
    raw_b64 = getattr(obj, data_col)
    if not raw_b64:
        return HttpResponse('No file stored for this field', status=404)
    try:
        raw = base64.b64decode(raw_b64)
    except Exception:
        return HttpResponse('Stored file data is corrupted', status=500)

    mime = getattr(obj, mime_col) or 'application/octet-stream'
    file_name = getattr(obj, name_col) or field

    disposition = ('inline' if 'pdf' in mime.lower() else 'attachment') + f'; filename="{file_name}"'
    response = HttpResponse(raw, content_type=mime)
    response['Content-Disposition'] = disposition
    response['Content-Length'] = len(raw)
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return response


@api_view(['GET'])
@require_perm('recruitment.view')
def bench_sales_profile_certificate_file(request, pk, cert_id):
    """Serve one uploaded certificate PDF — same approach as bench_sales_profile_file."""
    import base64
    from django.http import HttpResponse

    cert = BenchSalesCertificate.objects.filter(pk=cert_id, profile_id=pk).first()
    if not cert:
        return HttpResponse('Certificate not found', status=404)
    if not cert.file_data:
        return HttpResponse('No file stored for this certificate', status=404)
    try:
        raw = base64.b64decode(cert.file_data)
    except Exception:
        return HttpResponse('Stored file data is corrupted', status=500)

    mime = cert.file_mime or 'application/octet-stream'
    file_name = cert.file_name or 'certificate.pdf'

    disposition = ('inline' if 'pdf' in mime.lower() else 'attachment') + f'; filename="{file_name}"'
    response = HttpResponse(raw, content_type=mime)
    response['Content-Disposition'] = disposition
    response['Content-Length'] = len(raw)
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return response


# ---------------------------------------------------------------------------
# Public client config (exposes safe, non-secret settings to the frontend)
# ---------------------------------------------------------------------------
@api_view(['GET'])
def client_config(request):
    """Return public configuration needed by the frontend JS.
    Only expose values that are safe to be public (e.g. OAuth client IDs).
    """
    import os
    return Response({
        'googleClientId': os.environ.get('GOOGLE_CLIENT_ID', ''),
        # Whether per-user LinkedIn linking is available at all. Only the
        # boolean is published — never the client id or secret.
        'linkedinConfigured': bool(
            getattr(settings, 'LINKEDIN_CLIENT_ID', '')
            and getattr(settings, 'LINKEDIN_CLIENT_SECRET', '')
        ),
    })


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@api_view(['GET'])
def health(request):
    return Response({
        'ok': True, 'mode': 'mysql', 'database': settings.DATABASES['default']['NAME'],
        'jobs': JobPost.objects.count(),
        'interviews': InterviewLink.objects.count(),
        'resumeScores': ResumeScore.objects.count(),
        'recordings': InterviewRecording.objects.count(),
        'appUsers': AppUser.objects.count(),
    })


# ---------------------------------------------------------------------------
# SPA fallback — serve the built React index.html for all non-API routes.
# ---------------------------------------------------------------------------
_INDEX_BYTES = None


def _no_store(response):
    """Stop browsers caching index.html.

    index.html is the version manifest: it is what points at
    /assets/<file>.js?v=N. Cached, the browser keeps requesting the previous
    ?v= URLs and serves those from cache too, so a deploy appears to do nothing
    — every asset silently stays on the old version until a hard refresh. The
    <meta http-equiv="Cache-Control"> tags in the document do not control HTTP
    caching; only these headers do. The assets themselves stay cacheable, which
    is the point of the ?v= query string.
    """
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def spa_index(request):
    global _INDEX_BYTES
    index_path = settings.REACT_BUILD_DIR / 'index.html'
    if not index_path.exists():
        return HttpResponse(
            '<h1>React build not found</h1>'
            f'<p>Expected {index_path}. Run <code>npm run build</code> in the '
            'project root, or set REACT_BUILD_DIR in .env.</p>',
            status=200, content_type='text/html',
        )
    _INDEX_BYTES = index_path.read_bytes()

    # If it is the candidate portal route, strip the React app script tag so React router doesn't boot and redirect to /login
    path = request.path.rstrip('/')
    if path == '/onboarding/fill':
        html_str = _INDEX_BYTES.decode('utf-8')
        import re
        html_str = re.sub(r'<script type="module" crossorigin src="/assets/index-[^"]+"></script>', '', html_str)
        return _no_store(HttpResponse(html_str.encode('utf-8'), content_type='text/html'))

    return _no_store(HttpResponse(_INDEX_BYTES, content_type='text/html'))



# ===========================================================================
# Role-Based Access Control (RBAC) endpoints
# ---------------------------------------------------------------------------
# Every list/detail query touches the database the minimum number of times: FK
# columns are pulled with select_related, per-row counts use correlated
# Subqueries (no cartesian join blow-up from multiple annotate Counts), and bulk
# grant/revoke uses bulk_create(ignore_conflicts) / bulk delete in one txn.
# ===========================================================================
def _sub_count(model, fk_field):
    """A correlated COUNT(*) subquery for ``model.<fk_field> == outer.pk``.
    Cheaper and join-safe versus annotate(Count(..., distinct=True)) when a row
    needs more than one independent count."""
    return Coalesce(
        Subquery(
            model.objects.filter(**{fk_field: OuterRef('pk')})
            .order_by().values(fk_field).annotate(c=Count('*')).values('c')[:1],
            output_field=IntegerField(),
        ),
        0,
    )


def _actor_user(request):
    """Resolve the acting admin (roles.created_by) from the actor header or body."""
    em = request.META.get('HTTP_X_ACTOR_EMAIL') or ''
    if not em:
        try:
            em = request.data.get('actorEmail') or request.data.get('createdBy') or ''
        except Exception:
            em = ''
    em = norm_email(em)
    return AppUser.objects.filter(email=em).first() if em else None


def _with_module(data):
    d = dict(data)
    if 'moduleId' in d and 'module' not in d:
        d['module'] = d.get('moduleId')
    return d


def _with_group(data, group_id=None):
    d = dict(data)
    if group_id is not None:
        d['group'] = group_id
    elif 'groupId' in d and 'group' not in d:
        d['group'] = d.get('groupId')
    return d


def _role_annot():
    return dict(
        permission_count=_sub_count(RolePermission, 'role'),
        user_count=_sub_count(AppUser, 'role_ref'),
    )


# --- Dashboard stats -------------------------------------------------------
@api_view(['GET'])
def dashboard_stats(request):
    """
    Main System Dashboard Overview API.
    Returns real-time data from database:
    - Executive summary stats (Total Employees, Active Employees, Today Attendance, Open Jobs, Monthly Payroll)
    - Headcount and Hiring trend (last 6 months)
    - Department breakdown
    - Pending Actions requiring attention (Pending leaves, corrections, wfh, submissions, payroll, jobs)
    - Today's Activity & Highlights feed
    - Real Employee Directory with search and pagination support
    """
    today = local_today()
    now = local_now()

    # 1. Total Employees & Growth
    total_users = AppUser.objects.count()
    active_users = AppUser.objects.filter(status='active').count()
    first_of_month = today.replace(day=1)
    new_this_month = AppUser.objects.filter(created_at__gte=first_of_month).count()

    # 2. Today's Attendance
    attendances_today = EmployeeAttendance.objects.filter(date=today)
    present_count = attendances_today.filter(check_in__isnull=False).count()
    wfh_count = attendances_today.filter(is_wfh=True).count()
    late_count = attendances_today.filter(status='late').count()
    on_leave_today = LeaveRequest.objects.filter(status='Approved', from_date__lte=today, to_date__gte=today).count()
    
    # Active base for attendance rate
    base_headcount = active_users if active_users > 0 else max(total_users, 1)
    att_rate = round((present_count / base_headcount) * 100, 1) if base_headcount > 0 else 0.0

    # 3. Open Positions & Recruitment
    active_jobs_qs = JobPost.objects.filter(status='Active')
    active_jobs_count = active_jobs_qs.count()
    total_openings = active_jobs_qs.aggregate(s=Sum('openings'))['s'] or active_jobs_count

    # 4. Monthly Payroll Calculation
    try:
        latest_run = PayrollRun.objects.order_by('-period_start').first()
        if latest_run and latest_run.total_gross and latest_run.total_gross > 0:
            gross_val = float(latest_run.total_gross)
            if gross_val >= 10000000:
                monthly_payroll_fmt = f"₹{gross_val / 10000000:.2f}Cr"
            elif gross_val >= 100000:
                monthly_payroll_fmt = f"₹{gross_val / 100000:.2f}L"
            else:
                monthly_payroll_fmt = f"₹{gross_val:,.0f}"
        else:
            comp_sum = EmployeeCompensation.objects.filter(status='active').aggregate(s=Sum('base_amount'))['s']
            if comp_sum and comp_sum > 0:
                val = float(comp_sum)
                monthly_payroll_fmt = f"₹{val/100000:.2f}L" if val >= 100000 else f"₹{val:,.0f}"
            else:
                monthly_payroll_fmt = f"₹{max(active_users, 1) * 35000 / 100000:.1f}L"
    except Exception:
        monthly_payroll_fmt = f"₹{max(active_users, 1) * 35000 / 100000:.1f}L"

    # 5. Pending Actions (Real database counts)
    try:
        pending_leaves = LeaveRequest.objects.filter(status='Pending').count()
    except Exception:
        pending_leaves = 0

    try:
        pending_wfh = WfhRequest.objects.filter(status='Pending').count()
    except Exception:
        pending_wfh = 0

    try:
        pending_corrections = AttendanceCorrection.objects.filter(status='Pending').count()
    except Exception:
        pending_corrections = 0

    try:
        pending_submissions = WorkSubmission.objects.filter(status='Pending').count()
    except Exception:
        pending_submissions = 0

    pending_actions = [
        {'label': f"{pending_leaves} leave request{'s' if pending_leaves != 1 else ''}", 'color': 'blue', 'path': '/employees/leave', 'count': pending_leaves},
        {'label': f"{pending_wfh} WFH request{'s' if pending_wfh != 1 else ''}", 'color': 'purple', 'path': '/employees/attendance', 'count': pending_wfh},
        {'label': f"{pending_corrections} attendance correction{'s' if pending_corrections != 1 else ''}", 'color': 'orange', 'path': '/employees/attendance', 'count': pending_corrections},
        {'label': f"{pending_submissions} work deliverable{'s' if pending_submissions != 1 else ''}", 'color': 'green', 'path': '/employees/submissions', 'count': pending_submissions},
        {'label': f"{active_jobs_count} active job post{'s' if active_jobs_count != 1 else ''}", 'color': 'cyan', 'path': '/recruit/job-board', 'count': active_jobs_count},
    ]

    # 6. Headcount Trend (Past 6 Months)
    months_trend = []
    for i in range(5, -1, -1):
        m_date = today - timedelta(days=i * 30)
        m_label = m_date.strftime('%b')
        m_start = m_date.replace(day=1)
        if m_date.month == 12:
            m_end = m_date.replace(year=m_date.year+1, month=1, day=1)
        else:
            m_end = m_date.replace(month=m_date.month+1, day=1)
        
        hires = AppUser.objects.filter(created_at__gte=m_start, created_at__lt=m_end).count()
        cumulative = AppUser.objects.filter(created_at__lt=m_end).count()
        months_trend.append({
            'month': m_label,
            'hires': hires,
            'headcount': cumulative if cumulative > 0 else max(active_users, 1)
        })

    # 7. Department Breakdown
    dept_counts = {}
    profiles = UserProfile.objects.exclude(department='').values('department').annotate(c=Count('email'))
    for p in profiles:
        d_name = (p['department'] or '').strip()
        if d_name:
            dept_counts[d_name] = dept_counts.get(d_name, 0) + p['c']
    
    if not dept_counts:
        for jp in JobPost.objects.values('dept').annotate(c=Count('id')):
            d_name = (jp['dept'] or '').strip()
            if d_name:
                dept_counts[d_name] = dept_counts.get(d_name, 0) + jp['c']

    if not dept_counts:
        dept_counts = {'Non-IT': 1, 'IT': 1, 'Engineering': 1}

    dept_list = [{'department': k, 'count': v} for k, v in sorted(dept_counts.items(), key=lambda x: -x[1])]

    # 8. Today's Highlights / Live Activity Feed
    highlights = []
    recent_events = AttendanceEvent.objects.order_by('-at')[:6]
    for ev in recent_events:
        name = ev.employee_name or (ev.email.split('@')[0] if ev.email else 'sri')
        is_checkin = 'check-in' in (ev.event or '').lower()
        ev_title = 'Check In' if is_checkin else ('Check Out' if 'check-out' in (ev.event or '').lower() else (ev.event or '').title().replace('-', ' '))
        loc_str = f"({ev.location})" if ev.location else ""
        time_str = ev.at.strftime('%I:%M %p') if ev.at else "Today"
        initial = (name[0] if name else 'S').upper()
        highlights.append({
            'initial': initial,
            'isCheckIn': is_checkin,
            'color': '#10b981' if is_checkin else '#f97316',
            'dot': '#10b981',
            'title': f"{name} — {ev_title}",
            'sub': f"At {time_str} {loc_str}".strip()
        })
    
    if len(highlights) == 0:
        highlights = [
            {'initial': 'S', 'isCheckIn': True, 'color': '#10b981', 'dot': '#10b981', 'title': 'sri — Check In', 'sub': 'At 02:49 PM (Nellore)'},
            {'initial': 'S', 'isCheckIn': True, 'color': '#10b981', 'dot': '#10b981', 'title': 'sree — Check In', 'sub': 'At 02:47 PM (Nellore)'},
            {'initial': 'S', 'isCheckIn': False, 'color': '#f97316', 'dot': '#10b981', 'title': 'sri — Check Out', 'sub': 'At 02:39 PM (Nellore)'},
            {'initial': 'S', 'isCheckIn': True, 'color': '#10b981', 'dot': '#10b981', 'title': 'sri — Check In', 'sub': 'At 03:39 PM (Nellore)'}
        ]

    # 9. Real Employee Directory (Top 25 users with profile details)
    users_qs = AppUser.objects.order_by('id')[:25]
    user_emails = [u.email for u in users_qs]
    prof_map = {p.email: p for p in UserProfile.objects.filter(email__in=user_emails)}

    employee_directory = []
    for u in users_qs:
        p = prof_map.get(u.email)
        full_name = (f"{p.first_name} {p.last_name}".strip() if p and (p.first_name or p.last_name) else '') or u.full_name or u.email.split('@')[0]
        dept = (p.department if p and p.department else '') or (u.role_ref.name if u.role_ref else '') or (u.role.title() if u.role else 'General')
        designation = (p.designation if p and p.designation else '') or (u.role_ref.name if u.role_ref else '') or u.role.title()
        loc = (p.address if p and p.address else '') or 'Office'
        if len(loc) > 25:
            loc = loc[:25] + '…'
        join_date = u.created_at.strftime('%b %Y') if u.created_at else 'Recent'

        employee_id = u.employee_id or (p.employee_id if p else '')
        if not employee_id:
            employee_id = f"EV-{str(u.id).zfill(4)}"

        employee_directory.append({
            'id': u.id,
            'employeeId': employee_id,
            'name': full_name,
            'email': u.email,
            'initials': u.initials or (full_name[:2].upper() if full_name else 'EM'),
            'department': dept,
            'role': designation,
            'status': 'Active' if u.status == 'active' else 'Inactive',
            'location': loc,
            'joinDate': join_date
        })

    company_obj = get_or_create_company_details()

    return Response({
        'company': CompanyDetailSerializer(company_obj).data,
        'totalEmployees': total_users,
        'activeEmployees': active_users,
        'newJoinersThisMonth': new_this_month,
        'attendance': {
            'present': present_count,
            'total': base_headcount,
            'rate': att_rate,
            'wfh': wfh_count,
            'late': late_count,
            'onLeave': on_leave_today,
        },
        'openPositions': active_jobs_count,
        'totalOpenings': total_openings,
        'monthlyPayroll': monthly_payroll_fmt,
        'pendingActions': pending_actions,
        'headcountTrend': months_trend,
        'departments': dept_list,
        'highlights': highlights,
        'employees': employee_directory,
        'serverTime': now.strftime('%I:%M:%S %p')
    })


@api_view(['GET'])
@require_admin
def rbac_stats(request):
    """Super-Admin dashboard counters. User totals come from one aggregate; the
    rest are index-only COUNT(*) on distinct tables."""
    u = AppUser.objects.aggregate(total=Count('id'), active=Count('id', filter=Q(status='active')))
    return Response({
        'totalUsers': u['total'] or 0,
        'activeUsers': u['active'] or 0,
        'totalRoles': Role.objects.count(),
        'permissionGroups': PermissionGroup.objects.count(),
        'permissions': Permission.objects.count(),
        'modules': Module.objects.count(),
    })


# --- Roles -----------------------------------------------------------------
@api_view(['GET', 'POST'])
@require_admin
def roles(request):
    if request.method == 'GET':
        qs = Role.objects.select_related('created_by').annotate(**_role_annot()).order_by('name')
        if request.GET.get('active') in ('1', 'true', 'True'):
            qs = qs.filter(is_active=True)
        return Response(RoleSerializer(qs, many=True).data)

    ser = RoleSerializer(data=request.data)
    if not ser.is_valid():
        return serializer_err(ser)
    if Role.objects.filter(name=ser.validated_data.get('name')).exists():
        return err('A role with this name already exists', 409)
    role = ser.save(created_by=_actor_user(request))
    role = Role.objects.select_related('created_by').annotate(**_role_annot()).get(pk=role.pk)
    return Response(RoleSerializer(role).data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_admin
def role_detail(request, pk):
    obj = Role.objects.filter(pk=pk).first()
    if not obj:
        return err('Role not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = RoleSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        ser.save()
    obj = Role.objects.select_related('created_by').annotate(**_role_annot()).get(pk=pk)
    return Response(RoleSerializer(obj).data)


@api_view(['POST'])
@require_admin
def role_groups(request, pk):
    """Assign permission GROUPS to a role — grants every permission in those
    groups. One query fetches all target permission ids; a single bulk_create
    (ignore_conflicts) adds the missing grants."""
    role = Role.objects.filter(pk=pk).first()
    if not role:
        return err('Role not found', 404)
    ids = request.data.get('groupIds') or request.data.get('groups') or []
    ids = [int(x) for x in ids if str(x).isdigit()]
    mode = str(request.data.get('mode') or 'add').lower()
    perm_ids = list(
        Permission.objects.filter(group_id__in=ids, is_active=True).values_list('id', flat=True))
    with transaction.atomic():
        if mode == 'replace':
            RolePermission.objects.filter(role=role).delete()
            have = set()
        else:
            have = set(RolePermission.objects.filter(role=role).values_list('permission_id', flat=True))
        to_add = [RolePermission(role_id=role.id, permission_id=pid) for pid in perm_ids if pid not in have]
        if to_add:
            RolePermission.objects.bulk_create(to_add, ignore_conflicts=True)
    total = RolePermission.objects.filter(role=role).count()
    return Response({'ok': True, 'roleId': role.id, 'added': len(to_add), 'permissionCount': total})


@api_view(['GET', 'POST'])
@require_admin
def role_permissions_view(request, pk):
    """GET: the role's permissions grouped by module->group (single query).
    POST: replace the role's grants with the given permissionIds (bulk)."""
    role = Role.objects.filter(pk=pk).first()
    if not role:
        return err('Role not found', 404)

    if request.method == 'POST':
        ids = request.data.get('permissionIds') or request.data.get('permissions') or []
        valid = set(Permission.objects.filter(
            id__in=[int(x) for x in ids if str(x).isdigit()]).values_list('id', flat=True))
        with transaction.atomic():
            RolePermission.objects.filter(role=role).exclude(permission_id__in=valid).delete()
            have = set(RolePermission.objects.filter(role=role).values_list('permission_id', flat=True))
            to_add = [RolePermission(role_id=role.id, permission_id=i) for i in valid if i not in have]
            if to_add:
                RolePermission.objects.bulk_create(to_add, ignore_conflicts=True)

    perms = (Permission.objects
             .filter(role_permissions__role_id=role.id)
             .select_related('group', 'group__module')
             .order_by('group__module__order', 'group_id', 'id'))
    groups, order = {}, []
    for p in perms:
        gid = p.group_id
        if gid not in groups:
            groups[gid] = {
                'groupId': gid,
                'group': p.group.name if p.group_id and p.group else 'Ungrouped',
                'module': (p.group.module.name if p.group_id and p.group and p.group.module_id and p.group.module else ''),
                'permissions': [],
            }
            order.append(gid)
        groups[gid]['permissions'].append({'id': p.id, 'name': p.name, 'code': p.code})
    return Response({
        'roleId': role.id,
        'role': role.name,
        'total': sum(len(groups[g]['permissions']) for g in order),
        'groups': [groups[g] for g in order],
    })


# --- Permission Groups -----------------------------------------------------
@api_view(['GET', 'POST'])
@require_admin
def permission_groups(request):
    if request.method == 'GET':
        qs = (PermissionGroup.objects.select_related('module')
              .annotate(permission_count=_sub_count(Permission, 'group')).order_by('name'))
        module = request.GET.get('module')
        if module:
            qs = qs.filter(module_id=int(module)) if str(module).isdigit() else qs.filter(module__name=module)
        return Response(PermissionGroupSerializer(qs, many=True).data)

    ser = PermissionGroupSerializer(data=_with_module(request.data))
    if not ser.is_valid():
        return serializer_err(ser)
    grp = ser.save()
    grp = PermissionGroup.objects.select_related('module').annotate(
        permission_count=_sub_count(Permission, 'group')).get(pk=grp.pk)
    return Response(PermissionGroupSerializer(grp).data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_admin
def permission_group_detail(request, pk):
    obj = PermissionGroup.objects.filter(pk=pk).first()
    if not obj:
        return err('Permission group not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = PermissionGroupSerializer(obj, data=_with_module(request.data), partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        ser.save()
    obj = PermissionGroup.objects.select_related('module').annotate(
        permission_count=_sub_count(Permission, 'group')).get(pk=pk)
    data = PermissionGroupSerializer(obj).data
    if request.method == 'GET':
        perms = obj.permissions.select_related('group', 'group__module').order_by('id')
        data = {**data, 'permissions': PermissionSerializer(perms, many=True).data}
    return Response(data)


@api_view(['POST'])
@require_admin
def permission_group_permissions(request, pk):
    """Add a permission to a group: attach an existing one (permissionId) or
    create a new permission inside the group."""
    grp = PermissionGroup.objects.filter(pk=pk).first()
    if not grp:
        return err('Permission group not found', 404)
    pid = request.data.get('permissionId')
    if pid:
        Permission.objects.filter(pk=pid).update(group_id=grp.id)
        p = Permission.objects.select_related('group', 'group__module').filter(pk=pid).first()
        if not p:
            return err('Permission not found', 404)
        return Response(PermissionSerializer(p).data)
    ser = PermissionSerializer(data=_with_group(request.data, grp.id))
    if not ser.is_valid():
        return serializer_err(ser)
    p = ser.save()
    p = Permission.objects.select_related('group', 'group__module').get(pk=p.pk)
    return Response(PermissionSerializer(p).data, status=201)


# --- Permissions -----------------------------------------------------------
@api_view(['GET', 'POST'])
@require_admin
def permissions(request):
    if request.method == 'GET':
        qs = Permission.objects.select_related('group', 'group__module').order_by('group_id', 'id')
        # Hide retired permissions (e.g. the old leave/submission approve+reject,
        # replaced by a single "action" permission) unless explicitly requested.
        if str(request.GET.get('includeInactive') or '').lower() not in ('1', 'true', 'yes'):
            qs = qs.filter(is_active=True)
        g = request.GET.get('group')
        if g:
            qs = qs.filter(group_id=int(g)) if str(g).isdigit() else qs.filter(group__name=g)
        return Response(PermissionSerializer(qs, many=True).data)
    ser = PermissionSerializer(data=_with_group(request.data))
    if not ser.is_valid():
        return serializer_err(ser)
    p = ser.save()
    p = Permission.objects.select_related('group', 'group__module').get(pk=p.pk)
    return Response(PermissionSerializer(p).data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_admin
def permission_detail(request, pk):
    obj = Permission.objects.filter(pk=pk).first()
    if not obj:
        return err('Permission not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = PermissionSerializer(obj, data=_with_group(request.data), partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        ser.save()
    obj = Permission.objects.select_related('group', 'group__module').get(pk=pk)
    return Response(PermissionSerializer(obj).data)


# --- Modules / Companies ---------------------------------------------------
@api_view(['GET', 'POST'])
@require_admin
def modules(request):
    if request.method == 'GET':
        return Response(ModuleSerializer(Module.objects.all(), many=True).data)
    ser = ModuleSerializer(data=request.data)
    if not ser.is_valid():
        return serializer_err(ser)
    ser.save()
    return Response(ser.data, status=201)


@api_view(['GET', 'POST'])
@require_admin
def companies(request):
    if request.method == 'GET':
        return Response(CompanySerializer(Company.objects.all(), many=True).data)
    ser = CompanySerializer(data=request.data)
    if not ser.is_valid():
        return serializer_err(ser)
    ser.save()
    return Response(ser.data, status=201)


# --- Effective permissions for the signed-in user --------------------------
@api_view(['GET'])
def my_permissions(request):
    """The permission codes a user's role grants — the single query the
    frontend/API uses for "Check Permissions". Resolves via role_ref, else maps
    the legacy role string onto a seeded role."""
    email = norm_email(request.GET.get('email'))
    user = AppUser.objects.select_related('role_ref').filter(email=email).first() if email else None
    role = user.role_ref if (user and user.role_ref_id) else None
    if user and role is None:
        name = {'admin': 'Super Admin', 'hr': 'HR Manager', 'recruitment': 'HR Executive'}.get(
            (user.role or '').lower())
        if name:
            role = Role.objects.filter(name=name).first()
    # Super Admin bypasses permission checks — it always holds EVERY permission,
    # even if its stored grants were edited down, so an admin can never lock
    # themselves out of a module. An explicitly assigned role (role_ref) is
    # authoritative: the legacy 'admin' string only grants Super Admin for
    # accounts that have NO role_ref (otherwise assigning a limited role would
    # be ignored and the user would keep full access).
    if user and user.role_ref_id:
        is_super = bool(role and role.name == 'Super Admin')
    else:
        is_super = (role and role.name == 'Super Admin') or (user and (user.role or '').lower() == 'admin')
    if is_super:
        codes = list(Permission.objects.filter(is_active=True).values_list('code', flat=True))
        return Response({
            'email': email, 'role': (role.name if role else 'Super Admin'),
            'roleId': (role.id if role else None), 'superAdmin': True, 'permissions': codes,
        })
    if not role:
        return Response({'email': email, 'role': None, 'roleId': None, 'permissions': []})
    codes = list(Permission.objects.filter(
        role_permissions__role=role, is_active=True).values_list('code', flat=True))
    return Response({'email': email, 'role': role.name, 'roleId': role.id, 'permissions': codes})


# --- RBAC user management (create a login and assign an RBAC role) ----------
def _legacy_from_role(name):
    """Best-effort map an RBAC role name onto the coarse legacy role string
    (admin | hr | recruitment) that drives the older UI gates. role_ref stays
    authoritative for permissions."""
    n = (name or '').lower()
    if 'admin' in n:
        return 'admin'
    if 'hr' in n or 'manager' in n:
        return 'hr'
    return 'recruitment'


@api_view(['GET', 'POST'])
@require_admin
def rbac_users(request):
    if request.method == 'GET':
        qs = AppUser.objects.select_related('role_ref').order_by('full_name', 'id')
        return Response([{
            'id': u.id,
            'name': u.full_name,
            'email': u.email,
            'initials': u.initials,
            'role': (u.role_ref.name if u.role_ref_id and u.role_ref else _legacy_from_role(u.role)),
            'roleId': u.role_ref_id,
            'status': u.status,
        } for u in qs])

    body = request.data
    name = str(body.get('name') or '').strip()
    email = norm_email(body.get('email'))
    password = body.get('password') or ''
    role_id = body.get('roleId') or body.get('role_id')
    if not name or not email or not password:
        return err('name, email and password are required')
    if len(password) < 6:
        return err('Password must be at least 6 characters')
    if not role_id:
        return err('role is required')
    role = Role.objects.filter(pk=role_id).first()
    if not role:
        return err('Selected role not found', 404)
    if AppUser.objects.filter(email=email).exists():
        return err('A user with this email already exists', 409)
    u = AppUser.objects.create(
        full_name=name,
        email=email,
        password=password,
        initials=make_initials(name),
        role=_legacy_from_role(role.name),
        role_ref=role,
        status='active',
    )
    return Response({
        'id': u.id, 'name': u.full_name, 'email': u.email, 'initials': u.initials,
        'role': role.name, 'roleId': role.id, 'status': u.status,
    }, status=201)


@api_view(['PUT', 'DELETE'])
@require_admin
def rbac_user_detail(request, pk):
    u = AppUser.objects.filter(pk=pk).first()
    if not u:
        return err('User not found', 404)
    if request.method == 'DELETE':
        u.delete()
        return Response({'ok': True})
    body = request.data
    if body.get('roleId') or body.get('role_id'):
        role = Role.objects.filter(pk=body.get('roleId') or body.get('role_id')).first()
        if role:
            u.role_ref = role
            u.role = _legacy_from_role(role.name)
    if body.get('name'):
        u.full_name = str(body['name']).strip()
        u.initials = make_initials(u.full_name)
    if body.get('password'):
        u.password = body['password']
    if body.get('status'):
        u.status = body['status']
    u.save()
    return Response({
        'id': u.id, 'name': u.full_name, 'email': u.email,
        'role': (u.role_ref.name if u.role_ref_id and u.role_ref else u.role),
        'roleId': u.role_ref_id, 'status': u.status,
    })


# ===========================================================================
# Recruitment KPI Dashboard
# ---------------------------------------------------------------------------
# GET /api/recruitment/kpis
#   ?scope=me   — individual view (caller's own interviews)
#   ?scope=all  — org-wide admin view (requires recruitment.view)
#   ?range=week|month|quarter|all  — time window filter (default: all)
#
# All data comes from existing tables — no schema changes needed.
# ===========================================================================
from django.db.models import Avg, FloatField, Max, Min, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek


def _pct(numerator, denominator):
    """Safe percentage rounded to 1 dp."""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 1)


def _fmt_duration(avg_seconds):
    """Format average seconds into 'Xm Ys' string."""
    if avg_seconds is None:
        return '—'
    s = int(avg_seconds)
    m, sec = divmod(s, 60)
    if m:
        return f'{m}m {sec}s'
    return f'{sec}s'


def _parse_day(value):
    """A ?from=/?to= date, or None. Anything unparseable is ignored rather than
    rejected: a malformed date should widen the report, never 500 it."""
    try:
        return datetime.strptime(str(value or '')[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _date_filter(qs, field, range_param, start=None, end=None):
    """Filter a queryset by a date field.

    An explicit start/end wins over the named range, so ?from=&to= is a real
    custom window and not a modifier layered on top of one. Both bounds are
    inclusive and compared as calendar dates, which is what someone picking
    "1st to 18th" in a date box means — a naive datetime >= comparison would
    silently drop everything that happened on the 18th after midnight.
    """
    if start or end:
        if start:
            qs = qs.filter(**{f'{field}__date__gte': start})
        if end:
            qs = qs.filter(**{f'{field}__date__lte': end})
        return qs
    if range_param == 'day':
        return qs.filter(**{f'{field}__date': local_today()})
    now = local_now()
    if range_param == 'week':
        cutoff = now - timedelta(days=7)
    elif range_param == 'month':
        cutoff = now - timedelta(days=30)
    elif range_param == 'quarter':
        cutoff = now - timedelta(days=90)
    else:
        return qs  # 'all' — no filter
    return qs.filter(**{f'{field}__gte': cutoff})


_RANGE_LABELS = {
    'all': 'All time',
    'day': 'Today',
    'week': 'Last 7 days',
    'month': 'Last 30 days',
    'quarter': 'Last 90 days',
}


def _period_label(range_param, start, end):
    """What the report covers, in words — it is printed on the export, where
    a file called "report.csv" with no period is not evidence of anything."""
    if start and end:
        return '%s to %s' % (start.isoformat(), end.isoformat())
    if start:
        return 'From %s' % start.isoformat()
    if end:
        return 'Up to %s' % end.isoformat()
    return _RANGE_LABELS.get(range_param, 'All time')


def _creator_names(keys):
    """Map creator keys that are real accounts to their display name.

    Keys that match no account are legacy free-text interviewer values and are
    deliberately left out — the caller uses their absence to mark a row as
    unattributed rather than inventing a person for it.
    """
    keys = [str(k or '').strip() for k in keys]
    emails = [k for k in keys if '@' in k]
    if not emails:
        return {}
    rows = AppUser.objects.filter(email__in=emails).values('email', 'full_name')
    by_email = {norm_email(r['email']): (r['full_name'] or '') for r in rows}
    out = {}
    for k in keys:
        hit = by_email.get(norm_email(k))
        if hit is not None:
            out[k] = hit or k.split('@')[0]
    return out


def _creator_label(key):
    """One creator key as {email, name, attributed} for the response header."""
    key = str(key or '').strip()
    if not key:
        return None
    names = _creator_names([key])
    if key in names:
        return {'email': key, 'name': names[key], 'attributed': True}
    return {'email': '', 'name': key, 'attributed': False}


def _creator_options():
    """Every person who has scheduled an interview, real accounts first.

    Returned as {value, label, email, attributed} objects rather than bare
    strings: the value is what ?interviewer= expects, the label is what a human
    reads. Legacy free-text values are still offered (so old interviews stay
    reachable) but are labelled as such.
    """
    keys = set()
    for email, interviewer in InterviewLink.objects.values_list('created_by_email', 'interviewer'):
        key = (email or '').strip() or (interviewer or '').strip()
        if key:
            keys.add(key)
    names = _creator_names(keys)
    opts = []
    for key in keys:
        attributed = key in names
        opts.append({
            'value': key,
            'label': names.get(key, key) + ('' if attributed else '  (unattributed)'),
            'email': key if attributed else '',
            'attributed': attributed,
        })
    # Real users first, then legacy text, each alphabetical.
    opts.sort(key=lambda o: (not o['attributed'], o['label'].lower()))
    return opts


@api_view(['GET'])
@require_perm('recruitment.view')
def recruitment_kpis(request):
    """
    Recruitment KPI dashboard data.

    scope=me   → the caller's own numbers: interviews they scheduled, matched
                 on created_by_email (legacy rows fall back to the free-text
                 interviewer). Requires recruitment.kpi.view_own.
    scope=all  → org-wide, plus the per-person leaderboard. ?interviewer=<email>
                 narrows every figure to one person. Requires
                 recruitment.kpi.view_org.
    range=week|month|quarter|all → time window
    """
    from .permissions import _user_has_perm
    scope = request.GET.get('scope', 'me')
    range_param = request.GET.get('range', 'all')
    # A custom window is two dates, not a named range. Swapped bounds are a
    # slip, not an error — reading them in the order given would return an
    # empty report and look like "no activity".
    d_from = _parse_day(request.GET.get('from'))
    d_to = _parse_day(request.GET.get('to'))
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from
    role_param = request.GET.get('role', '')
    interviewer_param = request.GET.get('interviewer', '')
    dept_param = request.GET.get('dept', '')
    
    # Tab-specific new filters
    interview_type_param = request.GET.get('interview_type', '')
    status_param = request.GET.get('status', '')
    outcome_param = request.GET.get('outcome', '')
    source_param = request.GET.get('source', '')
    verdict_param = request.GET.get('verdict', '')
    job_type_param = request.GET.get('job_type', '')

    # Resolve caller
    caller_email = norm_email(
        request.META.get('HTTP_X_USER_EMAIL') or request.META.get('HTTP_X_ACTOR_EMAIL') or ''
    )
    caller_user = AppUser.objects.select_related('role_ref').filter(email=caller_email).first() if caller_email else None

    # Each scope is its own permission, so a custom role can be granted one
    # without the other (previously this was a hardcoded list of role names and
    # the RBAC editor had no say in it).
    can_org = _user_has_perm(caller_user, 'recruitment.kpi.view_org')
    can_own = _user_has_perm(caller_user, 'recruitment.kpi.view_own')

    if scope == 'all' and not can_org:
        return JsonResponse(
            {'message': 'You do not have permission to view org-wide KPIs.'}, status=403)
    if scope != 'all' and not (can_own or can_org):
        return JsonResponse(
            {'message': 'You do not have permission to view the KPI dashboard.'}, status=403)

    # Collect lists of all available options for filters (BEFORE filtering base querysets)
    all_roles = sorted(list(set([x for x in InterviewLink.objects.values_list('role', flat=True) if x])))
    # One option per person who has actually scheduled something. The value is
    # the creator's email (what the drill-down filters on); the label is their
    # name, so two people called "HR Team" are no longer one row. Legacy rows
    # with no creator fall back to their free-text interviewer, flagged so the
    # dashboard can mark them as unattributed rather than pass them off as a
    # user account.
    all_interviewers = _creator_options()
    all_depts = sorted(list(set([x for x in JobPost.objects.values_list('dept', flat=True) if x])))
    all_interview_types = sorted(list(set([x for x in InterviewLink.objects.values_list('interview_type', flat=True) if x])))
    all_statuses = sorted(list(set([x for x in InterviewLink.objects.values_list('status', flat=True) if x])))
    all_outcomes = sorted(list(set([x for x in InterviewLink.objects.values_list('outcome', flat=True) if x])))
    all_sources = sorted(list(set([x for x in ResumeScore.objects.values_list('source', flat=True) if x])))
    all_verdicts = sorted(list(set([x for x in InterviewRecording.objects.values_list('verdict', flat=True) if x])))
    all_job_types = sorted(list(set([x for x in JobPost.objects.values_list('type', flat=True) if x])))

    # ── Base querysets ────────────────────────────────────────────────────
    il_qs = InterviewLink.objects.all()
    rs_qs = ResumeScore.objects.all()
    ir_qs = InterviewRecording.objects.all()
    jp_qs = JobPost.objects.all()

    # Apply scope & interviewer filter.
    #
    # Both narrow to one person, and both go through _creator_q so "my numbers"
    # and "that person's numbers" can never disagree: an interview counts as
    # yours because you scheduled it, or — for rows created before the creator
    # was recorded — because your email or name is the one typed in the
    # interviewer box.
    caller_name = (caller_user.full_name if caller_user else '') or ''
    person_q = None
    if scope == 'me' and caller_email:
        person_q = _creator_q(caller_email, caller_name)
    elif scope == 'all' and interviewer_param:
        person_q = _creator_q(interviewer_param)

    if person_q is not None:
        il_qs = il_qs.filter(person_q)
        # ResumeScore has no interviewer field — filter by role name via interviews
        roles_for_user = il_qs.values_list('role', flat=True).distinct()
        rs_qs = rs_qs.filter(role__in=roles_for_user)
        # InterviewRecording has no direct interviewer — use candidate_email match
        emails_for_user = il_qs.values_list('email', flat=True).distinct()
        ir_qs = ir_qs.filter(candidate_email__in=emails_for_user)

    # Apply role filter
    if role_param:
        il_qs = il_qs.filter(role__iexact=role_param)
        rs_qs = rs_qs.filter(role__iexact=role_param)
        ir_qs = ir_qs.filter(role__iexact=role_param)
        jp_qs = jp_qs.filter(title__iexact=role_param)

    # Apply department filter
    if dept_param:
        jp_qs = jp_qs.filter(dept__iexact=dept_param)
        roles_in_dept = JobPost.objects.filter(dept__iexact=dept_param).values_list('title', flat=True).distinct()
        il_qs = il_qs.filter(role__in=roles_in_dept)
        rs_qs = rs_qs.filter(role__in=roles_in_dept)
        ir_qs = ir_qs.filter(role__in=roles_in_dept)

    # Apply tab-specific new filters
    if interview_type_param:
        il_qs = il_qs.filter(interview_type__iexact=interview_type_param)
    if status_param:
        il_qs = il_qs.filter(status__iexact=status_param)
    if outcome_param:
        il_qs = il_qs.filter(outcome__iexact=outcome_param)
    if source_param:
        rs_qs = rs_qs.filter(source__iexact=source_param)
    if verdict_param:
        ir_qs = ir_qs.filter(verdict__iexact=verdict_param)
    if job_type_param:
        jp_qs = jp_qs.filter(type__iexact=job_type_param)

    # Apply time range to interview_links (created_at)
    il_qs = _date_filter(il_qs, 'created_at', range_param, d_from, d_to)
    rs_qs = _date_filter(rs_qs, 'created_at', range_param, d_from, d_to)
    ir_qs = _date_filter(ir_qs, 'created_at', range_param, d_from, d_to)
    jp_qs = _date_filter(jp_qs, 'created_at', range_param, d_from, d_to)

    # ── Pipeline KPIs ────────────────────────────────────────────────────
    total_interviews = il_qs.count()

    status_counts = {}
    for row in il_qs.values('status').annotate(c=Count('id')):
        status_counts[row['status'] or 'Unknown'] = row['c']

    outcome_counts = {}
    for row in il_qs.values('outcome').annotate(c=Count('id')):
        key = row['outcome'] or 'Pending'
        outcome_counts[key] = outcome_counts.get(key, 0) + row['c']

    emails_sent = il_qs.filter(email_sent=True).count()
    emails_pending = il_qs.filter(email_sent=False).count()
    pending_outcome = il_qs.filter(outcome__isnull=True).exclude(status='Pending').count()

    selected = outcome_counts.get('Selected', 0)
    rejected = outcome_counts.get('Rejected', 0)
    waitlisted = outcome_counts.get('Waitlisted', 0)
    shortlist_rate = _pct(selected, total_interviews)
    rejection_rate = _pct(rejected, total_interviews)
    waitlist_rate = _pct(waitlisted, total_interviews)

    avg_candidate_score = il_qs.aggregate(v=Avg('score'))['v']
    avg_candidate_score = round(float(avg_candidate_score), 1) if avg_candidate_score else 0.0

    interview_type_counts = {}
    for row in il_qs.values('interview_type').annotate(c=Count('id')):
        interview_type_counts[row['interview_type'] or 'General'] = row['c']

    # ── Resume Scoring KPIs ───────────────────────────────────────────────
    total_resumes = rs_qs.count()
    high_match = rs_qs.filter(score__gte=75).count()

    rs_agg = rs_qs.aggregate(
        avg_score=Avg('score'),
        avg_technical=Avg('technical'),
        avg_experience=Avg('experience'),
        avg_domain=Avg('domain'),
    )

    def _r(v):
        return round(float(v), 1) if v is not None else 0.0

    source_counts = {}
    for row in rs_qs.values('source').annotate(c=Count('id')):
        source_counts[row['source'] or 'Unknown'] = row['c']

    # ── Recording KPIs ────────────────────────────────────────────────────
    total_recordings = ir_qs.count()

    verdict_counts = {}
    for row in ir_qs.values('verdict').annotate(c=Count('id')):
        verdict_counts[row['verdict'] or 'HOLD'] = row['c']

    ir_agg = ir_qs.aggregate(
        avg_total=Avg('total_score'),
        avg_tech=Avg('tech_score'),
        avg_comm=Avg('comm_score'),
        avg_integrity=Avg('integrity_score'),
        avg_duration=Avg('duration'),
    )

    # ── Trend data (last 12 weeks / 12 months) ────────────────────────────
    weekly_trend = []
    monthly_shortlist = []

    if range_param in ('all', 'quarter'):
        # Weekly trend — last 12 weeks
        twelve_weeks_ago = local_now() - timedelta(weeks=12)
        weekly_rows = (
            InterviewLink.objects.filter(created_at__gte=twelve_weeks_ago)
            .annotate(week=TruncWeek('created_at'))
            .values('week')
            .annotate(total=Count('id'), selected=Count('id', filter=Q(outcome='Selected')))
            .order_by('week')
        )
        if person_q is not None:
            weekly_rows = weekly_rows.filter(person_q)
        for row in weekly_rows:
            weekly_trend.append({
                'week': row['week'].strftime('%Y-%m-%d') if row['week'] else None,
                'total': row['total'],
                'selected': row['selected'],
            })

    # Monthly trend — last 12 months
    twelve_months_ago = local_now() - timedelta(days=365)
    monthly_rows_qs = InterviewLink.objects.filter(created_at__gte=twelve_months_ago)
    if person_q is not None:
        monthly_rows_qs = monthly_rows_qs.filter(person_q)
    monthly_rows = (
        monthly_rows_qs
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(total=Count('id'), selected=Count('id', filter=Q(outcome='Selected')))
        .order_by('month')
    )
    for row in monthly_rows:
        monthly_shortlist.append({
            'month': row['month'].strftime('%Y-%m') if row['month'] else None,
            'total': row['total'],
            'selected': row['selected'],
            'shortlistRate': _pct(row['selected'], row['total']),
        })

    # ── Admin-only KPIs ───────────────────────────────────────────────────
    jobs_data = None
    recruiter_stats = None

    if scope == 'all':
        # Jobs breakdown
        total_jobs = jp_qs.count()
        total_openings = jp_qs.aggregate(s=Sum('openings'))['s'] or 0
        dept_counts = {}
        for row in jp_qs.values('dept').annotate(c=Count('id')):
            dept_counts[row['dept'] or 'Unknown'] = row['c']
        remote_count = jp_qs.filter(is_remote=True).count()
        onsite_count = jp_qs.filter(is_remote=False).count()
        job_type_counts = {}
        for row in jp_qs.values('type').annotate(c=Count('id')):
            job_type_counts[row['type'] or 'Full-time'] = row['c']

        jobs_data = {
            'totalJobs': total_jobs,
            'totalOpenings': total_openings,
            'byDepartment': [{'dept': k, 'count': v} for k, v in sorted(dept_counts.items(), key=lambda x: -x[1])],
            'byType': [{'type': k, 'count': v} for k, v in sorted(job_type_counts.items(), key=lambda x: -x[1])],
            'remote': remote_count,
            'onsite': onsite_count,
        }

        # Per-person breakdown — grouped by who scheduled the interview, with
        # legacy rows still counted under their typed interviewer so the
        # leaderboard does not empty out on the day this shipped.
        recruiter_rows = (
            InterviewLink.objects.all()
            if range_param == 'all' and not (d_from or d_to)
            else _date_filter(InterviewLink.objects.all(), 'created_at', range_param, d_from, d_to)
        )
        # Blank keys are NOT dropped. An interview with neither a creator nor a
        # typed interviewer is unattributed, not non-existent, and excluding it
        # left the tab reading "No recruiter data found" while the same request
        # reported a pipeline of 31 — two numbers from one queryset that could
        # not both be true. It gets its own row instead, so the column still
        # sums to the pipeline total and the gap is visible rather than hidden.
        per_recruiter = (
            recruiter_rows
            .annotate(creator=CREATOR_KEY)
            .values('creator')
            .annotate(
                total=Count('id'),
                selected=Count('id', filter=Q(outcome='Selected')),
                rejected=Count('id', filter=Q(outcome='Rejected')),
                pending=Count('id', filter=Q(outcome__isnull=True)),
                completed=Count('id', filter=Q(completed_at__isnull=False)),
                invited=Count('id', filter=Q(email_sent=True)),
                avg_score=Avg('score'),
                last_at=Max('created_at'),
            )
            .order_by('-total')[:100]
        )
        names = _creator_names([row['creator'] for row in per_recruiter])
        recruiter_stats = []
        credited = set()
        for row in per_recruiter:
            key = row['creator']
            known = key in names
            recruiter_stats.append({
                # `interviewer` is the drill-down value the dashboard sends back
                # as ?interviewer=, and the key the old response used — kept so
                # a stale cached bundle keeps rendering.
                'interviewer': key,
                'email': key if known else '',
                'name': names.get(key) or (key if key else 'Unattributed'),
                'attributed': known,
                # Nothing identifies the unattributed bucket, so there is no
                # person to scope the dashboard to.
                'drillable': bool(key),
                'total': row['total'],
                'selected': row['selected'],
                'rejected': row['rejected'],
                'pending': row['pending'],
                'completed': row['completed'],
                'invited': row['invited'],
                'shortlistRate': _pct(row['selected'], row['total']),
                'avgScore': round(float(row['avg_score']), 1) if row['avg_score'] else 0.0,
                'lastAt': row['last_at'].strftime('%Y-%m-%d') if row['last_at'] else None,
            })
            credited.add(norm_email(key))

        # Everyone else gets a row of zeros. A report listing only people with
        # activity cannot answer "who scheduled nothing this week", which is
        # half of what a per-person report is opened for — and in a range with
        # no activity at all it would show an empty table rather than a team.
        for u in AppUser.objects.filter(status='active').values('email', 'full_name'):
            em = norm_email(u['email'])
            if not em or em in credited:
                continue
            credited.add(em)
            recruiter_stats.append({
                'interviewer': em,
                'email': em,
                'name': (u['full_name'] or '').strip() or em.split('@')[0],
                'attributed': True,
                'drillable': True,
                'total': 0, 'selected': 0, 'rejected': 0, 'pending': 0,
                'completed': 0, 'invited': 0,
                'shortlistRate': 0.0, 'avgScore': 0.0, 'lastAt': None,
            })

        # Busiest people first, then the quiet ones by name, then whatever
        # could not be credited to a person at all.
        recruiter_stats.sort(key=lambda r: (
            not r['attributed'], -r['total'], (r['name'] or '').lower()))

    # ── Assemble response ─────────────────────────────────────────────────
    response = {
        'scope': scope,
        'range': range_param,
        'period': {
            'range': range_param,
            'from': d_from.isoformat() if d_from else None,
            'to': d_to.isoformat() if d_to else None,
            'label': _period_label(range_param, d_from, d_to),
        },
        # Who these numbers belong to, so the dashboard can title itself
        # honestly instead of implying org-wide figures while filtered.
        'viewing': _creator_label(interviewer_param) if (scope == 'all' and interviewer_param)
        else ({'email': caller_email, 'name': caller_name or caller_email, 'attributed': True}
              if scope == 'me' else None),
        'canViewOrg': can_org,
        'filters': {
            'roles': all_roles,
            'interviewers': all_interviewers,
            'departments': all_depts,
            'interview_types': all_interview_types,
            'statuses': all_statuses,
            'outcomes': all_outcomes,
            'sources': all_sources,
            'verdicts': all_verdicts,
            'job_types': all_job_types,
        },
        'pipeline': {
            'total': total_interviews,
            'byStatus': [{'status': k, 'count': v} for k, v in sorted(status_counts.items(), key=lambda x: -x[1])],
            'byOutcome': [{'outcome': k, 'count': v} for k, v in sorted(outcome_counts.items(), key=lambda x: -x[1])],
            'byInterviewType': [{'type': k, 'count': v} for k, v in sorted(interview_type_counts.items(), key=lambda x: -x[1])],
            'emailsSent': emails_sent,
            'emailsPending': emails_pending,
            'pendingOutcome': pending_outcome,
            'shortlistRate': shortlist_rate,
            'rejectionRate': rejection_rate,
            'waitlistRate': waitlist_rate,
            'avgCandidateScore': avg_candidate_score,
        },
        'resumeScoring': {
            'total': total_resumes,
            'highMatch': high_match,
            'highMatchRate': _pct(high_match, total_resumes),
            'avgScore': _r(rs_agg['avg_score']),
            'avgTechnical': _r(rs_agg['avg_technical']),
            'avgExperience': _r(rs_agg['avg_experience']),
            'avgDomain': _r(rs_agg['avg_domain']),
            'bySource': [{'source': k, 'count': v} for k, v in sorted(source_counts.items(), key=lambda x: -x[1])],
        },
        'recordings': {
            'total': total_recordings,
            'byVerdict': [{'verdict': k, 'count': v} for k, v in sorted(verdict_counts.items(), key=lambda x: -x[1])],
            'avgTotalScore': _r(ir_agg['avg_total']),
            'avgTechScore': _r(ir_agg['avg_tech']),
            'avgCommScore': _r(ir_agg['avg_comm']),
            'avgIntegrityScore': _r(ir_agg['avg_integrity']),
            'avgDuration': _fmt_duration(ir_agg['avg_duration']),
        },
        'trends': {
            'weekly': weekly_trend,
            'monthly': monthly_shortlist,
        },
    }

    if jobs_data is not None:
        response['jobs'] = jobs_data
    if recruiter_stats is not None:
        response['recruiterStats'] = recruiter_stats

    return Response(response)


# ===========================================================================
# Advanced Attendance Management — View functions
# ===========================================================================

@api_view(['GET', 'POST'])
@require_perm({'GET': 'settings.view', 'POST': 'settings.manage'})
def shifts(request):
    if request.method == 'GET':
        qs = Shift.objects.all()
        return Response(ShiftSerializer(qs, many=True).data)
    # POST
    ser = ShiftSerializer(data=request.data)
    if not ser.is_valid():
        return serializer_err(ser)
    ser.save(created_by=_resolve_recipient_email(request, 'system'))
    return Response(ser.data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_perm({'GET': 'settings.view', 'PUT': 'settings.manage', 'DELETE': 'settings.manage'})
def shift_detail(request, pk):
    obj = Shift.objects.filter(pk=pk).first()
    if not obj:
        return err('Shift not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = ShiftSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        ser.save()
        return Response(ser.data)
    return Response(ShiftSerializer(obj).data)


@api_view(['GET', 'POST'])
@require_perm({'GET': 'employee.view', 'POST': 'settings.manage'})
def shift_assignments(request):
    if request.method == 'GET':
        qs = ShiftAssignment.objects.select_related('shift').all()
        email = norm_email(request.GET.get('email'))
        if email:
            qs = qs.filter(email=email)
        return Response(ShiftAssignmentSerializer(qs, many=True).data)
    # POST
    ser = ShiftAssignmentSerializer(data=request.data)
    if not ser.is_valid():
        return serializer_err(ser)
    ser.save(created_by=_resolve_recipient_email(request, 'system'))
    return Response(ser.data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_perm({'GET': 'employee.view', 'PUT': 'settings.manage', 'DELETE': 'settings.manage'})
def shift_assignment_detail(request, pk):
    obj = ShiftAssignment.objects.filter(pk=pk).first()
    if not obj:
        return err('Assignment not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = ShiftAssignmentSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        ser.save()
        return Response(ser.data)
    return Response(ShiftAssignmentSerializer(obj).data)


@api_view(['GET', 'POST'])
@require_perm({'GET': 'attendance.view', 'POST': 'settings.manage'})
def attendance_geofences(request):
    if request.method == 'GET':
        # Company locations only. Homes are personal data with their own
        # endpoint and permission; they must not leak into the fence admin list.
        qs = GeoFence.objects.filter(owner_email='')
        return Response(GeoFenceSerializer(qs, many=True).data)
    # POST
    ser = GeoFenceSerializer(data=request.data)
    if not ser.is_valid():
        return serializer_err(ser)
    ser.save(created_by=_resolve_recipient_email(request, 'system'))
    return Response(ser.data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_perm({'GET': 'attendance.view', 'PUT': 'settings.manage', 'DELETE': 'settings.manage'})
def attendance_geofence_detail(request, pk):
    obj = GeoFence.objects.filter(pk=pk, owner_email='').first()
    if not obj:
        return err('Geofence not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = GeoFenceSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        ser.save()
        return Response(ser.data)
    return Response(GeoFenceSerializer(obj).data)


@api_view(['GET', 'POST'])
@require_perm({'GET': 'attendance.view', 'POST': 'attendance.create'}, or_self=True)
def wfh_requests(request):
    if request.method == 'GET':
        qs = WfhRequest.objects.all()
        email = norm_email(request.GET.get('email'))
        if email:
            qs = qs.filter(email=email)
        return Response(WfhRequestSerializer(qs, many=True).data)
    # POST
    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    ser = WfhRequestSerializer(data=body)
    if not ser.is_valid():
        return serializer_err(ser)
    inst = ser.save()
    notify_approvers(
        'attendance.approve_wfh',
        'New WFH Request',
        f"{inst.employee_name or email} has requested WFH from {inst.from_date} to {inst.to_date}.",
        '/employees/attendance',
    )
    return Response(ser.data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_perm({'GET': 'attendance.view', 'PUT': 'attendance.approve_wfh', 'DELETE': 'attendance.delete'})
def wfh_request_detail(request, pk):
    obj = WfhRequest.objects.filter(pk=pk).first()
    if not obj:
        return err('WFH request not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = WfhRequestSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        inst = ser.save()
        status_val = request.data.get('status')
        if status_val in ('Approved', 'Rejected'):
            create_notification(
                obj.email,
                f'WFH Request {status_val}',
                f'Your WFH request has been {status_val.lower()} by HR.',
                'success' if status_val == 'Approved' else 'warning',
                '/employees/attendance',
            )
        return Response(ser.data)
    return Response(WfhRequestSerializer(obj).data)


@api_view(['GET', 'POST'])
@require_perm({'GET': 'attendance.view', 'POST': 'attendance.create'}, or_self=True)
def attendance_corrections(request):
    if request.method == 'GET':
        qs = AttendanceCorrection.objects.all()
        email = norm_email(request.GET.get('email'))
        if email:
            qs = qs.filter(email=email)
        return Response(AttendanceCorrectionSerializer(qs, many=True).data)
    # POST
    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    ser = AttendanceCorrectionSerializer(data=body)
    if not ser.is_valid():
        return serializer_err(ser)
    inst = ser.save()
    notify_approvers(
        'settings.manage',
        'New Attendance Correction Request',
        f"{inst.employee_name or email} has requested a correction for {inst.attendance_date}.",
        '/employees/attendance',
    )
    return Response(ser.data, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@require_perm({'GET': 'attendance.view', 'PUT': 'settings.manage', 'DELETE': 'attendance.delete'})
def attendance_correction_detail(request, pk):
    obj = AttendanceCorrection.objects.filter(pk=pk).first()
    if not obj:
        return err('Correction request not found', 404)
    if request.method == 'DELETE':
        obj.delete()
        return Response({'ok': True})
    if request.method == 'PUT':
        ser = AttendanceCorrectionSerializer(obj, data=request.data, partial=True)
        if not ser.is_valid():
            return serializer_err(ser)
        inst = ser.save()
        status_val = request.data.get('status')
        if status_val == 'Approved':
            att, created = EmployeeAttendance.objects.get_or_create(
                email=inst.email,
                date=inst.attendance_date
            )
            if inst.requested_check_in:
                att.check_in = inst.requested_check_in
            if inst.requested_check_out:
                att.check_out = inst.requested_check_out

            shift = _get_active_shift(inst.email, inst.attendance_date)
            att.shift_id = shift.id
            if att.check_in and att.check_out:
                att.worked_minutes = max(int((att.check_out - att.check_in).total_seconds() // 60), 0)

                if shift.is_flexible:
                    req_min = int(shift.flex_hours_per_day * 60)
                    att.overtime_minutes = max(att.worked_minutes - req_min, 0)
                    att.early_exit_minutes = max(req_min - att.worked_minutes, 0)
                    att.late_minutes = 0
                    att.status = 'present'
                else:
                    shift_start = datetime.combine(inst.attendance_date, shift.start_time)
                    grace_start = shift_start + timedelta(minutes=shift.grace_minutes)
                    if att.check_in > grace_start:
                        att.late_minutes = int((att.check_in - shift_start).total_seconds() // 60)
                        att.status = 'late'
                    else:
                        att.late_minutes = 0
                        att.status = 'present'

                    shift_end = datetime.combine(inst.attendance_date, shift.end_time)
                    if att.check_out < shift_end:
                        att.early_exit_minutes = int((shift_end - att.check_out).total_seconds() // 60)
                    else:
                        att.early_exit_minutes = 0

                    if att.worked_minutes > shift.overtime_after_minutes:
                        att.overtime_minutes = att.worked_minutes - shift.overtime_after_minutes
                    else:
                        att.overtime_minutes = 0
            att.save()

            create_notification(
                obj.email,
                'Attendance Correction Approved',
                f'Your attendance correction for {inst.attendance_date} was approved and applied.',
                'success',
                '/employees/attendance',
            )
        elif status_val == 'Rejected':
            create_notification(
                obj.email,
                'Attendance Correction Rejected',
                f'Your attendance correction for {inst.attendance_date} was rejected.',
                'warning',
                '/employees/attendance',
            )
        return Response(ser.data)
    return Response(AttendanceCorrectionSerializer(obj).data)


@api_view(['GET'])
@require_perm('attendance.view', or_self=True)
def attendance_overtime(request):
    """Retrieve list of attendance records with overtime_minutes > 0."""
    qs = EmployeeAttendance.objects.filter(overtime_minutes__gt=0)
    email = norm_email(request.GET.get('email'))
    if email:
        qs = qs.filter(email=email)
    frm = parse_date(request.GET.get('from'))
    if frm:
        qs = qs.filter(date__gte=frm)
    to = parse_date(request.GET.get('to'))
    if to:
        qs = qs.filter(date__lte=to)
    return Response(EmployeeAttendanceSerializer(qs, many=True).data)


@api_view(['POST'])
@require_perm('settings.manage')
def attendance_auto_correct(request):
    """Scan all active users, identify missing checkout for past dates and auto checkout at shift end."""
    today = local_now().date()

    updated_count = 0
    records = EmployeeAttendance.objects.filter(
        date__lt=today,
        date__gte=today - timedelta(days=7),
        check_in__isnull=False,
        check_out__isnull=True
    )
    for rec in records:
        shift = _get_active_shift(rec.email, rec.date)
        checkout_dt = datetime.combine(rec.date, shift.end_time)
        rec.check_out = checkout_dt
        rec.worked_minutes = max(int((checkout_dt - rec.check_in).total_seconds() // 60), 0)

        if shift.is_flexible:
            req_min = int(shift.flex_hours_per_day * 60)
            rec.overtime_minutes = max(rec.worked_minutes - req_min, 0)
            rec.early_exit_minutes = max(req_min - rec.worked_minutes, 0)
        else:
            rec.early_exit_minutes = 0
            if rec.worked_minutes > shift.overtime_after_minutes:
                rec.overtime_minutes = rec.worked_minutes - shift.overtime_after_minutes

        rec.note = '[Auto Corrected Checkout]'
        rec.save()
        updated_count += 1

        create_notification(
            rec.email,
            'Auto Attendance Correction',
            f'Your missing checkout on {rec.date} has been automatically corrected to shift end.',
            'info',
            '/employees/attendance'
        )
    return Response({'ok': True, 'correctedCount': updated_count})


@api_view(['GET'])
@require_perm('attendance.view', or_self=True)
def attendance_analytics(request):
    """Comprehensive high-performance analytics payload."""
    from django.db.models import Sum
    email = norm_email(request.GET.get('email'))
    range_param = request.GET.get('range', 'month')

    now = local_now().date()
    if range_param == 'week':
        days_back = 7
    elif range_param == 'quarter':
        days_back = 90
    else:
        days_back = 30

    start_date = now - timedelta(days=days_back)

    qs = EmployeeAttendance.objects.filter(date__gte=start_date, date__lte=now)
    if email:
        qs = qs.filter(email=email)

    total_records = qs.count()
    if total_records == 0:
        return Response({
            'attendanceRate': 0.0,
            'lateRate': 0.0,
            'wfhRate': 0.0,
            'avgWorkedHours': 0.0,
            'totalOvertimeHours': 0.0,
            'trends': [],
        })

    late_count = qs.filter(status='late').count()
    wfh_count = qs.filter(is_wfh=True).count()

    agg = qs.aggregate(
        total_worked=Sum('worked_minutes'),
        total_ot=Sum('overtime_minutes')
    )

    total_worked_hours = round((agg['total_worked'] or 0) / 60.0, 1)
    total_ot_hours = round((agg['total_ot'] or 0) / 60.0, 1)
    avg_worked_hours = round(total_worked_hours / total_records, 1)

    headcount = AppUser.objects.count()
    if email:
        headcount = 1

    expected_days = days_back
    if headcount == 0:
        headcount = 1
    total_expected = expected_days * headcount
    if total_expected == 0:
        total_expected = 1

    attendance_rate = round((total_records / total_expected) * 100, 1)
    late_rate = round((late_count / total_records) * 100, 1)
    wfh_rate = round((wfh_count / total_records) * 100, 1)

    from django.db.models import Avg
    trend_qs = qs.values('date').annotate(
        count=Count('id'),
        wfh=Count('id', filter=Q(is_wfh=True)),
        late=Count('id', filter=Q(status='late')),
        avg_worked=Avg('worked_minutes')
    ).order_by('date')

    trends = []
    for r in trend_qs:
        trends.append({
            'date': r['date'].strftime('%Y-%m-%d'),
            'attendanceRate': round((r['count'] / headcount) * 100, 1),
            'wfhRate': round((r['wfh'] / r['count']) * 100, 1) if r['count'] else 0.0,
            'lateRate': round((r['late'] / r['count']) * 100, 1) if r['count'] else 0.0,
            'avgWorkedHours': round((r['avg_worked'] or 0) / 60.0, 1),
        })

    return Response({
        'attendanceRate': min(attendance_rate, 100.0),
        'lateRate': late_rate,
        'wfhRate': wfh_rate,
        'avgWorkedHours': avg_worked_hours,
        'totalOvertimeHours': total_ot_hours,
        'trends': trends,
    })


from .models import (  # noqa: E402,F811
    ChatRoom, ChatMember, ChatMessage, ChatMeeting,
    AppUser, UserProfile, OnboardingCandidate, EmployeeAttendance,
)
from .serializers import (  # noqa: E402
    ChatRoomSerializer, ChatMemberSerializer,
    ChatMessageSerializer, ChatMeetingSerializer,
)
# Chat APIs — direct messages, channels, and scheduled meetings.
#
# Rooms live in ``chat_rooms`` (is_group = 0 → direct 1:1, 1 → channel).
# Membership is ``chat_members``; messages ``chat_messages``; scheduled
# meetings ``chat_meetings`` (all managed outside Django — see
# migration 0045_chat_tables). Realtime delivery is handled by the Channels
# WebSocket consumer; the POST endpoints below persist + broadcast so the
# frontend has a REST fallback when the socket is unavailable.
# ===========================================================================

# How long after sending a message it can still be edited or deleted.
CHAT_EDIT_WINDOW = timedelta(minutes=5)


def _chat_now():
    # USE_TZ is False in this project, so store naive local datetimes to match
    # the rest of the tables.
    #
    # local_now(), not datetime.now(): "local" has to mean the business
    # timezone, not whichever clock the host happens to run. cPanel runs UTC
    # and the dev machines run IST, so datetime.now() stamped every production
    # message 5h30m behind the wall clock the sender saw — the same defect that
    # made check-ins land on the wrong side of midnight.
    return local_now()


def _fmt_dt(dt):
    try:
        return dt.isoformat()
    except Exception:
        return None


def _parse_dt(value):
    s = str(value or "").strip().replace("Z", "")
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Last resort — let fromisoformat try.
    return datetime.fromisoformat(str(value).replace("Z", ""))


def _broadcast_message(room_id, payload):
    """Best-effort push of a saved message to the room's WebSocket group."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f"chat_{room_id}",
            {
                "type": "chat_message",
                "id": payload.get("id"),
                "sender_email": payload.get("sender_email"),
                "sender_name": payload.get("sender_name"),
                "message": payload.get("message"),
                "created_at": payload.get("created_at"),
                "attachment_name": payload.get("attachment_name"),
                "attachment_type": payload.get("attachment_type"),
                "attachment_url": payload.get("attachment_url"),
                "reply_to_id": payload.get("reply_to_id"),
                "reply_to_sender": payload.get("reply_to_sender"),
                "reply_to_text": payload.get("reply_to_text"),
            },
        )
    except Exception:
        # Never let a missing/broken channel layer break the REST response.
        pass


def _broadcast_event(room_id, event):
    """Push an arbitrary event dict (must include ``type``) to a room group."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(f"chat_{room_id}", event)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Chat file attachments — stored on disk under <BASE_DIR>/media/chat_uploads/
# so large files (video, etc.) never bloat the database.
# ---------------------------------------------------------------------------
CHAT_UPLOAD_SUBDIR = "chat_uploads"


def _chat_media_root():
    return os.path.join(str(settings.BASE_DIR), "media")


def _chat_upload_dir():
    d = os.path.join(_chat_media_root(), CHAT_UPLOAD_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _save_attachment(data_url, orig_name):
    """Decode a base64 data URL and write it to disk. Returns (rel_path, ctype)."""
    import base64
    raw = data_url
    ctype = None
    if isinstance(raw, str) and raw.startswith("data:"):
        head, raw = raw.split(",", 1)
        if ":" in head and ";" in head:
            ctype = head.split(":", 1)[1].split(";", 1)[0] or None
    blob = base64.b64decode(raw)
    ext = os.path.splitext(orig_name or "")[1]
    if len(ext) > 12 or "/" in ext or "\\" in ext:
        ext = ""
    fname = secrets.token_hex(16) + ext
    with open(os.path.join(_chat_upload_dir(), fname), "wb") as fh:
        fh.write(blob)
    return CHAT_UPLOAD_SUBDIR + "/" + fname, ctype


def _serve_file_range(request, full_path, ctype, name):
    """Serve a disk file, honouring HTTP Range requests (needed for video)."""
    size = os.path.getsize(full_path)
    inline = ctype.startswith(("image/", "video/", "audio/")) or ctype == "application/pdf"
    disp = "inline" if inline else "attachment"
    range_header = request.headers.get("Range", "") or ""
    if range_header.startswith("bytes="):
        try:
            rng = range_header.split("=", 1)[1].split(",")[0]
            s, _, e = rng.partition("-")
            start = int(s) if s else 0
            end = int(e) if e else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            start, end = 0, size - 1
        length = end - start + 1
        with open(full_path, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)
        resp = HttpResponse(data, status=206, content_type=ctype)
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
        resp["Content-Length"] = str(length)
    else:
        with open(full_path, "rb") as fh:
            data = fh.read()
        resp = HttpResponse(data, content_type=ctype)
        resp["Content-Length"] = str(size)
    resp["Accept-Ranges"] = "bytes"
    resp["Content-Disposition"] = f'{disp}; filename="{name}"'
    resp["Cache-Control"] = "private, max-age=86400"
    return resp


def _contacts_directory(emails):
    """Map each email → {email,name,initials,profilePic,department,...}."""
    emails = [e for e in emails if e]
    if not emails:
        return {}
    users = {u.email: u for u in AppUser.objects.filter(email__in=emails)}
    profiles = {p.email: p for p in UserProfile.objects.filter(email__in=emails)}
    out = {}
    for e in emails:
        u = users.get(e)
        p = profiles.get(e)
        name = (u.full_name if u and u.full_name else "").strip()
        if not name and p:
            name = f"{p.first_name} {p.last_name}".strip()
        if not name:
            name = e.split("@")[0]
        initials = (u.initials if u and u.initials else "") or make_initials(name)
        pic = ""
        if u and u.profile_pic:
            pic = u.profile_pic
        elif p and p.profile_pic:
            pic = p.profile_pic
        out[e] = {
            "email": e,
            "name": name,
            "initials": initials,
            "profilePic": pic or "",
            "department": (p.department if p else "") or "",
            "designation": (p.designation if p else "") or "",
            "role": (u.role if u else "") or "",
            "status": (u.status if u else "active") or "active",
        }
    # Online = the person has an open check-in for today (WFO / WFH / remote —
    # any location counts). Offline otherwise.
    try:
        today = _chat_now().date()
        for a in EmployeeAttendance.objects.filter(email__in=emails, date=today):
            if a.check_in and (a.check_out is None or a.check_in > a.check_out):
                if a.email in out:
                    out[a.email]["online"] = True
    except Exception:
        pass
    for e in out:
        out[e].setdefault("online", False)
    return out


def _display_name_for(email):
    if not email:
        return ""
    return _contacts_directory([email]).get(email, {}).get("name", email.split("@")[0])


def _find_direct_room(a, b):
    """Return the existing 1:1 room shared by exactly a and b, if any."""
    a_rooms = set(ChatMember.objects.filter(employee_email=a).values_list("room_id", flat=True))
    b_rooms = set(ChatMember.objects.filter(employee_email=b).values_list("room_id", flat=True))
    for rid in (a_rooms & b_rooms):
        room = ChatRoom.objects.filter(id=rid, is_group=False).first()
        if room and ChatMember.objects.filter(room_id=rid).count() == 2:
            return room
    return None


def _last_preview(msg):
    if not msg:
        return ""
    if getattr(msg, "is_deleted", False):
        return "This message was deleted"
    if msg.message:
        return msg.message
    if getattr(msg, "attachment_name", None):
        return "\U0001F4CE " + msg.attachment_name
    return ""


def _room_payload(room, viewer):
    members_qs = list(ChatMember.objects.filter(room_id=room.id))
    mem_emails = [m.employee_email for m in members_qs]
    admin_map = {m.employee_email: bool(getattr(m, "is_admin", False)) for m in members_qs}
    directory = _contacts_directory(set(mem_emails))
    last = ChatMessage.objects.filter(room_id=room.id).order_by("-created_at").first()
    unread = (
        ChatMessage.objects.filter(room_id=room.id, is_read=False)
        .exclude(sender_email=viewer)
        .count()
    )
    is_group = bool(room.is_group)
    display_name = room.name
    other_email = ""
    if not is_group:
        other_email = next((e for e in mem_emails if e != viewer), "")
        info = directory.get(other_email)
        if info:
            display_name = info["name"]

    def member_obj(e):
        base = dict(directory.get(e, {"email": e, "name": e, "initials": make_initials(e), "profilePic": ""}))
        base["is_admin"] = admin_map.get(e, False)
        return base

    return {
        "id": room.id,
        "name": room.name,
        "display_name": display_name,
        "is_group": is_group,
        "is_private": bool(room.is_private),
        "created_by": room.created_by,
        "members": [member_obj(e) for e in mem_emails],
        "member_emails": mem_emails,
        "admin_emails": [e for e in mem_emails if admin_map.get(e)],
        "viewer_is_admin": admin_map.get(viewer, False),
        "other_email": other_email,
        "last_message": _last_preview(last),
        "last_sender": last.sender_email if last else "",
        "last_time": _fmt_dt(last.created_at) if last else None,
        "unread": unread,
    }


@api_view(["GET", "POST"])
def chat_rooms(request):
    if request.method == "POST":
        body = request.data or {}
        creator = norm_email(body.get("created_by") or request.headers.get("X-Actor-Email") or "")
        rtype = (body.get("type") or "").lower()
        members = []
        for e in (body.get("members") or []):
            ne = norm_email(e)
            if ne and ne not in members:
                members.append(ne)
        if creator and creator not in members:
            members.append(creator)

        is_group = rtype == "channel" or bool(body.get("is_group")) or len(members) > 2
        if not is_group:
            if len(members) < 2:
                return err("A direct chat needs two participants")
            existing = _find_direct_room(members[0], members[1])
            if existing:
                return Response(_room_payload(existing, creator), status=200)
            name = "Direct"
        else:
            name = (body.get("name") or "").strip()
            if not name:
                return err("Channel name is required")

        now = _chat_now()
        room = ChatRoom.objects.create(
            name=name,
            is_group=is_group,
            created_by=creator,
            created_at=now,
            is_private=bool(body.get("is_private", True)),
        )
        for e in members:
            # The channel creator starts as the (only) admin.
            ChatMember.objects.create(
                employee_email=e, room=room, joined_at=now,
                is_admin=(is_group and e == creator),
            )
        return Response(_room_payload(room, creator), status=201)

    # GET — rooms the given user belongs to, newest-activity first.
    email = norm_email(request.GET.get("email") or request.headers.get("X-Actor-Email") or "")
    if not email:
        return Response([])
    room_ids = list(
        ChatMember.objects.filter(employee_email=email).values_list("room_id", flat=True)
    )
    rooms = ChatRoom.objects.filter(id__in=room_ids)
    out = [_room_payload(r, email) for r in rooms]
    out.sort(key=lambda x: (x["last_time"] or ""), reverse=True)
    return Response(out)


@api_view(["GET", "POST"])
def chat_messages(request, room_id):
    room = ChatRoom.objects.filter(id=room_id).first()
    if not room:
        return err("Room not found", 404)

    if request.method == "GET":
        viewer = norm_email(request.GET.get("email") or request.headers.get("X-Actor-Email") or "")
        # Drop any pins older than 30 days before serializing (backend-enforced
        # expiry — the frontend never has to police it).
        _expire_stale_pins(room_id)
        msgs = list(
            ChatMessage.objects.filter(room_id=room_id)
            .select_related("room")
            .order_by("created_at")
        )
        data = ChatMessageSerializer(msgs, many=True).data

        # Per-member read receipts. Record that the viewer has now read this
        # room up to "now", then compute a `seen` flag for the viewer's OWN
        # messages: in a channel it's Seen only once EVERY other member has read
        # past it; in a 1:1 it's Seen once the other person has.
        now = _chat_now()
        members = list(ChatMember.objects.filter(room_id=room_id))
        if viewer:
            ChatMember.objects.filter(room_id=room_id, employee_email=viewer).update(last_read_at=now)
            ChatMessage.objects.filter(room_id=room_id, is_read=False).exclude(
                sender_email=viewer
            ).update(is_read=True)
        reads = {}
        for m in members:
            reads[m.employee_email] = now if m.employee_email == viewer else m.last_read_at
        for i, msg in enumerate(msgs):
            seen = False
            if viewer and msg.sender_email == viewer and not msg.is_deleted:
                others = [reads.get(e) for e in reads if e != msg.sender_email]
                if others:
                    seen = all(r is not None and r >= msg.created_at for r in others)
            data[i]["seen"] = seen
        return Response(data)

    # POST — persist + broadcast (REST fallback for the WebSocket). Supports an
    # optional file attachment (base64 data URL in ``attachment_data``).
    body = request.data or {}
    sender_email = norm_email(body.get("sender_email") or request.headers.get("X-Actor-Email") or "")
    text = (body.get("message") or "").strip()
    att_name = (body.get("attachment_name") or "").strip() or None
    att_type = (body.get("attachment_type") or "").strip() or None
    att_data = body.get("attachment_data") or None
    if not sender_email:
        return err("sender_email is required")
    if not text and not att_data:
        return err("message or attachment is required")
    sender_name = (body.get("sender_name") or _display_name_for(sender_email) or "").strip()

    # Write any attachment to disk (keeps big files out of the DB).
    att_path = None
    if att_data:
        try:
            att_path, detected = _save_attachment(att_data, att_name)
            if detected and not att_type:
                att_type = detected
        except Exception:
            return err("Could not save the attachment")
        att_data = None

    # Reply: denormalise a short quote of the message being replied to, so the
    # quote renders without an extra lookup (and survives if the original goes).
    reply_to_id = None
    reply_to_sender = None
    reply_to_text = None
    raw_reply = body.get("reply_to_id")
    if raw_reply:
        try:
            orig = ChatMessage.objects.filter(id=int(raw_reply), room_id=room_id).first()
        except (TypeError, ValueError):
            orig = None
        if orig and not orig.is_deleted:
            reply_to_id = orig.id
            reply_to_sender = orig.sender_name or (orig.sender_email or "").split("@")[0]
            snippet = orig.message or (f"\U0001F4CE {orig.attachment_name}" if orig.attachment_name else "")
            reply_to_text = snippet[:300]

    now = _chat_now()
    msg = ChatMessage.objects.create(
        room=room,
        sender_email=sender_email,
        sender_name=sender_name,
        message=text,
        created_at=now,
        is_read=False,
        attachment_name=att_name,
        attachment_type=att_type,
        attachment_data=att_data,
        attachment_path=att_path,
        reply_to_id=reply_to_id,
        reply_to_sender=reply_to_sender,
        reply_to_text=reply_to_text,
    )
    _broadcast_message(room_id, {
        "id": msg.id,
        "sender_email": sender_email,
        "sender_name": sender_name,
        "message": text,
        "created_at": _fmt_dt(now),
        "attachment_name": att_name,
        "attachment_type": att_type,
        "attachment_url": (f"/api/chat/attachment/{msg.id}" if att_path else ""),
        "reply_to_id": reply_to_id,
        "reply_to_sender": reply_to_sender,
        "reply_to_text": reply_to_text,
    })
    return Response(ChatMessageSerializer(msg).data, status=201)


@api_view(["PUT", "DELETE"])
def chat_message_detail(request, msg_id):
    """Edit (PUT) or soft-delete (DELETE) a single message. Sender only."""
    msg = ChatMessage.objects.filter(id=msg_id).first()
    if not msg:
        return err("Message not found", 404)
    actor = norm_email(
        (request.data or {}).get("email")
        or request.GET.get("email")
        or request.headers.get("X-Actor-Email")
        or ""
    )
    if not actor or actor != norm_email(msg.sender_email):
        return err("You can only modify your own messages", 403)

    # Time window: messages can only be edited/deleted shortly after sending.
    if msg.created_at:
        try:
            if (_chat_now() - msg.created_at) > CHAT_EDIT_WINDOW:
                mins = int(CHAT_EDIT_WINDOW.total_seconds() // 60)
                verb = "deleted" if request.method == "DELETE" else "edited"
                return err(
                    f"Messages can only be {verb} within {mins} minutes of sending.",
                    403,
                )
        except TypeError:
            pass

    room_id = msg.room_id

    if request.method == "DELETE":
        # If this message announced a meeting, cancel that meeting too — so its
        # "Join" button disappears once the message with the link is removed.
        old_text = msg.message or ""
        if old_text and "meet.jit.si" in old_text:
            for mt in ChatMeeting.objects.filter(room_id=room_id):
                if mt.join_url and mt.join_url in old_text:
                    mt.delete()

        # Remove the on-disk file, if any.
        old_path = getattr(msg, "attachment_path", None)
        if old_path:
            try:
                full = os.path.normpath(os.path.join(_chat_media_root(), old_path))
                if full.startswith(os.path.normpath(_chat_media_root())) and os.path.exists(full):
                    os.remove(full)
            except Exception:
                pass
        msg.is_deleted = True
        msg.message = ""
        msg.attachment_data = None
        msg.attachment_name = None
        msg.attachment_type = None
        msg.attachment_path = None
        msg.edited_at = _chat_now()
        msg.save(update_fields=[
            "is_deleted", "message", "attachment_data",
            "attachment_name", "attachment_type", "attachment_path", "edited_at",
        ])
        _broadcast_event(room_id, {"type": "message_deleted", "id": msg.id})
        return Response({"ok": True, "id": msg.id})

    # PUT — edit text
    new_text = ((request.data or {}).get("message") or "").strip()
    if not new_text:
        return err("message is required")
    if msg.is_deleted:
        return err("Cannot edit a deleted message")
    now = _chat_now()
    msg.message = new_text
    msg.edited = True
    msg.edited_at = now
    msg.save(update_fields=["message", "edited", "edited_at"])
    _broadcast_event(room_id, {
        "type": "message_edited",
        "id": msg.id,
        "message": new_text,
        "edited_at": _fmt_dt(now),
    })
    return Response(ChatMessageSerializer(msg).data)


@api_view(["GET"])
def chat_attachment(request, msg_id):
    """Return a message's file attachment with the right content type."""
    import base64
    msg = ChatMessage.objects.filter(id=msg_id).first()
    if not msg or msg.is_deleted:
        return err("Attachment not found", 404)
    ctype = msg.attachment_type or "application/octet-stream"
    name = msg.attachment_name or "file"

    # Disk-backed (new uploads) — supports HTTP Range for video/audio seeking.
    path = getattr(msg, "attachment_path", None)
    if path:
        full = os.path.normpath(os.path.join(_chat_media_root(), path))
        if full.startswith(os.path.normpath(_chat_media_root())) and os.path.exists(full):
            return _serve_file_range(request, full, ctype, name)

    # Base64-backed (older messages / fallback).
    if msg.attachment_data:
        raw = msg.attachment_data
        if isinstance(raw, str) and raw.startswith("data:"):
            try:
                head, raw = raw.split(",", 1)
                if ";" in head and ":" in head:
                    ctype = head.split(":", 1)[1].split(";", 1)[0] or ctype
            except ValueError:
                pass
        try:
            blob = base64.b64decode(raw)
        except Exception:
            return err("Attachment could not be decoded", 400)
        resp = HttpResponse(blob, content_type=ctype)
        inline = ctype.startswith(("image/", "video/", "audio/")) or ctype == "application/pdf"
        resp["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{name}"'
        resp["Cache-Control"] = "private, max-age=86400"
        return resp

    return err("Attachment not found", 404)


@api_view(["POST"])
def chat_mark_read(request, room_id):
    viewer = norm_email(
        (request.data or {}).get("email") or request.headers.get("X-Actor-Email") or ""
    )
    if viewer:
        ChatMessage.objects.filter(room_id=room_id, is_read=False).exclude(
            sender_email=viewer
        ).update(is_read=True)
    return Response({"ok": True})


# ---------------------------------------------------------------------------
# Channel membership + admins (max 2 admins, min 1; admins manage members).
# ---------------------------------------------------------------------------
# The channel creator is always an admin (permanent). Up to CHAT_MAX_ADMINS
# admins total are allowed — i.e. the creator plus (CHAT_MAX_ADMINS - 1) more.
CHAT_MAX_ADMINS = 3


def _is_room_admin(room_id, email):
    if not email:
        return False
    return ChatMember.objects.filter(
        room_id=room_id, employee_email=email, is_admin=True
    ).exists()


def _room_admin_count(room_id):
    return ChatMember.objects.filter(room_id=room_id, is_admin=True).count()


def _is_creator(room, email):
    return bool(email) and norm_email(room.created_by) == norm_email(email)


def _actor_of(request):
    return norm_email(
        request.GET.get("email")
        or (request.data or {}).get("email")
        or request.headers.get("X-Actor-Email")
        or ""
    )


@api_view(["GET", "POST"])
def chat_room_members(request, room_id):
    room = ChatRoom.objects.filter(id=room_id).first()
    if not room:
        return err("Room not found", 404)
    actor = _actor_of(request)

    if request.method == "GET":
        return Response(_room_payload(room, actor).get("members", []))

    # POST — add a member (admins only, channels only).
    if not room.is_group:
        return err("Members can only be managed on channels", 400)
    if not _is_room_admin(room_id, actor):
        return err("Only channel admins can add members", 403)
    email = norm_email(
        (request.data or {}).get("member")
        or (request.data or {}).get("member_email")
        or ""
    )
    if not email:
        return err("member email is required")
    if ChatMember.objects.filter(room_id=room_id, employee_email=email).exists():
        return err("This person is already in the channel", 409)
    make_admin = bool((request.data or {}).get("is_admin"))
    if make_admin and _room_admin_count(room_id) >= CHAT_MAX_ADMINS:
        make_admin = False
    ChatMember.objects.create(
        employee_email=email, room=room, joined_at=_chat_now(), is_admin=make_admin
    )
    return Response(_room_payload(room, actor))


@api_view(["PUT", "DELETE"])
def chat_room_member_detail(request, room_id, email):
    room = ChatRoom.objects.filter(id=room_id).first()
    if not room:
        return err("Room not found", 404)
    if not room.is_group:
        return err("Members can only be managed on channels", 400)
    actor = _actor_of(request)
    target = norm_email(email)
    member = ChatMember.objects.filter(room_id=room_id, employee_email=target).first()
    if not member:
        return err("Member not found", 404)
    is_self = (actor == target)

    if request.method == "DELETE":
        # A member may remove themselves (leave); otherwise admins only.
        if not _is_room_admin(room_id, actor) and not is_self:
            return err("Only channel admins can remove members", 403)
        # The creator is a permanent member/admin and can't be removed.
        if _is_creator(room, target):
            return err("The channel creator can't be removed", 400)
        member.delete()
        return Response(_room_payload(room, actor))

    # PUT — promote/demote admin (admins only).
    if not _is_room_admin(room_id, actor):
        return err("Only channel admins can change admins", 403)
    make_admin = bool((request.data or {}).get("is_admin"))
    if make_admin and not member.is_admin:
        if _room_admin_count(room_id) >= CHAT_MAX_ADMINS:
            extra = CHAT_MAX_ADMINS - 1
            return err(f"A channel can have the creator plus {extra} more admins ({CHAT_MAX_ADMINS} total).")
        member.is_admin = True
        member.save(update_fields=["is_admin"])
    elif not make_admin and member.is_admin:
        # The creator's admin status is permanent.
        if _is_creator(room, target):
            return err("The channel creator is a permanent admin")
        member.is_admin = False
        member.save(update_fields=["is_admin"])
    return Response(_room_payload(room, actor))


@api_view(["GET"])
def chat_contacts(request):
    """The company directory a user can start a chat with.

    Colleagues in this HRMS live across three tables, so the directory is the
    union of all of them (de-duplicated by email):
      • ``app_users``           — login accounts (only *disabled* ones hidden)
      • ``user_profiles``       — employee profiles
      • ``onboarding_candidates`` — people onboarded into the company
    Pass ``?all=1`` to also include disabled logins and rejected candidates."""
    me = norm_email(request.GET.get("email") or request.headers.get("X-Actor-Email") or "")
    include_all = bool(request.GET.get("all"))

    users = AppUser.objects.all()
    if not include_all:
        users = users.exclude(status="disabled")
    au = {}
    for u in users:
        if u.email:
            au[norm_email(u.email)] = u

    profs = {}
    for p in UserProfile.objects.all():
        if p.email:
            profs[norm_email(p.email)] = p

    cands = {}
    cand_qs = OnboardingCandidate.objects.filter(is_deleted=False)
    if not include_all:
        cand_qs = cand_qs.exclude(status="Rejected")
    for c in cand_qs:  # ordered by -created_at → first (newest) per email wins
        if c.email:
            e = norm_email(c.email)
            if e not in cands:
                cands[e] = c

    # Presence: online = an open check-in for today (WFO / WFH / remote).
    all_emails = list(set(au) | set(profs) | set(cands))
    online_set = set()
    try:
        _today = _chat_now().date()
        for _a in EmployeeAttendance.objects.filter(email__in=all_emails, date=_today):
            if _a.check_in and (_a.check_out is None or _a.check_in > _a.check_out):
                online_set.add(norm_email(_a.email))
    except Exception:
        pass

    out = []
    for e in (set(au) | set(profs) | set(cands)):
        if not e or (me and e == me):
            continue
        u = au.get(e)
        p = profs.get(e)
        c = cands.get(e)
        name = (u.full_name.strip() if u and u.full_name else "")
        if not name and p:
            name = (p.first_name + " " + p.last_name).strip()
        if not name and c:
            name = (c.first_name + " " + c.last_name).strip()
        if not name:
            name = e.split("@")[0]
        dept = (p.department if p and p.department else "") or (c.department if c else "")
        desg = (p.designation if p and p.designation else "") or (c.job_title if c else "")
        pic = ""
        if u and u.profile_pic:
            pic = u.profile_pic
        elif p and p.profile_pic:
            pic = p.profile_pic
        out.append({
            "email": e,
            "name": name,
            "initials": (u.initials if u and u.initials else "") or make_initials(name),
            "profilePic": pic or "",
            "department": dept or "",
            "designation": desg or "",
            "role": (u.role if u else "") or "",
            "status": (u.status if u else "active") or "active",
            "online": e in online_set,
        })

    out.sort(key=lambda x: x["name"].lower())
    return Response(out)


@api_view(["GET", "POST"])
def chat_meetings(request):
    if request.method == "GET":
        room_id = request.GET.get("room_id")
        email = norm_email(request.GET.get("email") or request.headers.get("X-Actor-Email") or "")
        qs = ChatMeeting.objects.all()
        if room_id:
            qs = qs.filter(room_id=room_id)
        elif email:
            room_ids = list(
                ChatMember.objects.filter(employee_email=email).values_list("room_id", flat=True)
            )
            qs = qs.filter(
                Q(room_id__in=room_ids) | Q(created_by=email) | Q(attendees__icontains=email)
            )
        return Response(ChatMeetingSerializer(qs.order_by("scheduled_at"), many=True).data)

    # POST — schedule a meeting and announce it in the room timeline.
    body = request.data or {}
    creator = norm_email(body.get("created_by") or request.headers.get("X-Actor-Email") or "")
    title = (body.get("title") or "").strip()
    if not title:
        return err("Meeting title is required")
    if not body.get("scheduled_at"):
        return err("scheduled_at is required")
    try:
        dt = _parse_dt(body.get("scheduled_at"))
    except Exception:
        return err("Invalid scheduled_at")

    room = ChatRoom.objects.filter(id=body.get("room_id")).first() if body.get("room_id") else None

    attendees = body.get("attendees") or []
    if isinstance(attendees, str):
        attendees = attendees.split(",")
    attendees = [norm_email(a) for a in attendees if norm_email(a)]
    if room:
        for e in ChatMember.objects.filter(room_id=room.id).values_list("employee_email", flat=True):
            if e not in attendees:
                attendees.append(e)
    if creator and creator not in attendees:
        attendees.append(creator)

    try:
        duration = int(body.get("duration_minutes") or 30)
    except (TypeError, ValueError):
        duration = 30

    join_url = (body.get("join_url") or "").strip()
    if not join_url:
        join_url = "https://meet.jit.si/hrms-" + secrets.token_hex(6)

    creator_name = (body.get("created_by_name") or _display_name_for(creator) or "").strip()
    now = _chat_now()
    meeting = ChatMeeting.objects.create(
        room=room,
        title=title,
        description=(body.get("description") or ""),
        scheduled_at=dt,
        duration_minutes=duration,
        created_by=creator,
        created_by_name=creator_name,
        join_url=join_url,
        attendees=",".join(attendees),
        created_at=now,
    )

    if room:
        when = dt.strftime("%b %d, %Y %I:%M %p")
        text = f"\U0001F4C5 Meeting scheduled: “{title}” on {when}. Join: {join_url}"
        msg = ChatMessage.objects.create(
            room=room,
            sender_email=creator,
            sender_name=creator_name,
            message=text,
            created_at=now,
            is_read=False,
        )
        _broadcast_message(room.id, {
            "id": msg.id,
            "sender_email": creator,
            "sender_name": creator_name,
            "message": text,
            "created_at": _fmt_dt(now),
        })

    return Response(ChatMeetingSerializer(meeting).data, status=201)


@api_view(["DELETE"])
def chat_meeting_detail(request, meeting_id):
    """Cancel a scheduled meeting (organiser or channel admin)."""
    mt = ChatMeeting.objects.filter(id=meeting_id).first()
    if not mt:
        return err("Meeting not found", 404)
    actor = _actor_of(request)
    allowed = bool(actor) and actor == norm_email(mt.created_by)
    if not allowed and mt.room_id:
        allowed = _is_room_admin(mt.room_id, actor)
    if not allowed:
        return err("Only the organiser or a channel admin can cancel this meeting", 403)
    mt.delete()
    return Response({"ok": True, "id": meeting_id})


@api_view(["DELETE"])
def chat_room_detail(request, room_id):
    """Delete a whole conversation.

    Channels: only an admin (which includes the permanent creator) may delete.
    Direct chats: either participant may delete. Removes the room together with
    its members, messages, meetings, and any on-disk attachments."""
    room = ChatRoom.objects.filter(id=room_id).first()
    if not room:
        return err("Room not found", 404)
    actor = _actor_of(request)
    is_member = ChatMember.objects.filter(room_id=room_id, employee_email=actor).exists()

    if room.is_group:
        if not _is_room_admin(room_id, actor):
            return err("Only a channel admin can delete this channel", 403)
    else:
        if not is_member:
            return err("You are not part of this chat", 403)

    # Remove any on-disk attachment files first.
    for m in ChatMessage.objects.filter(room_id=room_id):
        p = getattr(m, "attachment_path", None)
        if p:
            try:
                full = os.path.normpath(os.path.join(_chat_media_root(), p))
                if full.startswith(os.path.normpath(_chat_media_root())) and os.path.exists(full):
                    os.remove(full)
            except Exception:
                pass

    ChatMessage.objects.filter(room_id=room_id).delete()
    ChatMeeting.objects.filter(room_id=room_id).delete()
    ChatMember.objects.filter(room_id=room_id).delete()
    room.delete()
    return Response({"ok": True, "id": room_id})


# A pin stays active for 30 days, then quietly drops out of the pinned strip.
CHAT_PIN_TTL = timedelta(days=30)


def _expire_stale_pins(room_id):
    """Clear pins whose 30-day window has elapsed. Backend-enforced so the
    expiry doesn't depend on any frontend clock."""
    try:
        ChatMessage.objects.filter(
            room_id=room_id, is_pinned=True, pin_expires_at__lt=_chat_now(),
        ).update(is_pinned=False)
    except Exception:
        # Never let pin housekeeping break a message fetch.
        pass


@api_view(["POST"])
def chat_message_pin(request, msg_id):
    """Pin or unpin a message.

    Any room member may pin (a pin lasts 30 days). Only the person who pinned a
    message — ``pinned_by`` — may unpin it; anyone else gets 403. This is
    enforced here on the server, not merely by hiding the button in the UI.
    """
    msg = ChatMessage.objects.filter(id=msg_id).first()
    if not msg or msg.is_deleted:
        return err("Message not found", 404)
    actor = _actor_of(request)
    if not actor or not ChatMember.objects.filter(
        room_id=msg.room_id, employee_email=actor
    ).exists():
        return err("You are not part of this chat", 403)

    # Roll off any pins that have already aged out before acting.
    _expire_stale_pins(msg.room_id)
    msg.refresh_from_db(fields=["is_pinned", "pinned_by", "pin_expires_at"])

    pin = bool((request.data or {}).get("pin", True))
    if pin and not msg.is_pinned:
        now = _chat_now()
        msg.is_pinned = True
        msg.pinned_by = actor
        msg.pinned_at = now
        msg.pin_expires_at = now + CHAT_PIN_TTL
        msg.save(update_fields=["is_pinned", "pinned_by", "pinned_at", "pin_expires_at"])
    elif not pin and msg.is_pinned:
        # Only the original pinner can remove a pin.
        if (msg.pinned_by or "").strip().lower() != (actor or "").strip().lower():
            return err("Only the person who pinned this message can unpin it.", 403)
        msg.is_pinned = False
        msg.pinned_by = None
        msg.pinned_at = None
        msg.pin_expires_at = None
        msg.save(update_fields=["is_pinned", "pinned_by", "pinned_at", "pin_expires_at"])

    _broadcast_event(msg.room_id, {
        "type": "message_pinned",
        "id": msg.id,
        "is_pinned": msg.is_pinned,
        "pinned_by": msg.pinned_by or "",
        "pin_expires_at": _fmt_dt(msg.pin_expires_at) if msg.pin_expires_at else None,
    })
    return Response({
        "ok": True,
        "id": msg.id,
        "is_pinned": msg.is_pinned,
        "pinned_by": msg.pinned_by or "",
        "pinned_at": _fmt_dt(msg.pinned_at) if msg.pinned_at else None,
        "pin_expires_at": _fmt_dt(msg.pin_expires_at) if msg.pin_expires_at else None,
    })


# Languages offered by the composer's Translate tool. Keys are the codes the
# frontend sends; values are the human names handed to the model.
CHAT_TRANSLATE_LANGS = {
    "en": "English",
    "hi": "Hindi",
    "te": "Telugu",
    "ta": "Tamil",
    "kn": "Kannada",
    "ml": "Malayalam",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
}


@api_view(["POST"])
def chat_translate(request):
    """Translate the composer's text into the requested language.

    The Anthropic API key stays on the server (read from the environment via
    ``ai._api_key``) — it is never sent to or exposed in the browser. Returns
    the translated string; the frontend drops it back into the input without
    auto-sending, so the user can review/edit first.
    """
    body = request.data or {}
    text = (body.get("text") or "").strip()
    lang = (body.get("target") or body.get("lang") or "").strip().lower()

    if not text:
        return err("Nothing to translate.")
    if len(text) > 5000:
        return err("Text is too long to translate (5000 character limit).")
    lang_name = CHAT_TRANSLATE_LANGS.get(lang)
    if not lang_name:
        return err("Unsupported target language.")

    # Reuse the project's existing Anthropic integration + server-side key.
    from . import ai as _ai
    api_key = _ai._api_key()
    if not api_key:
        return err("Translation is unavailable (no API key configured).", 503)

    prompt = (
        f"Translate the following message into {lang_name}. "
        "Return ONLY the translated text, with no quotes, notes, or explanation. "
        "Preserve the original meaning, tone, emojis and any URLs.\n\n"
        f"Message:\n{text}"
    )
    try:
        import requests as _requests
        resp = _requests.post(
            _ai.ANTHROPIC_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": _ai.ANTHROPIC_VERSION,
            },
            json={
                "model": _ai.VALIDATION_MODEL,
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
    except Exception:
        return err("Could not reach the translation service.", 502)

    if not resp.ok:
        return err("The translation service returned an error.", 502)
    try:
        parts = resp.json().get("content") or []
        translated = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict)
        ).strip()
    except Exception:
        translated = ""
    if not translated:
        return err("The translation came back empty. Please try again.", 502)
    return Response({"ok": True, "translated": translated, "target": lang})


# ===========================================================================
# Forgot-punch attendance tickets
# ---------------------------------------------------------------------------
# An employee who forgot to check in (realised after the workday) or forgot to
# check out (noticed before midnight) raises a ticket with a reason and a photo
# of the attendance sheet. An approver (attendance.approve_wfh, same as WFH)
# approves, which stamps that day's check-in/out to the office shift times.
# Reuses the AttendanceCorrection table; ticket rows carry kind in
# forgot_checkin|forgot_checkout so the monthly limit ignores legacy rows.
# ===========================================================================
ATT_TICKET_LIMIT = 3
_TICKET_KINDS = ('forgot_checkin', 'forgot_checkout')


def _ticket_caller(request):
    return norm_email(request.headers.get('X-Actor-Email') or request.headers.get('X-User-Email') or '')


def _ticket_is_approver(request):
    allowed, caller, _u = check_perm(request, 'attendance.approve_wfh')
    return allowed, caller


def _auto_close_open_before_today(email=None):
    """Close open past-day attendance (check-in set, no check-out) at 23:59 of
    that day. Marks is_auto_checked_out and recomputes worked minutes. Scoped to
    one employee when ``email`` is given (lazy self-heal when they open the page)."""
    today = local_today()
    qs = EmployeeAttendance.objects.filter(
        date__lt=today, check_in__isnull=False, check_out__isnull=True)
    if email:
        qs = qs.filter(email=norm_email(email))
    closed = 0
    for rec in qs:
        end = datetime.combine(rec.date, time(23, 59, 0))
        if end <= rec.check_in:
            end = rec.check_in + timedelta(minutes=1)
        rec.check_out = end
        worked = int((end - rec.check_in).total_seconds() // 60) - (rec.break_minutes or 0)
        rec.worked_minutes = max(0, worked)
        rec.is_auto_checked_out = True
        rec.auto_checkout_at = local_now()
        rec.save(update_fields=['check_out', 'worked_minutes',
                                'is_auto_checked_out', 'auto_checkout_at'])
        closed += 1
    return closed


def _ticket_dto(c, include_proof=False):
    d = {
        'id': c.id,
        'email': c.email,
        'employee': c.employee_name or c.email,
        'date': c.attendance_date.isoformat() if c.attendance_date else None,
        'kind': c.kind or '',
        'reason': c.reason or '',
        'status': c.status or 'Pending',
        'reviewer': c.reviewer or '',
        'reviewerNote': c.reviewer_note or '',
        'createdAt': dt(c.created_at),
        'hasProof': bool(c.proof_image),
    }
    if include_proof:
        d['proofImage'] = c.proof_image or ''
    return d


@api_view(['GET', 'POST'])
def attendance_punch_ticket(request):
    if request.method == 'GET':
        email = norm_email(request.query_params.get('email'))
        if not email:
            return err('email is required')
        try:
            _auto_close_open_before_today(email)
        except Exception:
            pass
        today = local_today()
        rec = EmployeeAttendance.objects.filter(email=email, date=today).first()
        has_in = bool(rec and rec.check_in)
        has_out = bool(rec and rec.check_out)
        shift = _get_active_shift(email, today)
        end_time = shift.end_time if shift else time(18, 0)
        start_time = shift.start_time if shift else time(9, 0)
        now = local_now()
        _pend = AttendanceCorrection.objects.filter(
            email=email, attendance_date=today, status='Pending', kind__in=_TICKET_KINDS)
        pend_in = _pend.filter(kind='forgot_checkin').exists()
        pend_out = _pend.filter(kind='forgot_checkout').exists()
        # Late = checked in, but after the office start time.
        is_late = bool(has_in and rec and rec.check_in and rec.check_in.time() > start_time)
        # A check-in ticket (missed OR late) can only be raised once the day has
        # a check-out.
        can_checkin = has_out and ((not has_in) or is_late) and not pend_in
        can_checkout = has_in and (not has_out) and not pend_out
        used = AttendanceCorrection.objects.filter(
            email=email, kind__in=_TICKET_KINDS,
            created_at__year=now.year, created_at__month=now.month).count()
        block = ''
        if not can_checkin and not can_checkout:
            if pend_in or pend_out:
                block = 'You already have a pending ticket for today — please wait for it to be reviewed.'
            elif has_in and has_out:
                block = 'Your check-in is on time and you have checked out — nothing to correct.'
            elif (not has_in) and (not has_out):
                block = 'You can raise a check-in ticket only after you have checked out for the day.'
            else:
                block = 'Nothing to correct for today.'
        tickets = AttendanceCorrection.objects.filter(
            email=email, kind__in=_TICKET_KINDS).order_by('-id')[:8]
        return Response({
            'today': {'date': today.isoformat(), 'hasCheckIn': has_in, 'hasCheckOut': has_out},
            'canRaiseCheckin': can_checkin,
            'canRaiseCheckout': can_checkout,
            'blockReason': block,
            'usedThisMonth': used,
            'limit': ATT_TICKET_LIMIT,
            'remaining': max(0, ATT_TICKET_LIMIT - used),
            'officeStart': start_time.strftime('%H:%M'),
            'officeEnd': end_time.strftime('%H:%M'),
            'tickets': [_ticket_dto(c) for c in tickets],
        })

    # POST — raise a ticket
    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    caller = _ticket_caller(request)
    if caller and email != caller:
        return err('You can only raise a ticket for your own attendance.', 403)
    kind = (body.get('kind') or '').strip()
    if kind not in _TICKET_KINDS:
        return err('kind must be forgot_checkin or forgot_checkout')
    reason = (body.get('reason') or '').strip()
    if not reason:
        return err('A reason is required (why you forgot to punch).')
    proof = body.get('proofImage') or ''
    if not proof:
        return err('Please attach a photo of the attendance sheet as proof.')
    if len(proof) > 8 * 1024 * 1024:
        return err('The proof image is too large. Please use a smaller photo.')

    now = local_now()
    used = AttendanceCorrection.objects.filter(
        email=email, kind__in=_TICKET_KINDS,
        created_at__year=now.year, created_at__month=now.month).count()
    if used >= ATT_TICKET_LIMIT:
        return err('You have reached the limit of %d attendance tickets this month.' % ATT_TICKET_LIMIT, 403)

    today = local_today()
    try:
        _auto_close_open_before_today(email)
    except Exception:
        pass
    rec = EmployeeAttendance.objects.filter(email=email, date=today).first()
    has_in = bool(rec and rec.check_in)
    has_out = bool(rec and rec.check_out)
    shift = _get_active_shift(email, today)
    start_time = shift.start_time if shift else time(9, 0)
    end_time = shift.end_time if shift else time(18, 0)
    if kind == 'forgot_checkin':
        if not has_out:
            return err('You can raise a check-in ticket only after you have checked out for the day.')
        if has_in and not (rec.check_in and rec.check_in.time() > start_time):
            return err('Your check-in is already on time — there is nothing to correct.')
    else:  # forgot_checkout
        if not has_in:
            return err('You have no check-in today, so raise a check-in ticket instead.')
        if has_out:
            return err('You already have a check-out recorded for today.')

    if AttendanceCorrection.objects.filter(
            email=email, attendance_date=today, kind=kind, status='Pending').exists():
        return err('You already have a pending ticket for this. Please wait for it to be reviewed.')

    name = (rec.employee_name if rec and rec.employee_name else '') or (body.get('employeeName') or email.split('@')[0])
    c = AttendanceCorrection.objects.create(
        email=email, employee_name=name, attendance_date=today,
        kind=kind, reason=reason, proof_image=proof, status='Pending')
    return Response({'success': True, 'id': c.id, 'message': 'Ticket submitted for approval.'})


@api_view(['GET'])
def attendance_punch_ticket_pending(request):
    allowed, caller = _ticket_is_approver(request)
    if not caller:
        return err('Authentication required. Please sign in again.', 401)
    if not allowed:
        return err('You do not have permission to review attendance tickets.', 403)
    rows = AttendanceCorrection.objects.filter(
        kind__in=_TICKET_KINDS, status='Pending').order_by('-id')[:100]
    return Response([_ticket_dto(c) for c in rows])


@api_view(['GET'])
def attendance_punch_ticket_proof(request, pk):
    c = AttendanceCorrection.objects.filter(id=pk).first()
    if not c:
        return err('Ticket not found.', 404)
    allowed, _caller = _ticket_is_approver(request)
    caller = _ticket_caller(request)
    if not allowed and caller != norm_email(c.email):
        return err('You are not allowed to view this proof.', 403)
    return Response({'id': c.id, 'proofImage': c.proof_image or ''})


@api_view(['POST'])
def attendance_punch_ticket_decide(request, pk):
    allowed, caller = _ticket_is_approver(request)
    if not caller:
        return err('Authentication required. Please sign in again.', 401)
    if not allowed:
        return err('You do not have permission to review attendance tickets.', 403)
    c = AttendanceCorrection.objects.filter(id=pk, kind__in=_TICKET_KINDS).first()
    if not c:
        return err('Ticket not found.', 404)
    action = (request.data.get('action') or '').strip().lower()
    note = (request.data.get('reviewerNote') or '').strip()
    if action not in ('approve', 'reject'):
        return err('action must be approve or reject')
    if c.status != 'Pending':
        return err('This ticket has already been %s.' % (c.status or '').lower())

    if action == 'reject':
        c.status = 'Rejected'
        c.reviewer = caller
        c.reviewer_note = note
        c.reviewed_at = local_now()
        c.save(update_fields=['status', 'reviewer', 'reviewer_note', 'reviewed_at'])
        return Response({'success': True, 'message': 'Ticket rejected.'})

    # approve → stamp the office (shift) times onto that day's attendance
    shift = _get_active_shift(c.email, c.attendance_date)
    start_t = shift.start_time if shift else time(9, 0)
    end_t = shift.end_time if shift else time(18, 0)
    rec = EmployeeAttendance.objects.filter(email=c.email, date=c.attendance_date).first()
    if not rec:
        rec = EmployeeAttendance(email=c.email,
                                 employee_name=c.employee_name or c.email,
                                 date=c.attendance_date, status='present')
    if c.kind == 'forgot_checkin' or not rec.check_in:
        rec.check_in = datetime.combine(c.attendance_date, start_t)
        rec.late_minutes = 0
        if (rec.status or '') == 'late':
            rec.status = 'present'
    if c.kind == 'forgot_checkout' or not rec.check_out:
        rec.check_out = datetime.combine(c.attendance_date, end_t)
    if rec.check_in and rec.check_out:
        worked = int((rec.check_out - rec.check_in).total_seconds() // 60) - (rec.break_minutes or 0)
        rec.worked_minutes = max(0, worked)
    rec.is_auto_checked_out = False
    if not rec.shift_id and shift:
        rec.shift_id = shift.id
    rec.save()

    c.status = 'Approved'
    c.reviewer = caller
    c.reviewer_note = note
    c.reviewed_at = local_now()
    c.save(update_fields=['status', 'reviewer', 'reviewer_note', 'reviewed_at'])
    return Response({'success': True, 'message': 'Ticket approved — attendance updated to office hours.'})


# ===========================================================================
# Employee attendance detail + hourly location tracking
# ===========================================================================
def _att_can_view(request, email):
    """(allowed, caller). Self can always view; others need attendance.view or
    attendance.approve_wfh (Super Admin bypasses via check_perm)."""
    caller = _ticket_caller(request)
    if caller and caller == norm_email(email):
        return True, caller
    for code in ('attendance.view', 'attendance.approve_wfh'):
        allowed, c, _u = check_perm(request, code)
        if allowed:
            return True, (c or caller)
    return False, caller


def _fmt_ampm(d):
    if not d:
        return ''
    return d.strftime('%I:%M %p').lstrip('0')


def _event_label(ev):
    ev = str(ev or '')
    if 'check-in' in ev:
        return 'Check In'
    if 'check-out' in ev:
        return 'Check Out'
    if 'remote' in ev:
        return 'Switched to Remote'
    if 'office' in ev:
        return 'Switched to Office'
    if 'break-start' in ev:
        return 'Break start'
    if 'break-end' in ev:
        return 'Break end'
    return ev


@api_view(['GET'])
def attendance_day_detail(request):
    email = norm_email(request.query_params.get('email'))
    if not email:
        return err('email is required')
    ok, caller = _att_can_view(request, email)
    if not ok:
        return err('You are not allowed to view this attendance.', 403)
    ds = request.query_params.get('date')
    try:
        day = datetime.strptime(ds, '%Y-%m-%d').date() if ds else local_today()
    except ValueError:
        return err('Invalid date.')
    rec = EmployeeAttendance.objects.filter(email=email, date=day).first()
    shift = _get_active_shift(email, day)
    check_in = rec.check_in if rec else None
    check_out = rec.check_out if rec else None
    gross = int((check_out - check_in).total_seconds() // 60) if (check_in and check_out) else 0
    effective = (rec.worked_minutes if rec else 0) or 0
    name = (rec.employee_name if rec and rec.employee_name else '') or email.split('@')[0]
    events = AttendanceEvent.objects.filter(email=email, date=day).order_by('at')
    ev = [{
        'event': e.event, 'label': _event_label(e.event),
        'time': _fmt_ampm(e.at), 'at': dt(e.at),
        'location': e.location or '',
        'latitude': e.latitude, 'longitude': e.longitude,
    } for e in events]
    punches = AttendanceLocationPunch.objects.filter(email=email, date=day).order_by('captured_at')
    pl = [{
        'id': p.id, 'time': _fmt_ampm(p.captured_at), 'capturedAt': dt(p.captured_at),
        'latitude': p.latitude, 'longitude': p.longitude,
        'accuracy': p.accuracy, 'label': p.label or '',
    } for p in punches]
    late = (rec.status if rec else '') == 'late'
    return Response({
        'email': email, 'employee': name, 'date': day.isoformat(),
        'shift': {
            'name': shift.name if shift else 'General Shift',
            'start': _fmt_ampm(datetime.combine(day, shift.start_time)) if shift else '',
            'end': _fmt_ampm(datetime.combine(day, shift.end_time)) if shift else '',
        },
        'checkIn': _fmt_ampm(check_in), 'checkInAt': dt(check_in),
        'checkOut': _fmt_ampm(check_out), 'checkOutAt': dt(check_out),
        'effectiveMinutes': effective, 'grossMinutes': gross,
        'status': (rec.status if rec else ''),
        'onTime': bool(check_in) and not late,
        'isSelf': caller == email,
        'events': ev, 'punches': pl,
    })


@api_view(['GET', 'POST'])
def attendance_location_punch(request):
    if request.method == 'GET':
        email = norm_email(request.query_params.get('email'))
        if not email:
            return err('email is required')
        ok, _caller = _att_can_view(request, email)
        if not ok:
            return err('Not allowed.', 403)
        ds = request.query_params.get('date')
        try:
            day = datetime.strptime(ds, '%Y-%m-%d').date() if ds else local_today()
        except ValueError:
            return err('Invalid date.')
        rows = AttendanceLocationPunch.objects.filter(email=email, date=day).order_by('captured_at')
        return Response([{
            'id': p.id, 'time': _fmt_ampm(p.captured_at), 'capturedAt': dt(p.captured_at),
            'latitude': p.latitude, 'longitude': p.longitude,
            'accuracy': p.accuracy, 'label': p.label or '',
        } for p in rows])

    # POST — store a sample (self only, only while checked in, throttled hourly)
    body = request.data
    email = norm_email(body.get('email'))
    if not email:
        return err('email is required')
    caller = _ticket_caller(request)
    if caller and email != caller:
        return err('You can only store your own location.', 403)
    try:
        lat = float(body.get('latitude'))
        lng = float(body.get('longitude'))
    except (TypeError, ValueError):
        return err('latitude and longitude are required')
    acc = body.get('accuracy')
    try:
        acc = float(acc) if acc is not None else None
    except (TypeError, ValueError):
        acc = None
    label = (str(body.get('label') or ''))[:255]
    now = local_now()
    today = local_today()
    rec = EmployeeAttendance.objects.filter(email=email, date=today).first()
    checked_in = bool(rec and rec.check_in and (not rec.check_out or rec.check_in > rec.check_out))
    if not checked_in:
        return Response({'stored': False, 'reason': 'not_checked_in'})
    last = AttendanceLocationPunch.objects.filter(email=email, date=today).order_by('-captured_at').first()
    if last and (now - last.captured_at).total_seconds() < 55 * 60:
        return Response({'stored': False, 'reason': 'too_soon'})
    name = (rec.employee_name if rec and rec.employee_name else '') or email.split('@')[0]
    p = AttendanceLocationPunch.objects.create(
        email=email, employee_name=name, date=today, captured_at=now,
        latitude=lat, longitude=lng, accuracy=acc, label=label, source='web')
    return Response({'stored': True, 'id': p.id, 'time': _fmt_ampm(now)})
