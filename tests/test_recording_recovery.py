"""F2F upload retries must preserve text, video and recording identity."""
import importlib
import uuid

from django.apps import apps
from django.db import connection
from django.test import Client, TestCase

from api.models import AppUser, InterviewLink, InterviewRecording
from api.serializers import InterviewRecordingSerializer
from api.views import _recording_list_qs


WEBM = b'\x1a\x45\xdf\xa3' + bytes(range(256))
MP4 = b'\x00\x00\x00\x18ftypisom' + bytes(range(128))


class RecordingRecoveryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.payload = {
            'candidateName': 'Candidate', 'candidateEmail': 'candidate@example.com',
            'role': 'Engineer', 'transcript': 'The answer', 'responses': [],
            'clientUploadId': str(uuid.uuid4()), 'totalScore': 80,
        }

    def create_recording(self):
        response = self.client.post('/api/interview-recordings', self.payload,
                                    content_type='application/json')
        self.assertEqual(response.status_code, 201)
        return response.json()

    def upload(self, pk, body=WEBM, mime='video/webm'):
        return self.client.post(f'/api/interview-recordings/{pk}/video', body,
                                content_type=mime)

    def test_retry_after_lost_create_response_returns_same_recording(self):
        first = self.create_recording()
        self.payload['transcript'] = 'A retry must not overwrite the original'
        second = self.client.post('/api/interview-recordings', self.payload,
                                 content_type='application/json')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['id'], first['id'])
        self.assertEqual(InterviewRecording.objects.count(), 1)
        self.assertEqual(second.json()['transcript'], 'The answer')
        self.assertNotIn('clientUploadId', second.json())

    def test_retry_does_not_complete_another_interview_link(self):
        first = InterviewLink.objects.create(name='Candidate', email=self.payload['candidateEmail'],
                                            role='Engineer', candidate_token='first')
        self.create_recording()
        first.refresh_from_db()
        self.assertIsNotNone(first.completed_at)
        next_link = InterviewLink.objects.create(name='Candidate', email=first.email,
                                                role='Engineer', candidate_token='next')
        self.client.post('/api/interview-recordings', self.payload, content_type='application/json')
        next_link.refresh_from_db()
        self.assertIsNone(next_link.completed_at)

    def test_empty_upload_preserves_text_and_can_be_retried(self):
        rec = self.create_recording()
        self.assertEqual(rec['videoStatus'], 'pending')
        self.assertFalse(rec['hasVideo'])
        self.assertEqual(self.upload(rec['id'], b'').status_code, 400)
        obj = InterviewRecording.objects.get(pk=rec['id'])
        self.assertEqual(obj.transcript, 'The answer')
        self.assertEqual(obj.video_status, 'failed')
        result = self.upload(rec['id']).json()
        self.assertEqual(result, {'ok': True, 'videoStatus': 'ready', 'videoBytes': len(WEBM)})
        obj.refresh_from_db()
        self.assertEqual(bytes(obj.video_buffer), WEBM)
        self.assertEqual(obj.video_error, '')

    def test_upload_retry_keeps_same_row_and_same_bytes(self):
        rec = self.create_recording()
        for _ in range(2):
            self.assertEqual(self.upload(rec['id']).status_code, 200)
        self.assertEqual(InterviewRecording.objects.count(), 1)
        self.assertEqual(self.client.get(f"/api/interview-recordings/{rec['id']}/video").content, WEBM)

    def test_rejected_retry_does_not_invalidate_saved_video(self):
        rec = self.create_recording()
        self.upload(rec['id'])
        self.assertEqual(self.upload(rec['id'], b'').status_code, 400)
        self.assertEqual(self.upload(rec['id'], b'broken').status_code, 400)
        obj = InterviewRecording.objects.get(pk=rec['id'])
        self.assertEqual(obj.video_status, 'ready')
        self.assertEqual(bytes(obj.video_buffer), WEBM)

    def test_mp4_mime_is_preserved_for_playback(self):
        rec = self.create_recording()
        self.assertEqual(self.upload(rec['id'], MP4, 'video/mp4;codecs=avc1').status_code, 200)
        response = self.client.get(f"/api/interview-recordings/{rec['id']}/video", HTTP_RANGE='bytes=0-7')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response['Content-Type'], 'video/mp4')
        self.assertEqual(response.content, MP4[:8])
        self.assertEqual(InterviewRecordingSerializer(
            InterviewRecording.objects.get(pk=rec['id'])).data['videoMime'], 'video/mp4')

    def test_invalid_mime_and_malformed_uuid_are_rejected(self):
        rec = self.create_recording()
        self.assertEqual(self.upload(rec['id'], WEBM, 'text/plain').status_code, 400)
        self.payload['clientUploadId'] = 'invalid'
        response = self.client.post('/api/interview-recordings', self.payload, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(InterviewRecording.objects.count(), 1)

    def test_empty_non_null_fields_have_consistent_list_and_detail_flags(self):
        obj = InterviewRecording.objects.create(candidate_name='Empty', video_buffer=b'', recording_data='')
        for record in (obj, _recording_list_qs().get(pk=obj.pk)):
            data = InterviewRecordingSerializer(record).data
            self.assertFalse(data['hasVideo'])
            self.assertFalse(data['hasRecording'])
            self.assertEqual(data['videoStatus'], 'pending')

    def test_list_does_not_read_video_blobs(self):
        InterviewRecording.objects.create(candidate_name='Ready', video_buffer=WEBM)
        InterviewRecording.objects.create(candidate_name='Pending')
        with self.assertNumQueries(1):
            data = InterviewRecordingSerializer(_recording_list_qs(), many=True).data
        self.assertEqual([r['videoStatus'] for r in data], ['ready', 'pending'])

    def test_existing_recordings_are_classified_without_loading_blobs(self):
        ready = InterviewRecording.objects.create(candidate_name='Ready', video_buffer=WEBM)
        missing = InterviewRecording.objects.create(candidate_name='Missing', video_buffer=b'', recording_data='')
        legacy = InterviewRecording.objects.create(candidate_name='Legacy', recording_data='base64')
        migration = importlib.import_module('api.migrations.0068_interview_video_recovery')
        migration.classify_existing(apps, connection.schema_editor())
        for obj, status in ((ready, 'ready'), (missing, 'failed'), (legacy, 'ready')):
            obj.refresh_from_db()
            self.assertEqual(obj.video_status, status)

    def test_status_is_returned_on_authorized_list_and_detail(self):
        rec = self.create_recording()
        AppUser.objects.create(email='admin@example.com', role='admin')
        headers = {'HTTP_X_USER_EMAIL': 'admin@example.com'}
        for url in ('/api/interview-recordings', f"/api/interview-recordings/{rec['id']}"):
            response = self.client.get(url, **headers)
            self.assertEqual(response.status_code, 200)
            value = response.json()
            value = value[0] if isinstance(value, list) else value
            self.assertEqual(value['videoStatus'], 'pending')
            self.assertFalse(value['hasVideo'])
