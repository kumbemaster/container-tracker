"""Test the BL scan pipeline against a local image file."""

import json

from django.core.management.base import BaseCommand

from tracking.ai.extract_bl import extract_shipment


class Command(BaseCommand):
    help = 'Scan a local JPEG or PNG with the BL extractor and print JSON.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to a local JPEG or PNG file.')

    def handle(self, *args, **options):
        try:
            with open(options['path'], 'rb') as handle:
                image_bytes = handle.read()
            result = extract_shipment(image_bytes)
        except Exception as exc:
            self.stderr.write('%s: %s' % (type(exc).__name__, exc))
            status = getattr(exc, 'status', None) or getattr(
                exc, 'status_code', None
            )
            if status is not None:
                self.stderr.write('status: %s' % status)
            raise SystemExit(1)
        self.stdout.write(json.dumps(result, indent=2))
