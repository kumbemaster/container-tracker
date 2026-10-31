"""Load stage explanations from tracking/content into the database."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tracking.models import Stage, StageExplanation

CONTENT_DIR = Path(__file__).resolve().parent.parent.parent / 'content'
MAX_TEXT_LENGTH = 300


def load_file(path, lang):
    """Validate one JSON file and store its texts. Returns the count."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CommandError('Cannot read %s: %s.' % (path.name, exc))
    if not isinstance(data, dict):
        raise CommandError(
            'Refusing %s: top level must be an object.' % path.name
        )
    stage_keys = set(
        Stage.objects.values_list('key', flat=True)
    )
    if set(data) != stage_keys:
        raise CommandError(
            'Refusing %s: keys do not match the Stage keys.' % path.name
        )
    count = 0
    for key in sorted(data):
        text = data[key]
        if not isinstance(text, str):
            raise CommandError(
                'Refusing %s: text for %s is not a string.'
                % (path.name, key)
            )
        text = text.strip()
        if len(text) > MAX_TEXT_LENGTH:
            raise CommandError(
                'Refusing %s: text for %s is over %d characters.'
                % (path.name, key, MAX_TEXT_LENGTH)
            )
        StageExplanation.objects.update_or_create(
            stage=Stage.objects.get(key=key),
            lang=lang,
            defaults={'text': text},
        )
        count += 1
    return count


class Command(BaseCommand):
    help = 'Load English (and Pidgin, if present) stage explanations.'

    def handle(self, *args, **options):
        english = CONTENT_DIR / 'stage_explanations_en.json'
        total = load_file(english, StageExplanation.Lang.EN)
        self.stdout.write('en: %d loaded' % total)
        pidgin = CONTENT_DIR / 'stage_explanations_pcm.json'
        if pidgin.exists():
            total = load_file(pidgin, StageExplanation.Lang.PCM)
            self.stdout.write('pcm: %d loaded' % total)
