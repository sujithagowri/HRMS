import re

from api.models import ResumeScore

from .base import CandidateSourceConnector


class InternalResumeConnector(CandidateSourceConnector):
    source_name = 'internal'

    @staticmethod
    def _experience_years(resume):
        text = ' '.join((resume.resume_text or '').split())
        values = [int(n) for n in re.findall(
            r'(?<!\d)(\d{1,2})\s*\+?\s*(?:years?|yrs?)', text, re.IGNORECASE
        )]
        return max(values) if values else 0

    def search(self, requirements, filters=None):
        candidates = []
        for resume in ResumeScore.objects.all().iterator():
            candidates.append({
                'externalId': str(resume.pk),
                'resumeId': resume.pk,
                'name': resume.name,
                'role': resume.role or '',
                'skills': resume.skills if isinstance(resume.skills, list) else [],
                'resumeText': resume.resume_text or '',
                'experienceYears': self._experience_years(resume),
                'originalScore': resume.score,
                'source': self.source_name,
            })
        return candidates
