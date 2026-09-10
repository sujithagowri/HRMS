"""Deterministic job-description parsing used by candidate sourcing."""
import re


KNOWN_SKILLS = (
    'python', 'django', 'flask', 'fastapi', 'java', 'spring', 'javascript',
    'typescript', 'react', 'angular', 'vue', 'node.js', 'nodejs', '.net', 'c#',
    'c++', 'go', 'golang', 'ruby', 'rails', 'php', 'laravel', 'sql', 'mysql',
    'postgresql', 'mongodb', 'redis', 'aws', 'azure', 'gcp', 'docker',
    'kubernetes', 'terraform', 'jenkins', 'git', 'rest', 'graphql', 'linux',
    'machine learning', 'data science', 'pandas', 'spark', 'salesforce',
    'figma', 'jira', 'agile', 'scrum', 'devops', 'cybersecurity',
)


def _contains(text, skill):
    pattern = r'(?<![\w])' + re.escape(skill) + r'(?![\w])'
    return bool(re.search(pattern, text, re.IGNORECASE))


def parse_requirements(description, title='', location='', remote=False):
    text = ' '.join(str(description or '').split())
    skills = []
    for skill in KNOWN_SKILLS:
        if _contains(text, skill):
            canonical = {'nodejs': 'node.js', 'golang': 'go'}.get(skill, skill)
            if canonical not in skills:
                skills.append(canonical)

    years = [int(n) for n in re.findall(
        r'(?<!\d)(\d{1,2})\s*\+?\s*(?:years?|yrs?)', text, re.IGNORECASE
    )]
    minimum_experience = min(years) if years else 0

    required, preferred = [], []
    for skill in skills:
        escaped = re.escape(skill)
        preference_pattern = (
            rf'(?:preferred|nice\s+to\s+have|bonus)[^.;\n]{{0,30}}{escaped}'
            rf'|{escaped}[^,;.\n]{{0,18}}(?:preferred|nice\s+to\s+have|bonus)'
        )
        target = preferred if re.search(preference_pattern, text, re.IGNORECASE) else required
        target.append(skill)

    return {
        'title': str(title or '').strip(),
        'requiredSkills': required,
        'preferredSkills': preferred,
        'minimumExperienceYears': minimum_experience,
        'location': str(location or '').strip(),
        'remoteAllowed': bool(remote),
    }
