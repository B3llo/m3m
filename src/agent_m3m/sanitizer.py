"""Deterministic Secret Sanitizer for agent-m3m.

Filters out sensitive credentials, API keys, tokens, and private data before saving memories.
"""

import re
from typing import Tuple

SECRET_PATTERNS = [
    # OpenAI / Anthropic / GitHub / AWS / Generic API Keys
    (r"sk-[a-zA-Z0-9_-]{20,}", "[REDACTED_API_KEY]"),
    (r"sk-ant-api[0-9a-zA-Z_-]{20,}", "[REDACTED_ANTHROPIC_KEY]"),
    (r"sk_(?:live|test)_[a-zA-Z0-9]{12,}", "[REDACTED_STRIPE_KEY]"),
    (r"ghp_[a-zA-Z0-9]{30,}", "[REDACTED_GITHUB_TOKEN]"),
    (r"github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}", "[REDACTED_GITHUB_TOKEN]"),
    (r"glpat-[a-zA-Z0-9_-]{20,}", "[REDACTED_GITLAB_TOKEN]"),
    (r"xox[baprs]-[a-zA-Z0-9-]{20,}", "[REDACTED_SLACK_TOKEN]"),
    (r"(?:npm|pypi|hf)_[a-zA-Z0-9_-]{20,}", "[REDACTED_PACKAGE_TOKEN]"),
    (r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_ACCESS_KEY]"),
    (r"ASIA[0-9A-Z]{16}", "[REDACTED_AWS_ACCESS_KEY]"),
    (r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[a-zA-Z0-9/+=]{30,}['\"]?", "aws_secret_access_key='[REDACTED_AWS_SECRET]'"),
    (r"(?i)aws_session_token\s*[:=]\s*['\"]?[^\s'\"]{20,}['\"]?", "aws_session_token='[REDACTED_AWS_SESSION]'"),
    (r"(?i)authorization\s*:\s*bearer\s+[a-zA-Z0-9._~+/=-]{12,}", "Authorization: Bearer [REDACTED_BEARER_TOKEN]"),
    (r"(?i)bearer\s+[a-zA-Z0-9._~+/=-]{12,}", "Bearer [REDACTED_BEARER_TOKEN]"),
    
    # JWTs
    (r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}", "[REDACTED_JWT_TOKEN]"),
    
    # Private keys
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]"),
    
    # Common Env Vars with passwords/tokens
    (r"(?i)(password|passwd|secret|token|api[_-]?key|auth[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?([^\s'\"]{6,})['\"]?", r"\1=[REDACTED_SECRET]"),
    
    # Database connection strings with credentials
    (r"(?i)(postgres|mysql|mongodb|redis)://[^:]+:[^@]+@", r"\1://[REDACTED_USER]:[REDACTED_PASSWORD]@"),
]


def scrub_secrets(text: str) -> str:
    """Scrub secrets from text using deterministic regex rules."""
    sanitized = text
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = re.sub(pattern, replacement, sanitized)
    return sanitized


def has_secrets(text: str) -> bool:
    """Check if text contains any recognized secret patterns."""
    for pattern, _ in SECRET_PATTERNS:
        if re.search(pattern, text):
            return True
    return False
