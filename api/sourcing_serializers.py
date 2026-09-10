from rest_framework import serializers

from .models import SourcingResult, SourcingSearch


class SourcingResultSerializer(serializers.ModelSerializer):
    candidateName = serializers.CharField(source='candidate_name', read_only=True)
    candidateRole = serializers.CharField(source='candidate_role', read_only=True)
    matchScore = serializers.IntegerField(source='match_score', read_only=True)
    profileUrl = serializers.CharField(source='profile_url', read_only=True)
    normalizedProfile = serializers.JSONField(source='normalized_profile', read_only=True)

    class Meta:
        model = SourcingResult
        fields = [
            'id', 'source', 'candidateName', 'candidateRole', 'profileUrl',
            'normalizedProfile', 'matchScore', 'explanation', 'status',
            'feedback', 'created_at', 'updated_at',
        ]


class SourcingSearchSerializer(serializers.ModelSerializer):
    jobId = serializers.IntegerField(source='job_id', read_only=True)
    jobTitle = serializers.CharField(source='job_title', read_only=True)
    jobDescription = serializers.CharField(source='job_description', read_only=True)
    resultCount = serializers.IntegerField(source='result_count', read_only=True)
    requestedByEmail = serializers.CharField(source='requested_by_email', read_only=True)

    class Meta:
        model = SourcingSearch
        fields = [
            'id', 'jobId', 'jobTitle', 'jobDescription', 'requirements',
            'sources', 'status', 'resultCount', 'requestedByEmail', 'error',
            'created_at', 'completed_at',
        ]
