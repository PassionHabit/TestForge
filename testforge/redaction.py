import re


def redact_log(content: str, pattern: str) -> str:
    if not pattern:
        return content
    return re.sub(pattern, lambda match: f"{match.group(1)}=[REDACTED]", content)
