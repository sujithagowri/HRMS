from django.utils import timezone

from api.models import SourcingResult

from .connectors import InternalResumeConnector
from .matching_service import score_candidate


CONNECTORS = {'internal': InternalResumeConnector}


def run_search(search):
    """Run enabled connectors synchronously and persist ranked results."""
    search.status = 'running'
    search.error = ''
    search.save(update_fields=['status', 'error'])
    created = []
    try:
        for source in search.sources or ['internal']:
            connector_class = CONNECTORS.get(source)
            if not connector_class:
                raise ValueError(f'Unsupported or unconfigured candidate source: {source}')
            for candidate in connector_class().search(search.requirements):
                score, explanation = score_candidate(search.requirements, candidate)
                result, _ = SourcingResult.objects.update_or_create(
                    search=search,
                    source=source,
                    external_id=candidate['externalId'],
                    defaults={
                        'resume_id': candidate.get('resumeId'),
                        'candidate_name': candidate.get('name') or 'Unknown candidate',
                        'candidate_role': candidate.get('role') or '',
                        'normalized_profile': candidate,
                        'match_score': score,
                        'explanation': explanation,
                    },
                )
                created.append(result)
        search.status = 'completed'
        search.result_count = search.results.count()
        search.completed_at = timezone.now()
        search.save(update_fields=['status', 'result_count', 'completed_at'])
        return created
    except Exception as exc:
        search.status = 'failed'
        search.error = str(exc)
        search.completed_at = timezone.now()
        search.save(update_fields=['status', 'error', 'completed_at'])
        raise
