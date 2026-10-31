"""Transcribe voice with Meta's Muse Voice Transcribe (ASR)."""

import json
import logging
import uuid
from time import monotonic, sleep
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from django.conf import settings

from .client import MissingAPIKeyError

logger = logging.getLogger(__name__)

MODEL = 'muse-voice-transcribe-1.0'
TIMEOUT = 30


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


def transcribe_audio(wav_bytes):
    """Transcribe WAV bytes and return the stripped transcript.

    Sends one request per the ASR reference (multipart "request" JSON
    plus "audio" part, Bearer key, per-request uuid4 session id).
    Retries a 500 once and a 429 at most twice with exponential
    backoff. Only status and duration are ever logged, never the key
    or the audio.
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
    payload = {
        'mode': getattr(settings, 'META_ASR_MODE', 'PUSH_TO_TALK'),
        'model': MODEL,
        'audioEncoding': 'WAV',
    }
    boundary = uuid.uuid4().hex
    body = _encode_multipart(boundary, json.dumps(payload), wav_bytes)
    headers = {
        'Authorization': 'Bearer ' + key,
        'Accept': 'application/json',
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
    }
    start = monotonic()
    retries_429 = 0
    retried_500 = False
    try:
        while True:
            try:
                status, raw = _send(url, body, headers)
            except (URLError, OSError, TimeoutError) as exc:
                raise TranscriptionError(
                    'Transcription failed.'
                ) from exc
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
