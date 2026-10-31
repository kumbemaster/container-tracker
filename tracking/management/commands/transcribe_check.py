"""Transcribe a local WAV file without storing anything."""

from django.core.management.base import BaseCommand

from tracking.ai.transcribe import transcribe_audio


class Command(BaseCommand):
    help = 'Transcribe a local WAV file and print the transcript.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to a local WAV file.')

    def handle(self, *args, **options):
        try:
            with open(options['path'], 'rb') as handle:
                wav_bytes = handle.read()
            transcript = transcribe_audio(wav_bytes)
        except Exception as exc:
            self.stderr.write('%s: %s' % (type(exc).__name__, exc))
            raise SystemExit(1)
        self.stdout.write(transcript)
