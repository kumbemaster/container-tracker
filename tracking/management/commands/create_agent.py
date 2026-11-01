import getpass

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from tracking.models import AgentCompany, StaffMember


class Command(BaseCommand):
    help = 'Create an agent user with the owner role.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--company', required=True)

    def handle(self, *args, **options):
        username = options['username']
        if User.objects.filter(username=username).exists():
            raise CommandError('User already exists: %s' % username)
        password = getpass.getpass('Password: ')
        if not password:
            raise CommandError('Password must not be empty.')
        try:
            validate_password(password, user=User(username=username))
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages))
        company, _ = AgentCompany.objects.get_or_create(
            name=options['company']
        )
        user = User(username=username)
        user.set_password(password)
        user.save()
        StaffMember.objects.create(
            user=user,
            agent_company=company,
            role=StaffMember.Role.OWNER,
        )
        self.stdout.write(self.style.SUCCESS('Agent created: %s' % username))
