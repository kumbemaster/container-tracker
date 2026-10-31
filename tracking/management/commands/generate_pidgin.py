"""Draft Nigerian Pidgin stage explanations with an open-weight model.

Reads tracking/content/stage_explanations_en.json and writes only
tracking/content/stage_explanations_pcm.draft.json. The final
stage_explanations_pcm.json is never written by this command; a person
reviews the draft first.
"""

import json
import logging
from time import monotonic

import openai
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tracking.ai.client import MissingAPIKeyError, get_client

from .load_stage_explanations import CONTENT_DIR

logger = logging.getLogger(__name__)

BASETEN_BASE_URL = 'https://inference.baseten.co/v1'
DRAFT_NAME = 'stage_explanations_pcm.draft.json'
REQUEST_TIMEOUT = 30

SYSTEM_INSTRUCTION = (
    'You write clear, friendly Nigerian Pidgin (Naija Pidgin) for '
    'importers in Nigeria who are tracking their cargo. Rewrite the '
    'English text in Pidgin. Keep every fact and add nothing. Keep '
    'technical terms in English: PAAR, DO, EIR, BL, gate pass, manifest, '
    'customs, terminal, shipping line, bonded terminal, container, duty. '
    'Use short natural sentences, at most two sentences, respectful and '
    'easy to read, no emojis, no quotation marks. Return only the Pidgin '
    'text. Examples. English: This is when the container arrives at your '
    'address. Pidgin: Na wen the container reach your address. English: '
    'Customs finishes its processing and releases your cargo. Pidgin: '
    'Customs don finish dem work and dem don release your cargo.'
)


def _resolve_provider(explicit):
    if explicit in ('baseten', 'meta'):
        return explicit
    setting = (getattr(settings, 'STAGE_TEXT_PROVIDER', '') or '').strip()
    if setting in ('baseten', 'meta'):
        return setting
    if getattr(settings, 'BASETEN_API_KEY', ''):
        return 'baseten'
    return 'meta'


def _complete_with_retry(create, text):
    """Call a chat-completions create function once, retrying once."""
    messages = [
        {'role': 'system', 'content': SYSTEM_INSTRUCTION},
        {'role': 'user', 'content': text},
    ]
    try:
        return _content(create(messages))
    except Exception:
        return _content(create(messages))


def _content(response):
    text = (
        getattr(
            getattr(response.choices[0], 'message', None), 'content', ''
        )
        or ''
    ).strip()
    if not text:
        raise ValueError('Empty Pidgin reply.')
    return text


class Command(BaseCommand):
    help = 'Draft Pidgin stage explanations (writes only the draft file).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--provider', choices=('baseten', 'meta'), default=None
        )
        parser.add_argument('--only', default=None)

    def handle(self, *args, **options):
        provider = _resolve_provider(options['provider'])
        try:
            english = json.loads(
                (CONTENT_DIR / 'stage_explanations_en.json').read_text(
                    encoding='utf-8'
                )
            )
        except (OSError, ValueError) as exc:
            raise CommandError('Cannot read English file: %s.' % exc)
        only = options['only']
        if only is not None and only not in english:
            raise CommandError('Unknown stage key: %s.' % only)
        keys = [only] if only is not None else sorted(english)
        try:
            if provider == 'baseten':
                results = self._run_baseten(
                    {key: english[key] for key in keys}
                )
            else:
                results = self._run_meta(
                    {key: english[key] for key in keys}
                )
        except MissingAPIKeyError as exc:
            raise CommandError('%s' % exc)
        except Exception as exc:
            raise CommandError(
                '%s failed: %s.' % (provider, exc)
            )
        draft = CONTENT_DIR / DRAFT_NAME
        draft.write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        for key in keys:
            self.stdout.write('%s: ok' % key)
        self.stdout.write(
            '%d of %d written to %s' % (len(results), len(keys), draft.name)
        )

    def _run_baseten(self, texts):
        key = getattr(settings, 'BASETEN_API_KEY', '') or ''
        if not key:
            raise MissingAPIKeyError(
                'BASETEN_API_KEY is not set. Pidgin draft is unavailable.'
            )
        client = openai.OpenAI(
            base_url=BASETEN_BASE_URL,
            api_key=key,
            timeout=REQUEST_TIMEOUT,
        )
        model = settings.BASETEN_MODEL

        def create(messages):
            return client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                timeout=REQUEST_TIMEOUT,
            )

        return {
            key: self._generate(create, key, text)
            for key, text in texts.items()
        }

    def _run_meta(self, texts):
        client = get_client()
        model = settings.META_MODEL

        def create(messages):
            return client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                timeout=REQUEST_TIMEOUT,
            )

        return {
            key: self._generate(create, key, text)
            for key, text in texts.items()
        }

    def _generate(self, create, key, text):
        start = monotonic()
        try:
            result = _complete_with_retry(create, text)
        except Exception as exc:
            logger.warning(
                'pidgin status=failed duration=%.1fs',
                monotonic() - start,
            )
            raise CommandError(
                'Could not draft %s: %s.' % (key, exc)
            )
        logger.info(
            'pidgin status=ok duration=%.1fs', monotonic() - start
        )
        return result
