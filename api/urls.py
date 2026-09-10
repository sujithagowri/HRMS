from django.urls import path

from . import (auth_views, email_template_views, linkedin_oauth, live_views, views,
               attendance_views, job_form_views, onboarding_views, payroll_views,
               sourcing_views)

# All paths are relative to the "/api/" prefix from the project urlconf.
urlpatterns = [
    # Authentication (OTP login + password reset + Google OAuth)
    path('auth/login', auth_views.login),
    path('auth/signup', auth_views.signup),
    path('auth/verify-otp', auth_views.verify_otp),
    path('auth/resend-otp', auth_views.resend_otp),
    path('auth/forgot-password', auth_views.forgot_password),
    path('auth/reset-password', auth_views.reset_password),
    path('auth/verify-reset-token', auth_views.verify_reset_token),
    path('auth/google', auth_views.google_auth),

    # Per-user LinkedIn linking, so new jobs post to the creator's own profile
    path('auth/linkedin/connect', linkedin_oauth.linkedin_connect),
    path('auth/linkedin/callback', linkedin_oauth.linkedin_callback),
    path('auth/linkedin/status', linkedin_oauth.linkedin_status),
    path('auth/linkedin/preview', linkedin_oauth.linkedin_preview),
    path('auth/linkedin/disconnect', linkedin_oauth.linkedin_disconnect),

    path('jobs', views.jobs),
    path('jobs/<int:pk>', views.job_detail),

    # Reusable candidate follow-up email templates
    path('email-templates', email_template_views.email_templates),
    path('email-templates/preview', email_template_views.email_template_preview),
    path('email-templates/<int:pk>', email_template_views.email_template_detail),

    # Job Form Builder: dynamic schema, templates, master data, currencies
    path('job-form/config', job_form_views.job_form_config),
    path('job-form/templates', job_form_views.job_form_templates),
    path('job-form/templates/<int:pk>', job_form_views.job_form_template_detail),
    path('master-data', job_form_views.master_data),
    path('master-data/<str:key>', job_form_views.master_data_detail),
    path('currencies', job_form_views.currencies),

    path('interviews', views.interviews),
    path('interviews/bulk/send-emails', views.interviews_bulk_send_emails),
    path('interviews/send-followup', views.interview_send_followup),
    path('interviews/verify-token', views.interview_verify_token),
    path('interviews/complete', views.interview_complete),
    path('interviews/<int:pk>', views.interview_detail),
    path('interviews/<int:pk>/note', views.interview_note),
    path('interviews/<int:pk>/regenerate-link', views.interview_regenerate_link),
    path('interviews/<int:pk>/resend-invitation', views.interview_resend_invitation),

    # Live interview (WebRTC signaling for recruiter live-view)
    path('live', live_views.live_list),
    path('live/start', live_views.live_start),
    path('live/<str:sid>', live_views.live_detail),
    path('live/<str:sid>/answer', live_views.live_answer),
    path('live/<str:sid>/ice', live_views.live_ice),
    path('live/<str:sid>/update', live_views.live_update),
    path('live/<str:sid>/end', live_views.live_end),

    path('resume-scores', views.resume_scores),
    path('resume-scores/<int:pk>', views.resume_score_detail),
    path('resume-scores/<int:pk>/file', views.resume_score_file),

    # Candidate sourcing — internal resumes today; approved providers plug into
    # api/services/sourcing/connectors without changing the public API.
    path('sourcing/searches', sourcing_views.sourcing_searches),
    path('sourcing/searches/<int:pk>', sourcing_views.sourcing_search_detail),
    path('sourcing/searches/<int:pk>/results', sourcing_views.sourcing_search_results),
    path('sourcing/results/<int:pk>/shortlist', sourcing_views.sourcing_result_shortlist),
    path('sourcing/results/<int:pk>/feedback', sourcing_views.sourcing_result_feedback),

    path('interview-recordings', views.recordings),
    path('interview-recordings/<int:pk>', views.recording_detail),
    path('interview-recordings/<int:pk>/video', views.recording_video),

    path('question-sets', views.question_sets),
    path('question-sets/<str:set_id>', views.question_set_detail),

    path('ai/status', views.ai_status),
    path('ai/status/', views.ai_status),
    path('ai/generate-questions', views.ai_generate_questions),
    path('ai/generate-questions/', views.ai_generate_questions),
    path('ai/score-resume', views.ai_score_resume),
    path('ai/score-resume/', views.ai_score_resume),
    path('ai/evaluate-interview', views.ai_evaluate_interview),
    path('ai/evaluate-interview/', views.ai_evaluate_interview),

    path('users', views.users),
    path('users/<str:email>', views.user_detail),
    path('employees', views.employees),

    path('notifications', views.notifications),
    path('notifications/<int:pk>/read', views.notification_read),
    path('notifications/<int:pk>', views.notification_delete),
    path('notifications/delete', views.notifications_delete_batch),
    path('notifications/read-all', views.notifications_read_all),

    path('user-settings/<str:email>', views.user_settings),
    path('user-settings/<str:email>/profile', views.user_profile),
    path('user-settings/<str:email>/email-config', views.user_email_config),
    path('user-settings/<str:email>/documents', views.user_documents),
    path('user-settings/<str:email>/documents/<str:doc_type>', views.user_document_detail),

    # Company Details & General System Settings (Settings -> General)
    path('settings/general', views.company_settings),
    path('settings/general/', views.company_settings),
    path('settings/company', views.company_settings),
    path('settings/company/', views.company_settings),
    path('settings/general/backfill', views.company_settings_backfill),
    path('settings/general/backfill/', views.company_settings_backfill),
    path('settings/company/backfill', views.company_settings_backfill),
    path('settings/company/backfill/', views.company_settings_backfill),

    # Employees module — Attendance / Check-In-Out
    path('attendance', views.attendance),
    path('attendance/check-in', views.attendance_check_in),
    path('attendance/check-out', views.attendance_check_out),
    path('attendance/today', views.attendance_today),
    path('attendance/events', views.attendance_events),
    path('attendance/team', views.attendance_team),
    path('attendance/presence', views.attendance_presence),
    path('attendance/overtime', views.attendance_overtime),
    path('attendance/auto-correct', views.attendance_auto_correct),
    path('attendance/analytics', views.attendance_analytics),
    path('attendance/geofence-check', views.attendance_geofence_check),
    path('attendance/location-reviews', views.attendance_location_reviews),
    path('attendance/map-feed', views.attendance_map_feed),
    path('attendance/departure-policy', views.attendance_departure_policy),
    path('attendance/roster', views.attendance_roster),
    path('attendance/home-locations', views.attendance_home_locations),
    path('attendance/home-locations/review', views.attendance_home_location_review),
    path('attendance/arrangements', views.attendance_arrangements),
    path('attendance/arrangements/<int:pk>', views.attendance_arrangement_detail),
    path('attendance/geofences', views.attendance_geofences),
    path('attendance/geofences/<int:pk>', views.attendance_geofence_detail),
    path('attendance/wfh', views.wfh_requests),
    path('attendance/wfh/<int:pk>', views.wfh_request_detail),
    path('attendance/corrections', views.attendance_corrections),
    path('attendance/corrections/<int:pk>', views.attendance_correction_detail),
    # Forgot-punch tickets (raise a ticket beside Request WFH)
    path('attendance/punch-ticket', views.attendance_punch_ticket),
    path('attendance/punch-ticket/pending', views.attendance_punch_ticket_pending),
    path('attendance/punch-ticket/<int:pk>/proof', views.attendance_punch_ticket_proof),
    path('attendance/punch-ticket/<int:pk>/decide', views.attendance_punch_ticket_decide),
    # Employee attendance detail + hourly location tracking
    path('attendance/day-detail', views.attendance_day_detail),
    path('attendance/location-punch', views.attendance_location_punch),
    path('attendance/<int:pk>', views.attendance_detail),

    # Shifts
    path('shifts', views.shifts),
    path('shifts/<int:pk>', views.shift_detail),
    path('shift-assignments', views.shift_assignments),
    path('shift-assignments/<int:pk>', views.shift_assignment_detail),

    # Employees module — Leave Management
    path('leave', views.leave),
    path('leave/balance', views.leave_balance),
    path('leave/<int:pk>', views.leave_detail),

    # Employees module — Task Tracker
    path('tasks', views.tasks),
    path('tasks/<int:pk>', views.task_detail),

    # Employees module — Work Submissions
    path('submissions', views.submissions),
    path('submissions/<int:pk>', views.submission_detail),
    path('submissions/<int:pk>/file', views.submission_file),
    path('submissions/<int:pk>/review-file', views.submission_review_file),

    # Recruit module — Bench Sales — Bench Submissions
    path('recruit/bench-submissions', views.bench_submissions),
    path('recruit/bench-submissions/<int:pk>', views.bench_submission_detail),

    # Recruit module — Bench Sales — consultant profile roster
    path('recruit/bench-sales', views.bench_sales_profiles),
    path('recruit/bench-sales/<int:pk>', views.bench_sales_profile_detail),
    path('recruit/bench-sales/<int:pk>/file/<str:field>', views.bench_sales_profile_file),
    path('recruit/bench-sales/<int:pk>/certificates/<int:cert_id>', views.bench_sales_profile_certificate_detail),
    path('recruit/bench-sales/<int:pk>/certificates/<int:cert_id>/file', views.bench_sales_profile_certificate_file),

    # Role-Based Access Control (RBAC)
    path('rbac/stats', views.rbac_stats),
    path('rbac/users', views.rbac_users),
    path('rbac/users/<int:pk>', views.rbac_user_detail),
    path('roles', views.roles),
    path('roles/<int:pk>', views.role_detail),
    path('roles/<int:pk>/groups', views.role_groups),
    path('roles/<int:pk>/permissions', views.role_permissions_view),
    path('permission-groups', views.permission_groups),
    path('permission-groups/<int:pk>', views.permission_group_detail),
    path('permission-groups/<int:pk>/permissions', views.permission_group_permissions),
    path('permissions', views.permissions),
    path('permissions/<int:pk>', views.permission_detail),
    path('modules', views.modules),
    path('companies', views.companies),
    path('me/permissions', views.my_permissions),

    path('dashboard/stats', views.dashboard_stats),
    path('config', views.client_config),
    path('health', views.health),

    # Recruitment KPI Dashboard
    path('recruitment/kpis', views.recruitment_kpis),

    # === Advanced Attendance Management API Endpoints ===
    # Shifts
    path('shifts/', attendance_views.ShiftViewSet.as_view({'get': 'list', 'post': 'create'})),
    path('shifts/active/', attendance_views.ShiftViewSet.as_view({'get': 'active'})),
    path('shifts/<int:pk>/', attendance_views.ShiftViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'})),
    path('shifts/<int:pk>/assignments/', attendance_views.ShiftViewSet.as_view({'get': 'assignments'})),

    # Check-In/Check-Out
    path('attendance/check-in/', attendance_views.AttendanceCheckInOutViewSet.as_view({'post': 'check_in'})),
    path('attendance/check-out/', attendance_views.AttendanceCheckInOutViewSet.as_view({'post': 'check_out'})),
    path('attendance/device-punch/', attendance_views.AttendanceCheckInOutViewSet.as_view({'post': 'device_punch'})),
    path('attendance/today/', attendance_views.AttendanceCheckInOutViewSet.as_view({'get': 'today'})),
    path('attendance/summary/', attendance_views.AttendanceCheckInOutViewSet.as_view({'get': 'summary'})),

    # Core Payroll Endpoints
    path('payroll/compensations', payroll_views.EmployeeCompensationViewSet.as_view({'get': 'list', 'post': 'create'})),
    path('payroll/compensations/<int:pk>', payroll_views.EmployeeCompensationViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'})),
    path('payroll/components', payroll_views.PayComponentViewSet.as_view({'get': 'list', 'post': 'create'})),
    path('payroll/components/<int:pk>', payroll_views.PayComponentViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'})),
    path('payroll/employee-components', payroll_views.EmployeePayComponentViewSet.as_view({'get': 'list', 'post': 'create'})),
    path('payroll/employee-components/<int:pk>', payroll_views.EmployeePayComponentViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'})),
    path('payroll/runs', payroll_views.PayrollRunViewSet.as_view({'get': 'list', 'post': 'create'})),
    path('payroll/runs/<int:pk>', payroll_views.PayrollRunViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'})),
    path('payroll/runs/<int:pk>/approve', payroll_views.PayrollRunViewSet.as_view({'post': 'approve'})),
    path('payroll/runs/<int:pk>/export-bank-file', payroll_views.PayrollRunViewSet.as_view({'get': 'export_bank_file'})),
    path('payroll/payslips', payroll_views.PayslipViewSet.as_view({'get': 'list', 'post': 'create'})),
    path('payroll/payslips/<int:pk>', payroll_views.PayslipViewSet.as_view({'get': 'retrieve'})),
    path('payroll/payslips/<int:pk>/pdf', payroll_views.PayslipViewSet.as_view({'get': 'download_pdf'})),
    path('payroll/settings', payroll_views.PayrollSettingViewSet.as_view({'get': 'list', 'post': 'create'})),

    # Breaks
    path('attendance/break/start/', attendance_views.BreakViewSet.as_view({'post': 'start'})),
    path('attendance/break/end/', attendance_views.BreakViewSet.as_view({'post': 'end'})),
    path('attendance/breaks/today/', attendance_views.BreakViewSet.as_view({'get': 'today'})),

    # Late Check-In Alerts
    path('attendance/late-alerts/', attendance_views.LateCheckInAlertViewSet.as_view({'get': 'list_alerts'})),
    path('attendance/late-alerts/excuse/', attendance_views.LateCheckInAlertViewSet.as_view({'post': 'excuse'})),

    # Overtime
    path('attendance/overtime/daily/', attendance_views.OvertimeViewSet.as_view({'get': 'daily'})),
    path('attendance/overtime/monthly/', attendance_views.OvertimeViewSet.as_view({'get': 'monthly'})),
    path('attendance/overtime/balance/', attendance_views.OvertimeViewSet.as_view({'get': 'balance'})),
    path('attendance/overtime/approve/', attendance_views.OvertimeViewSet.as_view({'post': 'approve'})),

    # Attendance Corrections
    path('attendance/correction/', attendance_views.AttendanceCorrectionViewSet.as_view({'post': 'request_correction'})),
    path('attendance/correction/approve/', attendance_views.AttendanceCorrectionViewSet.as_view({'post': 'approve_correction'})),
    path('attendance/correction/reject/', attendance_views.AttendanceCorrectionViewSet.as_view({'post': 'reject_correction'})),
    path('attendance/correction/pending/', attendance_views.AttendanceCorrectionViewSet.as_view({'get': 'pending'})),

    # WFH Requests
    path('attendance/wfh/submit/', attendance_views.WFHRequestViewSet.as_view({'post': 'submit_request'})),
    path('attendance/wfh/approve/', attendance_views.WFHRequestViewSet.as_view({'post': 'approve_request'})),
    path('attendance/wfh/reject/', attendance_views.WFHRequestViewSet.as_view({'post': 'reject_request'})),

    # Onboarding — work authorization, documents, and the candidate lifecycle.
    # Static segments precede <int:pk> so they are not swallowed by it.
    path('onboarding/dashboard', onboarding_views.onboarding_dashboard),
    path('onboarding/alerts', onboarding_views.onboarding_alerts),
    # One list per sidebar page — each returns what that page is about.
    path('onboarding/work-authorizations', onboarding_views.work_authorization_list),
    path('onboarding/documents', onboarding_views.documents_list),
    path('onboarding/verifications', onboarding_views.verifications_list),
    path('onboarding/asset-allocations', onboarding_views.asset_allocations_list),
    path('onboarding/payroll', onboarding_views.payroll_list),
    path('onboarding/field-config', onboarding_views.candidate_field_config),
    path('onboarding/work-auth-field-config', onboarding_views.work_auth_field_config),
    path('onboarding/work-auth-type-field-config', onboarding_views.work_auth_type_field_config),
    path('onboarding/form-templates', onboarding_views.candidate_form_templates),
    path('onboarding/form-templates/<int:pk>', onboarding_views.candidate_form_template_detail),
    path('onboarding/candidates', onboarding_views.candidates),
    path('onboarding/candidates/<int:pk>', onboarding_views.candidate_detail),
    path('onboarding/candidates/<int:pk>/timeline', onboarding_views.candidate_timeline),
    path('onboarding/candidates/<int:pk>/work-authorization', onboarding_views.candidate_work_authorization),
    path('onboarding/candidates/<int:pk>/documents', onboarding_views.candidate_documents),
    path('onboarding/candidates/<int:pk>/documents/<int:doc_id>', onboarding_views.candidate_document_detail),
    path('onboarding/candidates/<int:pk>/document-versions/<str:doc_type>', onboarding_views.candidate_document_versions),
    path('onboarding/candidates/<int:pk>/verify', onboarding_views.candidate_verification),
    path('onboarding/candidates/<int:pk>/approve', onboarding_views.candidate_approval),
    path('onboarding/candidates/<int:pk>/assets', onboarding_views.candidate_assets),
    path('onboarding/candidates/<int:pk>/assets/<int:asset_id>', onboarding_views.candidate_asset_detail),
    path('onboarding/candidates/<int:pk>/payroll', onboarding_views.candidate_payroll),
    path('onboarding/candidates/<int:pk>/activate', onboarding_views.candidate_activate),

    # Payroll form templates (admin)
    path('onboarding/forms', onboarding_views.payroll_templates),
    path('onboarding/forms/<int:pk>', onboarding_views.payroll_template_detail),

    # Candidate submissions (admin)
    path('onboarding/candidates/<int:pk>/submissions', onboarding_views.candidate_form_submissions),
    path('onboarding/candidates/<int:pk>/submissions/<int:sub_id>/pdf', onboarding_views.candidate_submission_pdf),
    path('onboarding/candidates/<int:pk>/send-portal-link', onboarding_views.send_portal_link),

    # Public portal endpoints (candidate)
    path('public/onboarding/forms', onboarding_views.public_candidate_forms),
    path('public/onboarding/forms/<int:form_id>', onboarding_views.public_form_template_detail),
    path('public/onboarding/submit-form', onboarding_views.public_submit_form),
    path('public/onboarding/upload-document', onboarding_views.public_upload_document),
    path('public/onboarding/complete', onboarding_views.public_complete_portal),
]

# --- Employee Chat routes (appended by chat-module integration) ---
urlpatterns += [
    path("chat/rooms", views.chat_rooms),
    path("chat/rooms/<int:room_id>", views.chat_room_detail),
    path("chat/messages/<int:room_id>", views.chat_messages),
    path("chat/message/<int:msg_id>", views.chat_message_detail),
    path("chat/message/<int:msg_id>/pin", views.chat_message_pin),
    path("chat/translate", views.chat_translate),           # POST translate composer text
    path("chat/attachment/<int:msg_id>", views.chat_attachment),
    path("chat/rooms/<int:room_id>/read", views.chat_mark_read),
    path("chat/rooms/<int:room_id>/members", views.chat_room_members),
    path("chat/rooms/<int:room_id>/members/<str:email>", views.chat_room_member_detail),
    path("chat/contacts", views.chat_contacts),
    path("chat/meetings", views.chat_meetings),
    path("chat/meetings/<int:meeting_id>", views.chat_meeting_detail),
]
