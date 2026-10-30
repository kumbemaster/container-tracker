"""Client for Meta's OpenAI-compatible model API."""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class MetaAPIError(Exception):
    """A call to the Meta model API failed."""


class MissingAPIKeyError(MetaAPIError):
    """MODEL_API_KEY is not set, so no model call can be made."""


def get_client():
    """Build the OpenAI-compatible client for the Meta API.

    The key is never logged here or anywhere else in this app.
    """
    from openai import OpenAI

    key = getattr(settings, 'META_API_KEY', '') or ''
    if not key:
        raise MissingAPIKeyError(
            'MODEL_API_KEY is not set. Document scanning is unavailable.'
        )
    return OpenAI(
        base_url=settings.META_API_BASE_URL,
        api_key=key,
        timeout=60,
    )
