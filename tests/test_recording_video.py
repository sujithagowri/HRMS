"""GET /api/interview-recordings/<id>/video must honour Range.

It used to advertise ``Accept-Ranges: bytes`` and then ignore the header,
answering every request with 200 and the whole file — so a player could not
seek, and each attempt re-fetched the entire recording. It also read the whole
LONGBLOB into memory to serve any request at all.
"""
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'hrms_project.settings')

import django
from django.db import connection, reset_queries
from django.test import TestCase, Client, override_settings

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from api.models import InterviewRecording

WEBM_MAGIC = bytes([0x1A, 0x45, 0xDF, 0xA3])
BODY = WEBM_MAGIC + bytes(range(256)) * 40      # 10 244 bytes, deterministic


class RecordingVideoRangeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.rec = InterviewRecording.objects.create(
            candidate_name='srikanth', candidate_email='c@example.com',
            role='data analyst', verdict='HOLD',
            video_buffer=BODY, video_mime='video/webm',
            transcript='', responses=[],
        )
        self.url = f'/api/interview-recordings/{self.rec.id}/video'

    def _body(self, resp):
        if hasattr(resp, 'streaming_content'):
            return b''.join(resp.streaming_content)
        return resp.content

    def test_plain_get_returns_the_whole_file(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._body(resp), BODY)
        self.assertEqual(resp['Accept-Ranges'], 'bytes')
        self.assertEqual(resp['Content-Type'], 'video/webm')

    def test_open_range_returns_206(self):
        """This is what a <video> element sends on first load."""
        resp = self.client.get(self.url, HTTP_RANGE='bytes=0-')
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp['Content-Range'], f'bytes 0-{len(BODY)-1}/{len(BODY)}')
        self.assertEqual(self._body(resp), BODY)

    def test_a_seek_returns_only_that_window(self):
        resp = self.client.get(self.url, HTTP_RANGE='bytes=1000-1999')
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(self._body(resp), BODY[1000:2000])
        self.assertEqual(resp['Content-Range'], f'bytes 1000-1999/{len(BODY)}')
        self.assertEqual(resp['Content-Length'], '1000')

    def test_suffix_range(self):
        resp = self.client.get(self.url, HTTP_RANGE='bytes=-500')
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(self._body(resp), BODY[-500:])

    def test_range_past_the_end_is_416(self):
        resp = self.client.get(self.url, HTTP_RANGE='bytes=999999-')
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(resp['Content-Range'], f'bytes */{len(BODY)}')

    def test_a_range_request_does_not_read_the_whole_blob(self):
        """The slice is taken in MySQL; a 1 KB seek must not load the video."""
        with override_settings(DEBUG=True):
            reset_queries()
            self.client.get(self.url, HTTP_RANGE='bytes=0-1023')
            sql = ' '.join(q['sql'] for q in connection.queries)
        self.assertIn('SUBSTR' if connection.vendor == 'sqlite' else 'SUBSTRING', sql)
        self.assertNotIn('SELECT `interview_recordings`.`video_buffer`', sql)

    def test_missing_video_is_404_not_a_broken_stream(self):
        empty = InterviewRecording.objects.create(
            candidate_name='novideo', candidate_email='n@example.com',
            role='data analyst', verdict='HOLD', transcript='', responses=[],
        )
        resp = self.client.get(f'/api/interview-recordings/{empty.id}/video')
        self.assertEqual(resp.status_code, 404)

    def test_unknown_recording_is_404(self):
        self.assertEqual(self.client.get('/api/interview-recordings/999999/video').status_code, 404)
