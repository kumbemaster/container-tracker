"""Read a shipping document image with Meta's model API."""

import base64
import logging
import time

from django.conf import settings

from .client import MetaAPIError, get_client
from .images import prepare_image
from .parsing import (
    ModelJSONError,
    normalise_extraction,
    parse_model_json,
)

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    'you read shipping documents (a bill of lading or similar); '
    'return ONLY a JSON object with the keys bl_number, shipping_line, '
    'port_of_discharge, place_of_delivery, importer_name (the consignee), '
    'goods_description (at most 15 words), containers (a list of objects '
    'with number and size) and uncertain (names of any fields you are not '
    'sure of); use null for anything you cannot read clearly; '
    'never guess a container number.'
)

RETRY_INSTRUCTION = (
    'That reply was not valid JSON. Return ONLY the JSON object, '
    'with no code fences and no other text.'
)


class ExtractionError(MetaAPIError):
    """The document could not be read."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _request(client, messages):
    """Send one chat completions request, logging only status/duration."""
    start = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=settings.META_MODEL,
            messages=messages,
        )
    except Exception as exc:
        status = getattr(exc, 'status_code', None)
        logger.warning(
            'bl extract failed status=%s duration=%.1fs',
            status,
            time.monotonic() - start,
        )
        raise ExtractionError(
            'The document could not be read.',
            status=status,
        ) from exc
    logger.info(
        'bl extract status=ok duration=%.1fs', time.monotonic() - start
    )
    return response.choices[0].message.content or ''


def extract_shipment(image_bytes):
    """Read a document image and return the normalised shipment dict.

    The image is sent as a base64 JPEG data URL. If the first reply
    does not parse as JSON, the model is asked once more for only valid
    JSON; otherwise ExtractionError is raised. Only request status and
    duration are ever logged, never the key, the image or the reply.
    """
    jpeg = prepare_image(image_bytes)
    data_url = 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode(
        'ascii'
    )
    client = get_client()
    messages = [
        {'role': 'system', 'content': SYSTEM_INSTRUCTION},
        {
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': 'Read this shipping document.',
                },
                {
                    'type': 'image_url',
                    'image_url': {'url': data_url},
                },
            ],
        },
    ]
    text = _request(client, messages)
    try:
        data = parse_model_json(text)
    except ModelJSONError:
        messages.append({'role': 'assistant', 'content': text})
        messages.append({'role': 'user', 'content': RETRY_INSTRUCTION})
        try:
            data = parse_model_json(_request(client, messages))
        except ModelJSONError as exc:
            raise ExtractionError(
                'The document could not be read: '
                'the model reply was not valid JSON.'
            ) from exc
    return normalise_extraction(data)
