import json

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .ai.assistant_tools import find_shipments, get_shipment
from .models import (
    AccessAttempt,
    AgentCompany,
    AuditLog,
    Container,
    Importer,
    ProposedAction,
    Shipment,
    StaffMember,
    StageEvent,
)
from .views import WRONG_DETAILS_MESSAGE


def _agent(company, username):
    user = User.objects.create_user(username=username, password='secret123')
    StaffMember.objects.create(
        user=user, agent_company=company, role=StaffMember.Role.OWNER
    )
    return user


def _shipment(company, bl='DELBL001', importer_name='Del Importer'):
    importer = Importer.objects.create(
        agent_company=company, name=importer_name, access_code='123456'
    )
    shipment = Shipment.objects.create(
        importer=importer,
        bl_number=bl,
        shipping_line='Line',
        port='Onne',
        destination_area='Port Harcourt',
    )
    Container.objects.create(
        shipment=shipment, number='CSQU3054383', size='40ft'
    )
    return shipment


def _signup_data(**overrides):
    data = {
        'company': 'New Company Ltd',
        'username': 'newuser',
        'password': 'StrongPass123!',
        'confirm': 'StrongPass123!',
        'question': '0',
        'custom_question': '',
        'answer': 'My First School Name',
    }
    data.update(overrides)
    return data


class SignupTests(TestCase):
    def test_signup_creates_owner_and_logs_in(self):
        response = self.client.post('/agent/signup/', _signup_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/agent/shipments/')
        user = User.objects.get(username='newuser')
        company = AgentCompany.objects.get(name='New Company Ltd')
        self.assertEqual(user.staffmember.agent_company, company)
        self.assertEqual(user.staffmember.role, 'owner')
        self.assertEqual(
            int(self.client.session['_auth_user_id']), user.pk
        )
        page = self.client.get('/agent/shipments/')
        self.assertContains(page, 'Welcome')
        self.assertContains(page, 'No shipments yet.')
        self.assertContains(page, 'New shipment')
        self.assertContains(page, 'Scan a BL')

    def test_taken_company_name_refused(self):
        company = AgentCompany.objects.create(name='Taken Co')
        response = self.client.post(
            '/agent/signup/',
            _signup_data(company='  taken co  ', username='otheruser'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'That company name is already registered.'
        )
        self.assertFalse(User.objects.filter(username='otheruser').exists())
        self.assertEqual(company.staffmember_set.count(), 0)

    def test_taken_username_refused(self):
        company = AgentCompany.objects.create(name='User Co')
        _agent(company, 'takenuser')
        before = AgentCompany.objects.count()
        response = self.client.post(
            '/agent/signup/', _signup_data(username='TakenUser')
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'That username is taken.')
        self.assertEqual(AgentCompany.objects.count(), before)

    def test_weak_password_refused(self):
        response = self.client.post(
            '/agent/signup/',
            _signup_data(password='short', confirm='short'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'too short')
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_rate_limit(self):
        for _ in range(10):
            response = self.client.post(
                '/agent/signup/',
                _signup_data(password='short', confirm='short'),
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            '/agent/signup/',
            _signup_data(password='short', confirm='short'),
        )
        self.assertContains(response, 'Too many sign-up attempts')
        self.assertTrue(
            AccessAttempt.objects.filter(key__startswith='signup-ip:').exists()
        )

    def test_setting_disables_page_and_link(self):
        with override_settings(ALLOW_AGENT_SIGNUP=False):
            self.assertEqual(
                self.client.get('/agent/signup/').status_code, 404
            )
            self.assertEqual(
                self.client.post(
                    '/agent/signup/', _signup_data()
                ).status_code,
                404,
            )
            login_page = self.client.get('/agent/login/')
            self.assertNotContains(login_page, 'Create an account')
        login_page = self.client.get('/agent/login/')
        self.assertContains(login_page, 'Create an account')
        signup_page = self.client.get('/agent/signup/')
        self.assertContains(signup_page, 'Already have an account? Log in')


class AuthLinksTests(TestCase):
    def test_login_and_signup_have_back_link(self):
        for url in ('/agent/login/', '/agent/signup/'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Back to tracking')
            self.assertContains(response, 'href="/"')

    def test_setting_disables_signup_back_link_stays(self):
        with override_settings(ALLOW_AGENT_SIGNUP=False):
            response = self.client.get('/agent/login/')
            self.assertContains(response, 'Back to tracking')


class ShipmentDeleteTests(TestCase):
    def setUp(self):
        self.company = AgentCompany.objects.create(name='Del Co')
        self.user = _agent(self.company, 'del-user')
        self.client.force_login(self.user)
        self.shipment = _shipment(self.company)

    def _delete(self):
        self.client.post(
            '/agent/shipments/%d/delete/' % self.shipment.pk
        )
        self.shipment.refresh_from_db()

    def test_confirm_page_then_delete(self):
        response = self.client.get(
            '/agent/shipments/%d/delete/' % self.shipment.pk
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'DELBL001')
        self.assertContains(response, 'Del Importer')
        self.assertContains(response, 'CSQU3054383')
        self.assertContains(
            response, 'You can restore it later from Deleted shipments.'
        )
        self.assertContains(response, 'Delete shipment')
        self.assertContains(response, 'Cancel')
        response = self.client.post(
            '/agent/shipments/%d/delete/' % self.shipment.pk,
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Shipment DELBL001 deleted.')
        self.assertEqual(
            response.redirect_chain, [('/agent/shipments/', 302)]
        )
        self.shipment.refresh_from_db()
        self.assertIsNotNone(self.shipment.deleted_at)
        self.assertTrue(
            AuditLog.objects.filter(
                shipment=self.shipment, action='shipment_deleted'
            ).exists()
        )
        self.assertEqual(self.shipment.containers.count(), 1)

    def test_deleted_disappears_from_agent_pages(self):
        self._delete()
        response = self.client.get('/agent/shipments/')
        self.assertNotContains(response, '<td class="mono">DELBL001</td>')
        self.assertNotContains(response, 'agent_shipment_detail')
        self.assertEqual(
            self.client.get(
                '/agent/shipments/%d/' % self.shipment.pk
            ).status_code,
            404,
        )
        response = self.client.get('/agent/importers/')
        self.assertContains(response, '0 shipments')

    def test_deleted_invisible_to_importers(self):
        verify = self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '123456'}
        )
        self.assertEqual(verify.status_code, 302)
        location = verify['Location']
        self._delete()
        response = self.client.get(location)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')
        response = self.client.post(
            '/track/', {'q': 'CSQU3054383'}
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '123456'}
        )
        self.assertContains(response, WRONG_DETAILS_MESSAGE)

    def test_deleted_hidden_from_assistant_tools(self):
        self._delete()
        result = find_shipments(self.company, 'DELBL001')
        self.assertEqual(result['shipments'], [])
        result = get_shipment(self.company, self.shipment.pk)
        self.assertFalse(result['ok'])

    def test_confirm_refuses_deleted_shipment(self):
        proposal = ProposedAction.objects.create(
            company=self.company,
            user=self.user,
            summary='Nothing.',
            actions=[
                {
                    'kind': 'stage_done',
                    'shipment_id': self.shipment.pk,
                    'stage_key': 'arrived_at_port',
                    'container_number': 'CSQU3054383',
                }
            ],
            transcript='',
        )
        self._delete()
        response = self.client.post(
            '/agent/assistant/confirm/',
            data=json.dumps({'proposal_id': proposal.pk}),
            content_type='application/json',
        )
        payload = json.loads(response.content)
        self.assertEqual(payload['recorded'], [])
        self.assertEqual(payload['refused'], ['This shipment was deleted.'])

    def test_bl_number_can_be_reused(self):
        self._delete()
        response = self.client.post(
            '/agent/shipments/new/',
            {
                'importer_name': 'Second Importer',
                'bl_number': 'delbl001',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'c_number': ['CSQU3054383'],
                'c_size': ['40ft'],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Shipment.objects.filter(
                bl_number='DELBL001', deleted_at__isnull=True
            ).exists()
        )

    def test_restore_brings_it_back(self):
        self._delete()
        response = self.client.get('/agent/shipments/deleted/')
        self.assertContains(response, 'DELBL001')
        self.assertContains(response, 'Restore')
        response = self.client.post(
            '/agent/shipments/%d/restore/' % self.shipment.pk,
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'DELBL001 restored.')
        self.assertContains(response, 'DELBL001')
        self.assertEqual(
            response.redirect_chain, [('/agent/shipments/', 302)]
        )
        self.shipment.refresh_from_db()
        self.assertIsNone(self.shipment.deleted_at)
        self.assertTrue(
            AuditLog.objects.filter(
                shipment=self.shipment, action='shipment_restored'
            ).exists()
        )
        self.assertEqual(
            self.client.get(
                '/agent/shipments/%d/' % self.shipment.pk
            ).status_code,
            200,
        )

    def test_restore_refuses_bl_clash(self):
        self._delete()
        clash = _shipment(
            self.company, bl='DELBL001', importer_name='Other Importer'
        )
        response = self.client.post(
            '/agent/shipments/%d/restore/' % self.shipment.pk,
            follow=True,
        )
        self.assertContains(
            response, 'already uses this BL number'
        )
        self.shipment.refresh_from_db()
        self.assertIsNotNone(self.shipment.deleted_at)
        clash.refresh_from_db()
        self.assertIsNone(clash.deleted_at)

    def test_other_company_gives_404(self):
        other_company = AgentCompany.objects.create(name='Other Co')
        _agent(other_company, 'other-user')
        foreign = _shipment(
            other_company, bl='FOREIGNBL1', importer_name='Foreign Imp'
        )
        delete_url = '/agent/shipments/%d/delete/' % foreign.pk
        restore_url = '/agent/shipments/%d/restore/' % foreign.pk
        self.assertEqual(self.client.get(delete_url).status_code, 404)
        self.assertEqual(self.client.post(delete_url).status_code, 404)
        self.assertEqual(self.client.get(restore_url).status_code, 405)
        self.assertEqual(self.client.post(restore_url).status_code, 404)
        response = self.client.get('/agent/shipments/deleted/')
        self.assertNotContains(response, 'FOREIGNBL1')

    def test_login_required(self):
        self.client.logout()
        url = '/agent/shipments/%d/delete/' % self.shipment.pk
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/agent/login/', response['Location'])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.shipment.refresh_from_db()
        self.assertIsNone(self.shipment.deleted_at)

    def test_no_csrf_exemptions(self):
        from pathlib import Path
        source = (Path(__file__).parent / 'agent_views.py').read_text()
        self.assertNotIn('csrf_exempt', source)
