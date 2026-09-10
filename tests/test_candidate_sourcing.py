from django.test import TestCase
from rest_framework.test import APIClient

from api.models import AppUser, JobPost, ResumeScore, SourcingResult, SourcingSearch
from api.services.sourcing.matching_service import score_candidate
from api.services.sourcing.requirement_parser import parse_requirements


class CandidateMatchingTests(TestCase):
    def test_requirement_parser_extracts_skills_and_experience(self):
        parsed = parse_requirements(
            'Requires 5+ years of Python, Django and AWS. Docker is preferred.',
            title='Senior Python Developer',
        )
        self.assertEqual(parsed['minimumExperienceYears'], 5)
        self.assertIn('python', parsed['requiredSkills'])
        self.assertIn('django', parsed['requiredSkills'])
        self.assertIn('docker', parsed['preferredSkills'])

    def test_better_candidate_receives_higher_score(self):
        requirements = parse_requirements(
            'Requires 5 years of Python and Django. AWS preferred.',
            title='Python Developer',
        )
        strong, explanation = score_candidate(requirements, {
            'role': 'Senior Python Developer',
            'skills': ['Python', 'Django', 'AWS'],
            'resumeText': 'Six years building Django REST APIs.',
            'experienceYears': 6,
        })
        weak, _ = score_candidate(requirements, {
            'role': 'Graphic Designer', 'skills': ['Figma'],
            'resumeText': '', 'experienceYears': 2,
        })
        self.assertGreater(strong, weak)
        self.assertEqual(explanation['missingRequiredSkills'], [])


class CandidateSourcingApiTests(TestCase):
    def setUp(self):
        self.user = AppUser.objects.create(
            full_name='Admin', email='admin@example.com', role='admin', status='active'
        )
        self.job = JobPost.objects.create(
            title='Python Developer', dept='Engineering',
            description='Requires 3 years of Python and Django. AWS preferred.',
        )
        ResumeScore.objects.create(
            name='Ada Candidate', initials='AC', role='Python Developer',
            score=90, skills=['Python', 'Django', 'AWS'],
            resume_text='Four years of Python and Django development.',
        )
        self.client = APIClient()
        self.client.credentials(HTTP_X_USER_EMAIL=self.user.email)

    def test_search_creates_ranked_internal_results(self):
        response = self.client.post(
            '/api/sourcing/searches',
            {'jobId': self.job.pk, 'sources': ['internal']},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'completed')
        self.assertEqual(response.data['resultCount'], 1)
        self.assertEqual(response.data['results'][0]['candidateName'], 'Ada Candidate')
        self.assertGreater(response.data['results'][0]['matchScore'], 70)
        self.assertEqual(SourcingSearch.objects.count(), 1)
        self.assertEqual(SourcingResult.objects.count(), 1)

    def test_unconfigured_external_source_is_rejected(self):
        response = self.client.post(
            '/api/sourcing/searches',
            {'jobId': self.job.pk, 'sources': ['linkedin']},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['availableSources'], ['internal'])
