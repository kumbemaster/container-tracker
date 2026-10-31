"""Try the agent assistant for a user without confirming anything."""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from tracking.ai.assistant import run_assistant


class Command(BaseCommand):
    help = 'Run the agent assistant for a user and print the result.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('text')

    def handle(self, *args, **options):
        user = User.objects.filter(username=options['username']).first()
        if user is None:
            self.stderr.write('ValueError: unknown user.')
            raise SystemExit(1)
        try:
            result = run_assistant(user, options['text'], [])
        except Exception as exc:
            self.stderr.write('%s: %s' % (type(exc).__name__, exc))
            raise SystemExit(1)
        kind = result.get('type')
        if kind == 'proposal':
            self.stdout.write('\n'.join(result.get('summary') or []))
        elif kind == 'question':
            self.stdout.write(result.get('question', ''))
        else:
            self.stdout.write(result.get('text', ''))
