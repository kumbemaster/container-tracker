from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase

from . import access as access_helpers
from .models import (
    AgentCompany,
    Container,
    DeviceGrant,
    Importer,
    Shipment,
    StaffMember,
)


class AgentTests(TestCase):
    def _company(self, name):
        return AgentCompany.objects.create(name=name)

    def _agent(self, company, username, password='secret123'):
        user = User.objects.create_user(username=username, password=password)
        StaffMember.objects.create(
            user=user, agent_company=company, role=StaffMember.Role.OWNER
        )
        return user

    def _importer(self, company, name='Agent Importer', code='482910'):
        return Importer.objects.create(
            agent_company=company, name=name, access_code=code
        )

    def _shipment(self, importer, bl='AGBL001'):
        return Shipment.objects.create(
            importer=importer,
            bl_number=bl,
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )

    def test_login_required(self):
        for url in (
            '/agent/shipments/',
            '/agent/shipments/new/',
            '/agent/importers/',
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(
                response['Location'].startswith('/agent/login/')
            )

    def test_user_without_staff_record_refused(self):
        user = User.objects.create_user(username='plain', password='secret123')
        self.client.force_login(user)
        response = self.client.get('/agent/shipments/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

    def test_failed_login_shows_generic_message(self):
        company = self._company('Login Co')
        self._agent(company, 'login-agent')
        response = self.client.post(
            '/agent/login/', {'username': 'login-agent', 'password': 'wrong'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Incorrect username or password.')

    def test_lockout_after_five_failed_logins(self):
        company = self._company('Lockout Co')
        self._agent(company, 'locked-agent')
        for _ in range(5):
            self.client.post(
                '/agent/login/',
                {'username': 'locked-agent', 'password': 'wrong'},
            )
        response = self.client.post(
            '/agent/login/',
            {'username': 'locked-agent', 'password': 'wrong'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'Too many attempts. Please try again in 15 minutes.'
        )

    def test_other_company_data_gives_404(self):
        company_a = self._company('Company A')
        company_b = self._company('Company B')
        user_a = self._agent(company_a, 'agent-a')
        importer_b = self._importer(company_b, name='B Importer')
        shipment_b = self._shipment(importer_b, bl='BBL001')
        self.client.force_login(user_a)
        response = self.client.get(
            '/agent/shipments/%d/' % shipment_b.pk
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            '/agent/importers/%d/rotate/' % importer_b.pk
        )
        self.assertEqual(response.status_code, 404)

    def test_create_shipment_with_valid_containers(self):
        company = self._company('Create Co')
        user = self._agent(company, 'creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self.client.post(
            '/agent/shipments/new/',
            {
                'importer': str(importer.pk),
                'new_importer_name': '',
                'bl_number': 'NEWBL001',
                'shipping_line': 'New Line',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'discharge_date': '',
                'containers': 'CSQU3054383 40ft\nDEMO4416733 20ft',
                'shipping_line_free_days': '',
                'terminal_free_days': '',
                'return_window_days': '14',
            },
        )
        self.assertEqual(response.status_code, 302)
        shipment = Shipment.objects.get(bl_number='NEWBL001')
        numbers = sorted(
            shipment.containers.values_list('number', flat=True)
        )
        self.assertEqual(numbers, ['CSQU3054383', 'DEMO4416733'])
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'NEWBL001')

    def test_invalid_check_digit_creates_nothing(self):
        company = self._company('Invalid Co')
        user = self._agent(company, 'invalid-creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self.client.post(
            '/agent/shipments/new/',
            {
                'importer': str(importer.pk),
                'new_importer_name': '',
                'bl_number': 'BADBL001',
                'shipping_line': 'Line',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'discharge_date': '',
                'containers': 'CSQU3054384 40ft',
                'shipping_line_free_days': '',
                'terminal_free_days': '',
                'return_window_days': '14',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a valid container number')
        self.assertFalse(Shipment.objects.filter(bl_number='BADBL001').exists())

    def test_duplicate_container_number_creates_nothing(self):
        company = self._company('Duplicate Co')
        user = self._agent(company, 'dup-creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self.client.post(
            '/agent/shipments/new/',
            {
                'importer': str(importer.pk),
                'new_importer_name': '',
                'bl_number': 'DUPBL001',
                'shipping_line': 'Line',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'discharge_date': '',
                'containers': 'CSQU3054383 40ft\ncsqu3054383 20ft',
                'shipping_line_free_days': '',
                'terminal_free_days': '',
                'return_window_days': '14',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'duplicate container number')
        self.assertFalse(Shipment.objects.filter(bl_number='DUPBL001').exists())

    def test_new_importer_gets_six_digit_text_code(self):
        company = self._company('Code Co')
        user = self._agent(company, 'code-creator')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/shipments/new/',
            {
                'importer': '',
                'new_importer_name': 'Brand New Importer',
                'bl_number': 'CODEBL001',
                'shipping_line': 'Line',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'discharge_date': '',
                'containers': 'CSQU3054383 40ft',
                'shipping_line_free_days': '',
                'terminal_free_days': '',
                'return_window_days': '14',
            },
        )
        self.assertEqual(response.status_code, 302)
        importer = Importer.objects.get(name='Brand New Importer')
        self.assertRegex(importer.access_code, r'^\d{6}$')

    def test_rotate_code_changes_it_and_revokes_grants(self):
        company = self._company('Rotate Co')
        user = self._agent(company, 'rotator')
        importer = self._importer(company, code='111111')
        session, _ = access_helpers.create_device_session()
        access_helpers.grant_access(session, importer)
        self.client.force_login(user)
        response = self.client.post(
            '/agent/importers/%d/rotate/' % importer.pk
        )
        self.assertEqual(response.status_code, 302)
        importer.refresh_from_db()
        self.assertRegex(importer.access_code, r'^\d{6}$')
        self.assertNotEqual(importer.access_code, '111111')
        self.assertTrue(
            DeviceGrant.objects.filter(
                session=session, importer=importer, revoked=True
            ).exists()
        )
        self.assertFalse(access_helpers.has_access(session, importer))

    def test_copy_message_has_bl_and_code(self):
        company = self._company('Copy Co')
        user = self._agent(company, 'copier')
        importer = self._importer(company, code='654321')
        shipment = self._shipment(importer, bl='COPYBL001')
        self.client.force_login(user)
        response = self.client.get('/agent/shipments/%d/' % shipment.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'COPYBL001')
        self.assertContains(response, '654321')
        self.assertContains(response, 'Track your shipment')

    def test_no_csrf_exemptions(self):
        for name in ('views.py', 'agent_views.py'):
            source = (Path(__file__).parent / name).read_text()
            self.assertNotIn('csrf_exempt', source)
