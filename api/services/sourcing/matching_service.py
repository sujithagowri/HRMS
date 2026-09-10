"""Explainable, deterministic matching for normalized candidate profiles."""
import re


def _tokens(value):
    if isinstance(value, list):
        value = ' '.join(str(v) for v in value)
    return set(re.findall(r'[a-z0-9+#.]+', str(value or '').lower()))


def _skill_present(skill, candidate_text):
    skill_tokens = _tokens(skill)
    return bool(skill_tokens) and skill_tokens.issubset(candidate_text)


def score_candidate(requirements, profile):
    candidate_text = _tokens([
        profile.get('role', ''), profile.get('skills', []),
        profile.get('resumeText', ''),
    ])
    required = requirements.get('requiredSkills') or []
    preferred = requirements.get('preferredSkills') or []
    matched_required = [s for s in required if _skill_present(s, candidate_text)]
    matched_preferred = [s for s in preferred if _skill_present(s, candidate_text)]
    missing_required = [s for s in required if s not in matched_required]

    skill_ratio = len(matched_required) / len(required) if required else 1.0
    preferred_ratio = len(matched_preferred) / len(preferred) if preferred else 1.0

    job_title_tokens = _tokens(requirements.get('title'))
    role_tokens = _tokens(profile.get('role'))
    title_ratio = (
        len(job_title_tokens & role_tokens) / len(job_title_tokens)
        if job_title_tokens else 1.0
    )

    minimum_years = int(requirements.get('minimumExperienceYears') or 0)
    candidate_years = int(profile.get('experienceYears') or 0)
    experience_ratio = min(candidate_years / minimum_years, 1.0) if minimum_years else 1.0

    # Skills 55%, relevant title 20%, experience 20%, preferred skills 5%.
    score = round(55 * skill_ratio + 20 * title_ratio + 20 * experience_ratio + 5 * preferred_ratio)
    score = max(0, min(100, score))
    return score, {
        'matchedRequiredSkills': matched_required,
        'missingRequiredSkills': missing_required,
        'matchedPreferredSkills': matched_preferred,
        'candidateExperienceYears': candidate_years,
        'minimumExperienceYears': minimum_years,
        'titleMatchPercent': round(title_ratio * 100),
        'summary': (
            f'Matched {len(matched_required)} of {len(required)} required skills; '
            f'title similarity {round(title_ratio * 100)}%.'
        ),
    }
