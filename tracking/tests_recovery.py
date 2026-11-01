"""Password recovery, account page and security-review tests."""

from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import agent_views
from .agent_views import normalise_answer
from .models import AccessAttempt, AgentCompany, SecurityProfile, StaffMember

NEWPW = 'NewStrongPass456!'


def _company(name):
    return AgentCompany.objects.create(name=name)


def _agent(company, username, password='StrongPass123!'):
    user = User.objects.create_user(username=username, password=password)
    StaffMember.objects.create(
        user=user, agent_company=company, role=StaffMember.Role.OWNER
    )
    return user


def _profile(user, answer='My First School Name'):
    return SecurityProfile.objects.create(
        user=user,
        question=agent_views.SECURITY_QUESTIONS[0],
        answer_hash=make_password(normalise_answer(answer)),
    )


def _reset_flow(client, username, answer, password=NEWPW):
    client.post('/agent/forgot/', {'username': username})
    return client.post(
        '/agent/forgot/answer/',
        {'answer': answer, 'new_password': password, 'confirm': password},
    )


class NormaliseAnswerTests(TestCase):
    def test_normalises(self):
        self.assertEqual(
            normalise_answer('  My   First SCHOOL-NAME!! '),
            'my first schoolname',
        )
        self.assertEqual(normalise_answer('A  B\tC\nD'), 'a b c d')


class SignupSecurityTests(TestCase):
    def test_signup_stores_hashed_normalised_answer(self):
        from .tests_signup import _signup_data

        response = self.client.post(
            '/agent/signup/',
            _signup_data(answer='  My   First SCHOOL-Name!! '),
        )
        self.assertEqual(response.status_code, 302)
        profile = SecurityProfile.objects.get(user__username='newuser')
        self.assertNotIn('SCHOOL', profile.answer_hash)
        self.assertNotIn('school-name', profile.answer_hash)
        self.assertTrue(
            check_password('my first schoolname', profile.answer_hash)
        )

    def test_signup_requires_question_and_answer(self):
        from .tests_signup import _signup_data

        for data in (
            _signup_data(question='', answer='Valid Answer Here'),
            _signup_data(question='0', answer='abc'),
            _signup_data(
                question='own', custom_question='short', answer='Valid One'
            ),
        ):
            response = self.client.post('/agent/signup/', data)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(
                User.objects.filter(username=data['username']).exists()
            )

    def test_signup_custom_question(self):
        from .tests_signup import _signup_data

        response = self.client.post(
            '/agent/signup/',
            _signup_data(
                question='own',
                custom_question='What is my favourite colour scheme?',
                answer='Blue And Green',
            ),
        )
        self.assertEqual(response.status_code, 302)
        profile = SecurityProfile.objects.get(user__username='newuser')
        self.assertEqual(
            profile.question, 'What is my favourite colour scheme?'
        )

    def test_signup_never_echoes_password_or_answer(self):
        from .tests_signup import _signup_data

        response = self.client.post(
            '/agent/signup/',
            _signup_data(password='short', answer='Secret Answer XYZ'),
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('Secret Answer XYZ', html)
        self.assertNotIn('value="short"', html)

    def test_signup_user_is_not_staff(self):
        from .tests_signup import _signup_data

        self.client.post('/agent/signup/', _signup_data())
        user = User.objects.get(username='newuser')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        response = self.client.get('/admin/')
        self.assertNotEqual(response.status_code, 200)

    def test_admin_hides_answer_hash(self):
        from django.contrib import admin

        model_admin = admin.site._registry[SecurityProfile]
        self.assertNotIn('answer_hash', model_admin.fields)
        self.assertNotIn('answer_hash', model_admin.list_display)


class ForgotFlowTests(TestCase):
    def test_reset_with_right_answer(self):
        company = _company('Reset Co')
        user = _agent(company, 'reset-user')
        _profile(user)
        response = self.client.post(
            '/agent/forgot/', {'username': 'reset-user'}
        )
        self.assertRedirects(response, '/agent/forgot/answer/')
        page = self.client.get('/agent/forgot/answer/')
        self.assertContains(
            page, agent_views.SECURITY_QUESTIONS[0]
        )
        response = _reset_flow(
            self.client, 'reset-user', 'My First School Name'
        )
        self.assertRedirects(response, '/agent/login/?reset=1')
        page = self.client.get('/agent/login/?reset=1')
        self.assertContains(page, 'Password changed. Please log in.')
        self.client.logout()
        self.assertTrue(
            self.client.login(username='reset-user', password=NEWPW)
        )

    def test_wrong_answer_gives_generic_message(self):
        company = _company('Wrong Co')
        user = _agent(company, 'wrong-user')
        _profile(user)
        response = _reset_flow(self.client, 'wrong-user', 'No Such School')
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, agent_views.RESET_GENERIC_MESSAGE
        )
        user.refresh_from_db()
        self.assertTrue(user.check_password('StrongPass123!'))

    def test_unknown_user_gets_stable_fake_question(self):
        self.client.post('/agent/forgot/', {'username': 'ghost-user'})
        first = self.client.get('/agent/forgot/answer/')
        second = self.client.get('/agent/forgot/answer/')
        fake = agent_views._reset_fake_question('ghost-user')
        self.assertEqual(
            agent_views._reset_fake_question('ghost-user'), fake
        )
        self.assertContains(first, fake)
        self.assertContains(second, fake)
        response = self.client.post(
            '/agent/forgot/answer/',
            {
                'answer': 'anything here',
                'new_password': NEWPW,
                'confirm': NEWPW,
            },
        )
        self.assertContains(response, agent_views.RESET_GENERIC_MESSAGE)
        self.assertFalse(User.objects.filter(username='ghost-user').exists())

    def test_user_without_profile_gets_same_message(self):
        company = _company('Noprof Co')
        _agent(company, 'noprof-user')
        response = _reset_flow(self.client, 'noprof-user', 'Any Answer')
        self.assertContains(response, agent_views.RESET_GENERIC_MESSAGE)
        user = User.objects.get(username='noprof-user')
        self.assertTrue(user.check_password('StrongPass123!'))

    def test_username_rate_limit(self):
        company = _company('Rate Co')
        user = _agent(company, 'rate-user')
        _profile(user)
        for _ in range(5):
            _reset_flow(self.client, 'rate-user', 'wrong answer here')
        response = _reset_flow(self.client, 'rate-user', 'wrong answer')
        self.assertContains(
            response, agent_views.RESET_LOCKED_MESSAGE
        )

    def test_step1_rate_limit(self):
        for number in range(20):
            self.client.post(
                '/agent/forgot/', {'username': 'user%d' % number}
            )
        response = self.client.post(
            '/agent/forgot/', {'username': 'user20'}
        )
        self.assertContains(response, agent_views.RESET_LOCKED_MESSAGE)

    def test_success_clears_username_counters(self):
        company = _company('Clear Co')
        user = _agent(company, 'clear-user')
        _profile(user)
        for _ in range(3):
            _reset_flow(self.client, 'clear-user', 'wrong answer here')
        _reset_flow(self.client, 'clear-user', 'My First School Name')
        self.assertFalse(
            AccessAttempt.objects.filter(
                key='forgot-user:clear-user'
            ).exists()
        )

    def test_reset_ends_other_sessions(self):
        company = _company('Session Co')
        user = _agent(company, 'session-user')
        _profile(user)
        self.client.force_login(user)
        from django.test import Client

        other = Client()
        _reset_flow(other, 'session-user', 'My First School Name')
        response = self.client.get('/agent/shipments/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

    def test_reset_enforces_password_validators(self):
        company = _company('Valid Co')
        user = _agent(company, 'valid-user')
        _profile(user)
        response = _reset_flow(
            self.client, 'valid-user', 'My First School Name',
            password='short',
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, agent_views.RESET_GENERIC_MESSAGE
        )
        user.refresh_from_db()
        self.assertTrue(user.check_password('StrongPass123!'))

    def test_reset_session_expires(self):
        company = _company('Expire Co')
        user = _agent(company, 'expire-user')
        _profile(user)
        self.client.post('/agent/forgot/', {'username': 'expire-user'})
        session = self.client.session
        session['reset_started'] = (
            timezone.now() - timedelta(minutes=11)
        ).isoformat()
        session.save()
        response = self.client.post(
            '/agent/forgot/answer/',
            {
                'answer': 'My First School Name',
                'new_password': NEWPW,
                'confirm': NEWPW,
            },
        )
        self.assertRedirects(response, '/agent/forgot/')
        user.refresh_from_db()
        self.assertTrue(user.check_password('StrongPass123!'))

    def test_answer_step_needs_step_one(self):
        response = self.client.get('/agent/forgot/answer/')
        self.assertRedirects(response, '/agent/forgot/')

    def test_no_secret_in_reset_responses(self):
        company = _company('Leak Co')
        user = _agent(company, 'leak-user')
        _profile(user, answer='Leaky Answer Phrase')
        response = _reset_flow(
            self.client, 'leak-user', 'Leaky wrong guess',
            password='LeakyNewPass999!',
        )
        html = response.content.decode()
        self.assertNotIn('Leaky', html)
        self.assertNotIn('482910', html)


class AccountPageTests(TestCase):
    def test_login_required(self):
        response = self.client.get('/agent/account/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

    def test_change_password_keeps_session(self):
        company = _company('Acct Co')
        user = _agent(company, 'acct-user')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/account/',
            {
                'form': 'password',
                'current_password': 'StrongPass123!',
                'new_password': NEWPW,
                'confirm': NEWPW,
            },
        )
        self.assertContains(response, 'Password changed.')
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEWPW))
        page = self.client.get('/agent/shipments/')
        self.assertEqual(page.status_code, 200)

    def test_change_password_needs_current(self):
        company = _company('Acct2 Co')
        user = _agent(company, 'acct2-user')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/account/',
            {
                'form': 'password',
                'current_password': 'wrong-current',
                'new_password': NEWPW,
                'confirm': NEWPW,
            },
        )
        self.assertContains(response, 'current password is not correct')
        user.refresh_from_db()
        self.assertTrue(user.check_password('StrongPass123!'))

    def test_set_question_needs_password(self):
        company = _company('Acct3 Co')
        user = _agent(company, 'acct3-user')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/account/',
            {
                'form': 'question',
                'current_password': 'wrong-current',
                'question': '1',
                'custom_question': '',
                'answer': 'A Valid Answer Here',
            },
        )
        self.assertContains(response, 'current password is not correct')
        self.assertFalse(
            SecurityProfile.objects.filter(user=user).exists()
        )

    def test_set_question(self):
        company = _company('Acct4 Co')
        user = _agent(company, 'acct4-user')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/account/',
            {
                'form': 'question',
                'current_password': 'StrongPass123!',
                'question': '1',
                'custom_question': '',
                'answer': 'A Valid Answer Here',
            },
        )
        self.assertContains(response, 'Security question saved.')
        profile = SecurityProfile.objects.get(user=user)
        self.assertEqual(
            profile.question, agent_views.SECURITY_QUESTIONS[1]
        )

    def test_notice_only_without_profile(self):
        company = _company('Notice Co')
        user = _agent(company, 'notice-user')
        self.client.force_login(user)
        page = self.client.get('/agent/shipments/')
        self.assertContains(
            page, 'Set a <a href="/agent/account/">security question</a>'
        )
        _profile(user)
        page = self.client.get('/agent/shipments/')
        self.assertNotContains(page, 'recover your account')


class ReviewFixTests(TestCase):
    def test_target_key_cannot_dodge_with_spaces(self):
        for query in ('csqu 3054-383', 'CSQU3054383', 'csqu-3054383'):
            self.client.post(
                '/track/verify/', {'q': query, 'code': '000000'}
            )
        self.assertEqual(
            AccessAttempt.objects.filter(key='target:CSQU3054383').count(),
            3,
        )

    def test_answer_ip_rate_limit(self):
        for number in range(10):
            self.client.post('/agent/forgot/', {'username': 'ghost%d' % number})
            self.client.post(
                '/agent/forgot/answer/',
                {
                    'answer': 'some wrong answer here',
                    'new_password': NEWPW,
                    'confirm': NEWPW,
                },
            )
        self.client.post('/agent/forgot/', {'username': 'ghost10'})
        response = self.client.post(
            '/agent/forgot/answer/',
            {
                'answer': 'some wrong answer here',
                'new_password': NEWPW,
                'confirm': NEWPW,
            },
        )
        self.assertContains(
            response, agent_views.RESET_LOCKED_MESSAGE
        )

    def test_login_limit_ignores_username_case(self):
        company = _company('Case Co')
        _agent(company, 'case-user')
        for _ in range(5):
            self.client.post(
                '/agent/login/',
                {'username': 'Case-User', 'password': 'wrong'},
            )
        response = self.client.post(
            '/agent/login/',
            {'username': 'CASE-USER', 'password': 'wrong'},
        )
        self.assertContains(
            response, 'Too many attempts. Please try again in 15 minutes.'
        )

    def test_login_next_is_validated(self):
        company = _company('Next Co')
        _agent(company, 'next-user')
        response = self.client.post(
            '/agent/login/?next=https://evil.example/',
            {'username': 'next-user', 'password': 'StrongPass123!'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('evil.example', response['Location'])

    def test_download_is_attachment_with_nosniff(self):
        from django.core.files.base import ContentFile

        from .models import Document

        company = _company('Dl Co')
        user = _agent(company, 'dl-user')
        document = Document.objects.create(
            company=company, uploaded_by=user, kind=Document.Kind.BL
        )
        document.file.save('scan-1.jpg', ContentFile(b'fakepngdata'))
        self.client.force_login(user)
        response = self.client.get('/agent/documents/%d/' % document.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertEqual(
            response['X-Content-Type-Options'], 'nosniff'
        )

    def test_huge_image_rejected(self):
        from PIL import Image

        from .ai.images import MAX_PIXELS, ImageError, prepare_image

        self.assertEqual(MAX_PIXELS, 40_000_000)
        pixels = Image.new('RGB', (6500, 6500), 'white')
        from io import BytesIO

        buffer = BytesIO()
        pixels.save(buffer, format='PNG')
        with self.assertRaises(ImageError):
            prepare_image(buffer.getvalue())

    def test_no_innerhtml_in_assistant(self):
        self.client.force_login(
            _agent(_company('Inner Co'), 'inner-user')
        )
        response = self.client.get('/agent/shipments/')
        self.assertNotContains(response, 'innerHTML')
        self.assertContains(response, 'createTextNode')

    def test_security_headers(self):
        response = self.client.get('/')
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(
            response['X-Content-Type-Options'], 'nosniff'
        )
        self.assertEqual(
            response['Referrer-Policy'], 'same-origin'
        )

    def test_cookie_samesite_defaults(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, 'Lax')
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, 'Lax')

    def test_create_agent_validates_password(self):
        from unittest.mock import patch

        from django.core.management.base import CommandError

        with patch(
            'getpass.getpass', return_value='short'
        ), self.assertRaises(CommandError):
            call_command(
                'create_agent', username='cmd-user', company='Cmd Co'
            )
        self.assertFalse(User.objects.filter(username='cmd-user').exists())

    @override_settings(DEBUG=False)
    def test_signup_never_joins_existing_company(self):
        from .tests_signup import _signup_data

        AgentCompany.objects.create(name='Existing Co')
        response = self.client.post(
            '/agent/signup/',
            _signup_data(company='Existing Co', username='joiner'),
        )
        self.assertContains(
            response, 'already registered'
        )
        user = User.objects.filter(username='joiner').first()
        self.assertIsNone(user)
