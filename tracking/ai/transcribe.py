"""Transcribe voice with Meta's Muse Voice Transcribe (ASR)."""

import json
import logging
import uuid
from time import monotonic, sleep
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from django.conf import settings

from ..models import Importer, Shipment
from .client import MissingAPIKeyError

logger = logging.getLogger(__name__)

MODEL = 'muse-voice-transcribe-1.0'
TIMEOUT = 30
MAX_KEYWORDS = 40
MAX_KEYWORD_LENGTH = 40

STATIC_KEYWORDS = [
    'duty paid',
    'PAAR',
    'PAAR assessed',
    'manifest',
    'manifest available',
    'arrived at port',
    'shipping line processed',
    'terminal processed',
    'customs processed',
    'customs examination',
    'bonded terminal',
    'gate out',
    'in transit',
    'delivered',
    'empty container returned',
    'container',
    'bill of lading',
]


def build_keywords(company):
    """Vocabulary hints for transcription: static stage words first,
    then this company's importer names, then its open BL numbers.
    Trimmed, de-duplicated, capped at MAX_KEYWORDS terms of at most
    MAX_KEYWORD_LENGTH characters. Never includes another company.
    """
    terms = list(STATIC_KEYWORDS)
    if company is not None:
        terms.extend(
            Importer.objects.filter(
                agent_company=company
            )
            .order_by('name')
            .values_list('name', flat=True)
        )
        terms.extend(
            Shipment.objects.filter(
                importer__agent_company=company, closed_at__isnull=True
            )
            .order_by('-created_at')
            .values_list('bl_number', flat=True)
        )
    cleaned = []
    seen = set()
    for term in terms:
        term = (term or '').strip()
        if (
            not term
            or len(term) > MAX_KEYWORD_LENGTH
            or term in seen
        ):
            continue
        seen.add(term)
        cleaned.append(term)
        if len(cleaned) >= MAX_KEYWORDS:
            break
    return cleaned


class TranscriptionError(Exception):
    """Transcription failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _encode_multipart(boundary, request_json, wav_bytes):
    part_request = (
        '--%s\r\n'
        'Content-Disposition: form-data; name="request"\r\n'
        'Content-Type: application/json\r\n'
        '\r\n'
        '%s\r\n' % (boundary, request_json)
    ).encode('utf-8')
    part_audio = (
        '--%s\r\n'
        'Content-Disposition: form-data; name="audio"; '
        'filename="audio.wav"\r\n'
        'Content-Type: audio/wav\r\n'
        '\r\n' % boundary
    ).encode('utf-8')
    return (
        part_request + part_audio + wav_bytes + ('\r\n--%s--\r\n' % boundary).encode('utf-8')
    )


def _send(url, body, headers):
    """POST the multipart body; return (status, response bytes)."""
    outgoing = urlrequest.Request(
        url, data=body, headers=headers, method='POST'
    )
    try:
        with urlrequest.urlopen(outgoing, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _request_payload(with_hints, keywords):
    payload = {
        'mode': getattr(settings, 'META_ASR_MODE', 'PUSH_TO_TALK'),
        'model': MODEL,
        'audioEncoding': 'WAV',
    }
    if with_hints:
        payload['keywords'] = keywords
        payload['languageBias'] = ['English']
    return payload


def transcribe_audio(wav_bytes, company=None):
    """Transcribe WAV bytes and return the stripped transcript.

    Sends one request per the ASR reference (multipart "request" JSON
    plus "audio" part, Bearer key, per-request uuid4 session id).
    When a company is given and hints are enabled, the request also
    carries vocabulary keywords and an English language bias. A 400
    answer to a request with hints is retried once without them.
    Retries a 500 once and a 429 at most twice with exponential
    backoff. Only status and duration are ever logged, never the key,
    the keywords or the audio.
    """
    key = getattr(settings, 'META_API_KEY', '') or ''
    if not key:
        raise MissingAPIKeyError(
            'MODEL_API_KEY is not set. Transcription is unavailable.'
        )
    session_id = str(uuid.uuid4())
    url = (
        settings.META_API_BASE_URL.rstrip('/')
        + '/asr/transcribe?sessionId='
        + session_id
    )
    keywords = []
    if company is not None and getattr(
        settings, 'META_ASR_HINTS_ENABLED', True
    ):
        keywords = build_keywords(company)
    with_hints = bool(keywords)
    boundary = uuid.uuid4().hex
    body = _encode_multipart(
        boundary,
        json.dumps(_request_payload(with_hints, keywords)),
        wav_bytes,
    )
    headers = {
        'Authorization': 'Bearer ' + key,
        'Accept': 'application/json',
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
    }
    start = monotonic()
    retries_429 = 0
    retried_500 = False
    retried_without_hints = False
    try:
        while True:
            try:
                status, raw = _send(url, body, headers)
            except (URLError, OSError, TimeoutError) as exc:
                raise TranscriptionError(
                    'Transcription failed.'
                ) from exc
            if (
                status == 400
                and with_hints
                and not retried_without_hints
            ):
                retried_without_hints = True
                with_hints = False
                body = _encode_multipart(
                    boundary,
                    json.dumps(_request_payload(False, [])),
                    wav_bytes,
                )
                continue
            if status == 429 and retries_429 < 2:
                retries_429 += 1
                sleep(2 ** retries_429)
                continue
            if status == 500 and not retried_500:
                retried_500 = True
                continue
            break
    except TranscriptionError:
        logger.warning(
            'asr status=network-error duration=%.1fs',
            monotonic() - start,
        )
        raise
    if status != 200:
        logger.warning(
            'asr status=%s duration=%.1fs', status, monotonic() - start
        )
        raise TranscriptionError(
            'Transcription failed.', status=status
        )
    try:
        transcript = (json.loads(raw or b'{}').get('transcript') or '')
        transcript = transcript.strip()
    except ValueError:
        transcript = ''
    if not transcript:
        logger.warning(
            'asr status=empty duration=%.1fs', monotonic() - start
        )
        raise TranscriptionError('Transcription came back empty.')
    logger.info('asr status=ok duration=%.1fs', monotonic() - start)
    return transcript
