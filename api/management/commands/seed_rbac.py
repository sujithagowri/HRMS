from django.core.management.base import BaseCommand
from django.db import transaction
from api.models import Module, PermissionGroup, Permission, Role, RolePermission, AppUser

class Command(BaseCommand):
    help = 'Seeds default Modules, Permission Groups, Permissions, and Roles for RBAC.'

    def handle(self, *args, **options):
        self.stdout.write("Seeding RBAC default data...")
        with transaction.atomic():
            # 1. Modules
            modules_data = [
                {'name': 'Dashboard', 'icon': 'dashboard', 'order': 1},
                {'name': 'Recruitment', 'icon': 'briefcase', 'order': 2},
                {'name': 'Employees', 'icon': 'users', 'order': 3},
                {'name': 'Attendance', 'icon': 'clock', 'order': 4},
                {'name': 'Leave', 'icon': 'calendar', 'order': 5},
                {'name': 'Payroll', 'icon': 'credit-card', 'order': 6},
                {'name': 'Settings', 'icon': 'settings', 'order': 7},
                {'name': 'RBAC', 'icon': 'shield', 'order': 8},
                {'name': 'Onboarding', 'icon': 'user-check', 'order': 9},
            ]
            modules = {}
            for m_info in modules_data:
                m, created = Module.objects.get_or_create(
                    name=m_info['name'],
                    defaults={'icon': m_info['icon'], 'order': m_info['order'], 'is_active': True}
                )
                modules[m_info['name']] = m
                if created:
                    self.stdout.write(f"  Created Module: {m.name}")

            # 2. Permission Groups
            groups_data = [
                {'name': 'Recruitment Group', 'module': 'Recruitment', 'desc': 'Manage jobs, candidates, and interviews'},
                {'name': 'Employee Group', 'module': 'Employees', 'desc': 'Manage employee records and tasks'},
                {'name': 'Attendance Group', 'module': 'Attendance', 'desc': 'Track and edit attendance logs'},
                {'name': 'Leave Group', 'module': 'Leave', 'desc': 'Create, view, and approve/decline leave requests'},
                {'name': 'Payroll Group', 'module': 'Payroll', 'desc': 'Manage employee salaries and payslips'},
                {'name': 'Settings Group', 'module': 'Settings', 'desc': 'Manage general settings and credentials'},
                {'name': 'RBAC Group', 'module': 'RBAC', 'desc': 'Manage roles, permissions, and user access'},
                {'name': 'Onboarding Group', 'module': 'Onboarding', 'desc': 'Work authorization, documents, verification, and employee onboarding'},
            ]
            groups = {}
            for g_info in groups_data:
                g, created = PermissionGroup.objects.get_or_create(
                    name=g_info['name'],
                    defaults={
                        'module': modules[g_info['module']],
                        'description': g_info['desc'],
                        'is_active': True
                    }
                )
                groups[g_info['name']] = g
                if created:
                    self.stdout.write(f"  Created Permission Group: {g.name}")

            # 3. Permissions
            perms_data = [
                # Recruitment
                {'name': 'View Recruitment', 'code': 'recruitment.view', 'group': 'Recruitment Group'},
                {'name': 'Create Jobs/Interviews', 'code': 'recruitment.create', 'group': 'Recruitment Group'},
                {'name': 'Edit Jobs/Interviews', 'code': 'recruitment.edit', 'group': 'Recruitment Group'},
                {'name': 'Delete Jobs/Interviews', 'code': 'recruitment.delete', 'group': 'Recruitment Group'},
                # KPI dashboard scopes — "My View" is the recruiter's own numbers,
                # "Org View" exposes every recruiter's, so they are granted separately.
                {'name': 'KPI Dashboard — My View', 'code': 'recruitment.kpi.view_own', 'group': 'Recruitment Group'},
                {'name': 'KPI Dashboard — Org View', 'code': 'recruitment.kpi.view_org', 'group': 'Recruitment Group'},
                {'name': 'View Candidate Sourcing', 'code': 'sourcing.view', 'group': 'Recruitment Group'},
                {'name': 'Run Candidate Searches', 'code': 'sourcing.search', 'group': 'Recruitment Group'},
                {'name': 'Review Sourcing Results', 'code': 'sourcing.review', 'group': 'Recruitment Group'},
                {'name': 'Shortlist Sourced Candidates', 'code': 'sourcing.shortlist', 'group': 'Recruitment Group'},
                # Employees
                {'name': 'View Employee Data', 'code': 'employee.view', 'group': 'Employee Group'},
                {'name': 'Create Employee/Tasks', 'code': 'employee.create', 'group': 'Employee Group'},
                {'name': 'Edit Employee/Tasks', 'code': 'employee.edit', 'group': 'Employee Group'},
                {'name': 'Delete Employee/Tasks', 'code': 'employee.delete', 'group': 'Employee Group'},
                # Scoped to the Task Tracker only (task_detail), separate from the
                # generic employee.edit/delete Work Submissions also uses — so
                # granting a reviewer task edit/delete never also unlocks
                # Work Submissions editing for them. See migration 0062.
                {'name': 'Edit Tasks', 'code': 'task.edit', 'group': 'Employee Group'},
                {'name': 'Delete Tasks', 'code': 'task.delete', 'group': 'Employee Group'},
                # Work Submissions scope. employee.view reaches the module;
                # submission.view_all decides whether you see the whole queue or
                # only what you filed yourself. Employee is deliberately left
                # without it. Mirrors the KPI My View / Org View split above.
                {'name': 'View All Submissions', 'code': 'submission.view_all', 'group': 'Employee Group'},
                # Attendance
                {'name': 'View Attendance Logs', 'code': 'attendance.view', 'group': 'Attendance Group'},
                {'name': 'Create Attendance Log', 'code': 'attendance.create', 'group': 'Attendance Group'},
                {'name': 'Edit Attendance Log', 'code': 'attendance.edit', 'group': 'Attendance Group'},
                {'name': 'Delete Attendance Log', 'code': 'attendance.delete', 'group': 'Attendance Group'},
                # Standing remote access: switch to Remote without filing a WFH
                # request each time. Without it, an approved WfhRequest is required.
                {'name': 'Work Remotely (no approval needed)', 'code': 'attendance.remote', 'group': 'Attendance Group'},
                # The two approval decisions. Separate from attendance.edit so
                # correcting a mistyped punch does not also let someone clear a
                # check-in from outside the geofence.
                {'name': 'Approve / Reject WFH Requests', 'code': 'attendance.approve_wfh', 'group': 'Attendance Group'},
                {'name': 'Approve / Reject Off-site Check-ins', 'code': 'attendance.approve_offsite', 'group': 'Attendance Group'},
                # Whether someone is onsite, hybrid or fully remote. A 'remote'
                # arrangement exempts every check-in from the geofence, so this
                # is the control the fence ultimately rests on.
                {'name': 'Manage Work Arrangements (onsite / hybrid / remote)', 'code': 'attendance.manage_arrangement', 'group': 'Attendance Group'},
                # Leave
                {'name': 'View Leave Requests', 'code': 'leave.view', 'group': 'Leave Group'},
                {'name': 'Create Leave Request', 'code': 'leave.create', 'group': 'Leave Group'},
                {'name': 'Action (Approve / Reject)', 'code': 'leave.action', 'group': 'Leave Group'},
                {'name': 'Delete Leave Request', 'code': 'leave.delete', 'group': 'Leave Group'},
                # Payroll
                {'name': 'View Payroll', 'code': 'payroll.view', 'group': 'Payroll Group'},
                {'name': 'Manage Payroll', 'code': 'payroll.manage', 'group': 'Payroll Group'},
                # Settings
                {'name': 'View Settings', 'code': 'settings.view', 'group': 'Settings Group'},
                {'name': 'Manage Settings', 'code': 'settings.manage', 'group': 'Settings Group'},
                # RBAC
                {'name': 'View Access Control', 'code': 'rbac.view', 'group': 'RBAC Group'},
                {'name': 'Manage Access Control', 'code': 'rbac.manage', 'group': 'RBAC Group'},
                # Onboarding — the workflow stages are granted separately so a
                # Manager can approve without being able to edit bank details,
                # and IT can allocate assets without seeing payroll.
                {'name': 'View Onboarding', 'code': 'onboarding.view', 'group': 'Onboarding Group'},
                {'name': 'Create Candidate', 'code': 'onboarding.create', 'group': 'Onboarding Group'},
                {'name': 'Edit Candidate / Work Authorization', 'code': 'onboarding.edit', 'group': 'Onboarding Group'},
                {'name': 'Delete Candidate', 'code': 'onboarding.delete', 'group': 'Onboarding Group'},
                {'name': 'Verify Documents (HR)', 'code': 'onboarding.verify', 'group': 'Onboarding Group'},
                {'name': 'Approve Onboarding (Manager)', 'code': 'onboarding.approve', 'group': 'Onboarding Group'},
                {'name': 'Allocate IT Assets', 'code': 'onboarding.assets', 'group': 'Onboarding Group'},
                {'name': 'Complete Payroll Information', 'code': 'onboarding.payroll', 'group': 'Onboarding Group'},
                {'name': 'Manage Onboarding Settings', 'code': 'onboarding.settings', 'group': 'Onboarding Group'},
            ]
            permissions = {}
            for p_info in perms_data:
                p, created = Permission.objects.get_or_create(
                    code=p_info['code'],
                    defaults={
                        'name': p_info['name'],
                        'group': groups[p_info['group']],
                        'is_active': True
                    }
                )
                permissions[p_info['code']] = p
                if created:
                    self.stdout.write(f"  Created Permission: {p.code}")

            # 4. Roles
            roles_data = [
                {'name': 'Super Admin', 'desc': 'Full control over the entire system (bypasses all checks)'},
                {'name': 'HR Manager', 'desc': 'Manage recruitment, employees, leaves, payroll, and settings'},
                {'name': 'HR Executive', 'desc': 'Manage candidates, interviews, and view employee records'},
                {'name': 'Employee', 'desc': 'View dashboard, check in/out, apply leave, and track own tasks'},
                # Onboarding workflow roles. Each owns exactly one stage, so no
                # single role can drive a candidate end-to-end unaided.
                {'name': 'Manager', 'desc': 'Approve, reject, or return onboarding candidates'},
                {'name': 'Team Lead', 'desc': 'Review work submitted by their team; view attendance and leave'},
                {'name': 'IT Admin', 'desc': 'Allocate and track IT assets for onboarding candidates'},
                {'name': 'Payroll Admin', 'desc': 'Complete payroll information for onboarding candidates'},
                {'name': 'Recruiter', 'desc': 'Create and edit onboarding candidates'},
            ]
            roles = {}
            for r_info in roles_data:
                r, created = Role.objects.get_or_create(
                    name=r_info['name'],
                    defaults={'description': r_info['desc'], 'is_active': True}
                )
                roles[r_info['name']] = r
                if created:
                    self.stdout.write(f"  Created Role: {r.name}")

            # 5. Grant permissions to Roles
            grants = {
                'Super Admin': list(permissions.keys()),  # gets all
                # Everything except RBAC management and manager approval.
                # Onboarding splits those two duties on purpose: HR verifies the
                # documents and a Manager approves them, so an HR Manager
                # holding both could clear their own verification. Mirrors the
                # grants in migration 0027, which withholds .approve here too.
                'HR Manager': [
                    code for code in permissions.keys()
                    if code not in ('rbac.manage', 'onboarding.approve')
                ],
                'HR Executive': [
                    'recruitment.view', 'recruitment.create', 'recruitment.edit',
                    'recruitment.kpi.view_own',
                    'sourcing.view', 'sourcing.search', 'sourcing.review',
                    'sourcing.shortlist',
                    'employee.view', 'submission.view_all', 'task.edit',
                    'attendance.view', 'leave.view', 'settings.view',
                    # HR Executive is the recruiter today: creates and edits the
                    # candidate, but cannot verify, approve, or touch payroll.
                    'onboarding.view', 'onboarding.create', 'onboarding.edit',
                ],
                'Employee': [
                    # task.edit (not task.delete) lets an employee accept/reject
                    # their own assigned tasks from the To Do column.
                    'employee.view', 'task.edit', 'attendance.view', 'attendance.create',
                    'leave.view', 'leave.create', 'settings.view'
                ],
                'Recruiter': [
                    'recruitment.view', 'recruitment.create', 'recruitment.edit',
                    'recruitment.kpi.view_own', 'settings.view',
                    'sourcing.view', 'sourcing.search', 'sourcing.review',
                    'sourcing.shortlist',
                    'onboarding.view', 'onboarding.create', 'onboarding.edit',
                ],
                'Manager': [
                    'employee.view', 'submission.view_all', 'task.edit', 'task.delete',
                    'attendance.view', 'leave.view', 'leave.action',
                    'settings.view',
                    'onboarding.view', 'onboarding.approve',
                ],
                # A lead reviews their team's work. Deliberately without
                # submission.action / leave.action / onboarding.* — grant those
                # from the RBAC screen if a lead should approve as well as see.
                'Team Lead': [
                    'employee.view', 'submission.view_all', 'task.edit', 'task.delete',
                    'attendance.view', 'leave.view', 'settings.view',
                ],
                'IT Admin': [
                    'settings.view',
                    'onboarding.view', 'onboarding.assets',
                ],
                'Payroll Admin': [
                    'payroll.view', 'payroll.manage', 'settings.view',
                    'onboarding.view', 'onboarding.payroll',
                ],
            }

            for role_name, perm_codes in grants.items():
                role = roles[role_name]
                for code in perm_codes:
                    perm = permissions[code]
                    RolePermission.objects.get_or_create(role=role, permission=perm)
                self.stdout.write(f"  Assigned {len(perm_codes)} permissions to Role: {role_name}")

            # 6. Map existing users to Roles if not already mapped
            for u in AppUser.objects.all():
                if not u.role_ref:
                    legacy_role = (u.role or '').lower()
                    if legacy_role == 'admin':
                        u.role_ref = roles['Super Admin']
                    elif legacy_role == 'hr':
                        u.role_ref = roles['HR Manager']
                    elif legacy_role == 'recruitment':
                        u.role_ref = roles['HR Executive']
                    else:
                        u.role_ref = roles['Employee']
                    u.save()
                    self.stdout.write(f"  Mapped user {u.email} to Role: {u.role_ref.name}")

        self.stdout.write(self.style.SUCCESS("Successfully seeded RBAC defaults!"))
