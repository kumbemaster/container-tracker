import secrets

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from tracking.demo_data import seed_demo_data
from tracking.models import AgentCompany, StaffMember


class Command(BaseCommand):
    help = (
        'Set up a fresh clone: migrate, load stage explanations, '
        'create demo data and a demo agent user.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Set a new random password for demo_agent.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run even when DEBUG is false.',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['force']:
            raise CommandError(
                'bootstrap_demo refuses to run when DEBUG is false. '
                'Use --force to override.'
            )
        call_command('migrate')
        call_command('load_stage_explanations')
        data = seed_demo_data()

        user, created = User.objects.get_or_create(username='demo_agent')
        company = AgentCompany.objects.get(
            name=data['company'].name
        )
        StaffMember.objects.get_or_create(
            user=user,
            defaults={
                'agent_company': company,
                'role': StaffMember.Role.OWNER,
            },
        )
        password = None
        if created or options['reset_password']:
            password = secrets.token_urlsafe(12)
            user.set_password(password)
            user.save()
            if created:
                self.stdout.write(self.style.SUCCESS(
                    'Created user demo_agent.'
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    'Password reset for demo_agent.'
                ))
        else:
            self.stdout.write(
                'demo_agent already exists; password unchanged. '
                'Use --reset-password to set a new one.'
            )

        self.stdout.write('Agent login: /agent/login/ (username: demo_agent)')
        if password is not None:
            self.stdout.write('Password: %s' % password)
        importer_c = data['importer_c']
        importer_c.refresh_from_db()
        self.stdout.write(
            'Demo Importer A code %s; search DEMOBL001, CSQU3054383 '
            'or DEMO4416733.' % data['importer_a'].access_code
        )
        self.stdout.write(
            'Demo Importer C Ltd code %s; search DEMOBL002, '
            'TSTU1234568 or TSTU6543214.' % importer_c.access_code
        )
