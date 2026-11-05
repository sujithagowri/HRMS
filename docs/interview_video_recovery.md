# F2F interview video recovery

Implemented on `fix/f2f-video-recovery`, based on `sujitha-development` at `4ff2345`.
`git fetch origin` and `git pull --ff-only origin sujitha-development` completed;
the remote branch was already up to date. The recording changes are isolated from the original checkout’s sourcing edits.

## Problem and resulting behavior

The existing application saved interview text first, then uploaded a single video
request. A failed upload left a text-only interview with no retry or durable browser
copy. The final blob was also always labelled WebM, including when a browser chose MP4.

The recorder now writes each non-empty chunk to IndexedDB while the interview runs.
At submission it persists the final transcript and metadata, then creates the server
record with a random `clientUploadId`. The database enforces uniqueness of that UUID:
repeating a submission after a lost response returns the same recording ID and does
not overwrite its transcript, create another recording, or complete another interview link.

Video uploads retry transient errors up to three total attempts with exponential
backoff. Permanent HTTP errors, including 413, need user action instead of repeated
automatic requests. Pending finalized recordings retry when the browser comes back
online or the site is reopened. A visible recovery panel provides Retry upload,
Download video, and confirmed Discard local copy actions. A successful response must
confirm `videoStatus: ready` and the exact byte count before local chunks are removed.

Interrupted recordings are offered as partial downloads. They are **not** automatically
submitted as completed interviews. Storage failures warn the candidate to keep the
original page open; the complete in-memory video can still upload. A recovered job
known to have lost chunks is blocked from silently uploading a truncated recording.

The browser's actual MIME type is preserved through upload, playback and filename
extension. An interview cannot start when camera recording is unavailable or fails
to start. The final chunks are collected before submission, including when the
recorder has already stopped after an error.

## Status meanings

| Status | Where | Meaning |
| --- | --- | --- |
| Recording | Browser | Capture in progress; chunks are being retained locally. |
| Pending | API/browser | Text is saved or awaiting submission; video has not been confirmed. |
| Uploading | Browser | Video request is in progress. Interview completion remains separate. |
| Ready | API/browser | Server stored the video; browser verifies the byte count before cleanup. |
| Failed | API/browser | Retry or investigation is needed. A network failure may leave the API pending because the request never reached it. |
| Interrupted | Browser | Unfinished interview with partial local video; download only. |

The dashboard shows Play, Video pending or Video unavailable, refreshes every 30
seconds, and refreshes immediately after an upload succeeds in the same tab.
Non-empty video data is checked consistently in list and detail responses.
Existing records with stored video are classified ready by the migration; existing
text-only records are classified failed with `No video stored for this interview.`
This classification does not verify whether an existing media file is playable.

## Files changed for this implementation

| File | Change |
| --- | --- |
| `api/models.py` | Add `client_upload_id`, `video_status`, `video_error` to InterviewRecording. |
| `api/serializers.py` | Accept write-only UUID; return video status/error/MIME; fix empty-field availability flags. |
| `api/views.py` | Idempotent metadata creation; atomic interview completion; validate video MIME/header; acknowledge stored byte count; preserve existing video on rejected retries. |
| `api/migrations/0068_interview_video_recovery.py` | Add fields and classify existing records without reading blobs into Python. |
| `dist/dist/assets/hrms-recording-recovery.js` | New browser recovery, persistence, retries, upload panel and retention handling. |
| `dist/dist/assets/index-DCh6bk0Rv4.js` | Small integration hooks into the shipped recorder, submission, dashboard and download paths. |
| `dist/dist/index.html` | Load recovery module before the application; bump application cache version to 79. |
| `tests/test_recording_recovery.py` | 11 backend regression tests. |
| `tests/test_recording_recovery.js` | Seven browser-module recovery scenarios using DOM/network/IndexedDB test doubles. |
| `tests/test_recording_video.py` | Make the existing SQL slicing assertion work with SQLite's SUBSTR and MySQL's SUBSTRING. |
| `docs/interview_video_recovery.md` | This implementation, validation and deployment record. |

This checkout already had candidate-sourcing changes in models, serializers,
requirements, sourcing services, tests, and `docs/contact_normalization.md`. Those
changes were preserved. The recording migration depends directly on the existing committed migration leaves
0066 and 0059; it does not require the uncommitted contact-fields migration.

The repository ships a pre-built React bundle and has no matching interview React
source file. The integration therefore follows the repository's existing pattern:
keep the feature in a readable `hrms-*.js` module and add minimal bundle hooks.
A future replacement/rebuild of that bundle must retain these integration calls.

## Validation

- 31 backend tests passed: existing video Range/seek tests, interview-score tests,
  and the new recovery tests.
- Seven browser-module scenarios passed: lost metadata response, offline/reload
  recovery, failed-upload/online retry, interrupted interview handling, byte-count
  mismatch, storage quota failure, and expired-data cleanup.
- The new migration applied and reversed against an isolated in-memory SQLite
  database; existing ready/missing recordings were classified correctly.
- Django `makemigrations --check --dry-run` reported no changes.
- JavaScript syntax checks and `git diff --check` passed.

The normal SQLite test command encounters pre-existing MySQL-specific migration
SQL (`near "ON": syntax error`). Backend regression tests were therefore run with
the API schema synchronized from models; the new migration was checked separately.
No production MySQL migration or real-camera/browser smoke test has been performed.

Reproduce the backend regression run from the repository using its Python environment:

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'hrms_project.test_settings'
from django.conf import settings
settings.MIGRATION_MODULES = {'api': None}
import django
django.setup()
from django.core.management import call_command
call_command('test', 'tests.test_recording_video', 'tests.test_interview_score',
             'tests.test_recording_recovery', interactive=False)
```

```sh
node tests/test_recording_recovery.js
node --check dist/dist/assets/hrms-recording-recovery.js
node --input-type=module --check < dist/dist/assets/index-DCh6bk0Rv4.js
python manage.py makemigrations --check --dry-run --settings=hrms_project.test_settings
git diff --check
```

## Deployment and browser verification

1. Deploy the backend and recording migration together.
2. Run `python manage.py migrate` against the configured MySQL database before
   serving the new application code.
3. Publish the updated assets/index and run the deployment's usual static collection
   and application restart. Keep the HTML cache-version update.
4. Complete a short camera interview and verify the server reports ready and the
   dashboard video plays, seeks and downloads with the appropriate extension.
5. Disconnect the network during submission. Verify text/video remain in the recovery
   panel, then reconnect or reload. Confirm there is one recording ID and playable video.
6. Close/reload during capture. Verify partial download is available and the interview
   has not been submitted as completed. Also check rejected camera permission and a
   browser that chooses MP4.

## Limits and follow-up

- Browser copies are retained for up to 24 hours and purged on the next visit after
  that age. Recovery needs the same browser profile and site origin; clearing site
  data/private mode/storage eviction can remove it. No video is stored in localStorage.
- Existing videos that were never uploaded cannot be reconstructed from transcripts.
  Recovery requires a browser copy or backup.
- The server still receives one binary video request and stores it in MySQL. Proxy
  body limits, Passenger timeouts, and MySQL `max_allowed_packet` must accommodate
  the expected recording size. A 413 remains actionable in the recovery panel.
  Resumable server uploads and object storage are a separate follow-up for long interviews.
- The existing upload endpoint's access policy is unchanged. Recovery uses the same
  endpoint and recruitment permissions as the original flow.
