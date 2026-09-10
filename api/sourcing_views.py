from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import JobPost, SourcingResult, SourcingSearch
from .permissions import require_perm
from .services.sourcing import parse_requirements, run_search
from .sourcing_serializers import SourcingResultSerializer, SourcingSearchSerializer


def _actor_email(request):
    return (
        request.META.get('HTTP_X_USER_EMAIL')
        or request.META.get('HTTP_X_ACTOR_EMAIL')
        or ''
    ).strip().lower()


@api_view(['GET', 'POST'])
@require_perm({'GET': 'sourcing.view', 'POST': 'sourcing.search'})
def sourcing_searches(request):
    if request.method == 'GET':
        rows = SourcingSearch.objects.select_related('job').all()
        return Response(SourcingSearchSerializer(rows, many=True).data)

    body = request.data or {}
    job = None
    if body.get('jobId') is not None:
        job = JobPost.objects.filter(pk=body.get('jobId')).first()
        if not job:
            return Response({'message': 'Job not found'}, status=404)

    description = str(body.get('jobDescription') or (job.description if job else '') or '').strip()
    if not description:
        return Response({'message': 'jobDescription or a job with a description is required'}, status=400)

    sources = body.get('sources') or ['internal']
    if not isinstance(sources, list) or not sources:
        return Response({'message': 'sources must be a non-empty array'}, status=400)
    unsupported = sorted(set(sources) - {'internal'})
    if unsupported:
        return Response({
            'message': 'One or more sources are not configured',
            'unsupportedSources': unsupported,
            'availableSources': ['internal'],
        }, status=400)

    title = str(body.get('jobTitle') or (job.title if job else '') or '').strip()
    requirements = parse_requirements(
        description,
        title=title,
        location=(job.location if job else body.get('location', '')),
        remote=(job.is_remote if job else body.get('remoteAllowed', False)),
    )
    # Recruiters may correct or augment deterministic extraction.
    overrides = body.get('requirements') or {}
    if not isinstance(overrides, dict):
        return Response({'message': 'requirements must be an object'}, status=400)
    requirements.update(overrides)

    search = SourcingSearch.objects.create(
        job=job,
        job_title=title,
        job_description=description,
        requirements=requirements,
        sources=sources,
        requested_by_email=_actor_email(request),
    )
    run_search(search)
    data = SourcingSearchSerializer(search).data
    data['results'] = SourcingResultSerializer(search.results.all(), many=True).data
    return Response(data, status=201)


@api_view(['GET'])
@require_perm('sourcing.view')
def sourcing_search_detail(request, pk):
    search = get_object_or_404(SourcingSearch.objects.select_related('job'), pk=pk)
    data = SourcingSearchSerializer(search).data
    data['results'] = SourcingResultSerializer(search.results.all(), many=True).data
    return Response(data)


@api_view(['GET'])
@require_perm('sourcing.view')
def sourcing_search_results(request, pk):
    search = get_object_or_404(SourcingSearch, pk=pk)
    results = search.results.all()
    minimum = request.GET.get('minimumScore')
    if minimum not in (None, ''):
        try:
            results = results.filter(match_score__gte=max(0, min(100, int(minimum))))
        except (TypeError, ValueError):
            return Response({'message': 'minimumScore must be an integer'}, status=400)
    return Response(SourcingResultSerializer(results, many=True).data)


@api_view(['POST'])
@require_perm('sourcing.shortlist')
def sourcing_result_shortlist(request, pk):
    result = get_object_or_404(SourcingResult, pk=pk)
    result.status = 'shortlisted'
    result.feedback = str(request.data.get('feedback') or result.feedback or '')
    result.reviewed_by_email = _actor_email(request)
    result.save(update_fields=['status', 'feedback', 'reviewed_by_email', 'updated_at'])
    return Response(SourcingResultSerializer(result).data)


@api_view(['POST'])
@require_perm('sourcing.review')
def sourcing_result_feedback(request, pk):
    result = get_object_or_404(SourcingResult, pk=pk)
    status_value = str(request.data.get('status') or '').strip().lower()
    if status_value not in {'new', 'reviewed', 'shortlisted', 'rejected'}:
        return Response({'message': 'status must be new, reviewed, shortlisted, or rejected'}, status=400)
    result.status = status_value
    result.feedback = str(request.data.get('feedback') or '')
    result.reviewed_by_email = _actor_email(request)
    result.save(update_fields=['status', 'feedback', 'reviewed_by_email', 'updated_at'])
    return Response(SourcingResultSerializer(result).data)
