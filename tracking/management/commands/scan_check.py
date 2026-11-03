"""Test the BL scan pipeline against a local image or PDF file."""

import json

from django.core.management.base import BaseCommand

from tracking.ai.extract_bl import extract_shipment
from tracking.ai.pdfs import PROTECTED_PDF_MESSAGE, PdfError


class Command(BaseCommand):
    help = 'Scan a local JPEG, PNG or PDF with the BL extractor and print JSON.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to a local JPEG, PNG or PDF file.')

    def handle(self, *args, **options):
        try:
            with open(options['path'], 'rb') as handle:
                file_bytes = handle.read()
            result = extract_shipment(file_bytes)
        except PdfError as exc:
            if str(exc) == PROTECTED_PDF_MESSAGE:
                self.stderr.write('%s: %s' % (type(exc).__name__, exc))
            else:
                self.stderr.write(
                    'We could not read this document. '
                    'You can enter the shipment manually.'
                )
            raise SystemExit(1)
        except Exception as exc:
            self.stderr.write('%s: %s' % (type(exc).__name__, exc))
            status = getattr(exc, 'status', None) or getattr(
                exc, 'status_code', None
            )
            if status is not None:
                self.stderr.write('status: %s' % status)
            raise SystemExit(1)
        self.stdout.write(json.dumps(result, indent=2))
