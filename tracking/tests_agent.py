from html.parser import HTMLParser
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone


class _RenderedFormParser(HTMLParser):
    def __init__(self, action):
        super().__init__()
        self.wanted = action
        self.forms = []
        self._current = None
        self._inside = False
        self._select = None
        self._select_options = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            if self.wanted in attrs.get('action', ''):
                self._current = {'action': attrs['action'], 'data': {}}
                self.forms.append(self._current)
                self._inside = True
            else:
                self._inside = False
                self._current = None
            self._select = None
            return
        if not self._inside or self._current is None:
            return
        if tag == 'input':
            name = attrs.get('name')
            if not name:
                return
            kind = attrs.get('type', 'text')
            if kind == 'checkbox':
                if 'checked' in attrs:
                    current = self._current['data'].setdefault(name, [])
                    if not isinstance(current, list):
                        current = self._current['data'][name] = [current]
                    current.append(attrs.get('value', ''))
            elif kind in ('hidden', 'text', 'datetime-local'):
                self._current['data'][name] = attrs.get('value', '')
        elif tag == 'select':
            self._select = attrs.get('name')
            self._select_options = []
        elif tag == 'option' and self._select is not None:
            self._select_options.append(
                (attrs.get('value', ''), 'selected' in attrs)
            )

    def handle_endtag(self, tag):
        if tag == 'form':
            self._inside = False
            self._current = None
            self._select = None
            return
        if tag == 'select' and self._current is not None and self._select:
            chosen = next(
                (value for value, selected in self._select_options if selected),
                self._select_options[0][0]
                if self._select_options
                else '',
            )
            self._current['data'][self._select] = chosen
            self._select = None


def rendered_forms(response, action):
    parser = _RenderedFormParser(action)
    parser.feed(response.content.decode())
    return parser.forms

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

    def _new_shipment_post(self, importer_pk='', **overrides):
        data = {
            'importer': importer_pk,
            'new_importer_name': '',
            'bl_number': 'NEWBL001',
            'shipping_line': '',
            'port': 'Onne',
            'destination_area': 'Port Harcourt',
            'c_number': ['CSQU3054383', 'DEMO4416733'],
            'c_size': ['40ft', '20ft'],
        }
        data.update(overrides)
        return self.client.post('/agent/shipments/new/', data)

    def test_create_shipment_with_valid_containers(self):
        company = self._company('Create Co')
        user = self._agent(company, 'creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self._new_shipment_post(
            importer_pk=str(importer.pk), bl_number='newbl001'
        )
        self.assertEqual(response.status_code, 302)
        shipment = Shipment.objects.get(bl_number='NEWBL001')
        rows = dict(
            shipment.containers.values_list('number', 'size')
        )
        self.assertEqual(
            rows, {'CSQU3054383': '40ft', 'DEMO4416733': '20ft'}
        )
        self.assertEqual(shipment.shipping_line, '')
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'NEWBL001')

    def test_new_form_fields(self):
        company = self._company('Fields Co')
        user = self._agent(company, 'fields-viewer')
        self._importer(company)
        self.client.force_login(user)
        response = self.client.get('/agent/shipments/new/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="c_number"')
        self.assertContains(response, 'name="c_size"')
        for size in ('20ft', '40ft', '40HC'):
            self.assertContains(response, size)
        self.assertContains(response, 'As written on the bill of lading')
        self.assertContains(
            response, 'Container numbers have 4 letters and 7 digits'
        )
        self.assertNotContains(response, 'name="discharge_date"')
        self.assertNotContains(response, 'free_days')
        self.assertNotContains(response, 'Free time')

    def test_invalid_check_digit_creates_nothing(self):
        company = self._company('Invalid Co')
        user = self._agent(company, 'invalid-creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self._new_shipment_post(
            importer_pk=str(importer.pk),
            bl_number='BADBL001',
            c_number=['CSQU3054384'],
            c_size=['40ft'],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a valid container number')
        self.assertContains(response, 'CSQU3054384')
        self.assertFalse(Shipment.objects.filter(bl_number='BADBL001').exists())

    def test_duplicate_container_number_creates_nothing(self):
        company = self._company('Duplicate Co')
        user = self._agent(company, 'dup-creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self._new_shipment_post(
            importer_pk=str(importer.pk),
            bl_number='DUPBL001',
            c_number=['CSQU3054383', 'csqu-3054383'],
            c_size=['40ft', '20ft'],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'uplicate container number')
        self.assertContains(response, 'CSQU3054383')
        self.assertFalse(Shipment.objects.filter(bl_number='DUPBL001').exists())

    def test_missing_size_creates_nothing(self):
        company = self._company('Size Co')
        user = self._agent(company, 'size-creator')
        importer = self._importer(company)
        self.client.force_login(user)
        response = self._new_shipment_post(
            importer_pk=str(importer.pk),
            bl_number='SIZEBL001',
            c_number=['CSQU3054383'],
            c_size=[''],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose a size')
        self.assertContains(response, 'CSQU3054383')
        self.assertFalse(
            Shipment.objects.filter(bl_number='SIZEBL001').exists()
        )

    def test_at_least_one_container_required(self):
        company = self._company('Empty Co')
        user = self._agent(company, 'empty-creator')
        importer = self._importer(company)
        self.client.force_login(user)
        data = {
            'importer': str(importer.pk),
            'new_importer_name': '',
            'bl_number': 'EMPTYBL001',
            'shipping_line': '',
            'port': 'Onne',
            'destination_area': 'Port Harcourt',
        }
        response = self.client.post('/agent/shipments/new/', data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Add at least one container')
        self.assertFalse(
            Shipment.objects.filter(bl_number='EMPTYBL001').exists()
        )

    def test_new_importer_gets_six_digit_text_code(self):
        company = self._company('Code Co')
        user = self._agent(company, 'code-creator')
        self.client.force_login(user)
        response = self._new_shipment_post(
            new_importer_name='Brand New Importer', bl_number='CODEBL001'
        )
        self.assertEqual(response.status_code, 302)
        importer = Importer.objects.get(name='Brand New Importer')
        self.assertRegex(importer.access_code, r'^\d{6}$')

    def test_add_importer(self):
        company = self._company('Add Co')
        user = self._agent(company, 'adder')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/importers/add/', {'name': '  Fresh Foods  '}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        importer = Importer.objects.get(
            agent_company=company, name='Fresh Foods'
        )
        self.assertRegex(importer.access_code, r'^\d{6}$')
        self.assertTrue(importer.access_active)
        self.assertIsNone(importer.access_expires_at)
        self.assertContains(response, 'Fresh Foods')
        self.assertContains(response, importer.access_code)
        self.assertContains(response, 'Your tracking code for')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_add_importer_rejects_duplicates_and_blanks(self):
        company = self._company('Dup Name Co')
        user = self._agent(company, 'dup-adder')
        self._importer(company, name='Existing Foods')
        self.client.force_login(user)
        for bad_name in ('', '   ', 'existing foods', 'EXISTING FOODS'):
            response = self.client.post(
                '/agent/importers/add/', {'name': bad_name}, follow=True
            )
            self.assertContains(response, 'error')
        self.assertEqual(
            Importer.objects.filter(agent_company=company).count(), 1
        )

    def test_same_importer_name_allowed_in_other_company(self):
        company_a = self._company('Same A')
        company_b = self._company('Same B')
        user = self._agent(company_a, 'same-adder')
        self._importer(company_b, name='Shared Foods')
        self.client.force_login(user)
        response = self.client.post(
            '/agent/importers/add/', {'name': 'Shared Foods'}, follow=True
        )
        self.assertContains(response, 'Shared Foods')
        self.assertTrue(
            Importer.objects.filter(
                agent_company=company_a, name='Shared Foods'
            ).exists()
        )

    def test_importer_list_shows_counts_and_preselect(self):
        company = self._company('List Co')
        other = self._company('Other List Co')
        user = self._agent(company, 'lister')
        importer = self._importer(company, name='Listed Foods')
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='LISTBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        Container.objects.create(
            shipment=shipment, number='CSQU3054383', size='40ft'
        )
        foreign = self._importer(other, name='Foreign Foods')
        self.client.force_login(user)
        response = self.client.get('/agent/importers/')
        self.assertContains(response, 'New shipment')
        self.assertContains(response, '<td>1</td>')
        self.assertContains(
            response, '/agent/shipments/new/?importer=%d' % importer.pk
        )
        response = self.client.get(
            '/agent/shipments/new/?importer=%d' % importer.pk
        )
        self.assertIn(
            '<option value="%d" selected>' % importer.pk,
            response.content.decode(),
        )
        response = self.client.get(
            '/agent/shipments/new/?importer=%d' % foreign.pk
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            '<option value="%d" selected>' % foreign.pk,
            response.content.decode(),
        )
        self.assertNotContains(response, 'Foreign Foods')

    def test_importer_add_requires_login(self):
        response = self.client.post(
            '/agent/importers/add/', {'name': 'Nope'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

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

    def test_importer_shown_by_name_only(self):
        company = self._company('Acme Clearing Ltd')
        user = self._agent(company, 'solo-viewer')
        importer = self._importer(company, name='Solo Foods')
        shipment = self._shipment(importer, bl='SOLOBL001')
        self.client.force_login(user)
        response = self.client.get('/agent/importers/')
        self.assertContains(response, 'Solo Foods')
        self.assertNotContains(response, 'Acme Clearing Ltd')
        response = self.client.get('/agent/shipments/new/')
        self.assertContains(response, '>Solo Foods</option>')
        self.assertNotContains(response, 'Acme Clearing Ltd')
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'Solo Foods')
        self.assertNotContains(response, 'Acme Clearing Ltd')
        response = self.client.get(
            '/agent/shipments/%d/' % shipment.pk
        )
        self.assertContains(response, 'Solo Foods')
        self.assertNotContains(response, 'Acme Clearing Ltd')
        response = self.client.post(
            '/agent/importers/add/', {'name': 'Second Foods'}, follow=True
        )
        self.assertContains(response, 'Second Foods')
        self.assertNotContains(response, 'Acme Clearing Ltd')

    def _rename(self, pk, name):
        return self.client.post(
            '/agent/importers/%d/rename/' % pk,
            {'name': name},
            follow=True,
        )

    def test_rename_importer_keeps_code(self):
        company = self._company('Rename Co')
        user = self._agent(company, 'renamer')
        importer = self._importer(company, name='Old Foods')
        session, _ = access_helpers.create_device_session()
        access_helpers.grant_access(session, importer)
        code = importer.access_code
        self.client.force_login(user)
        response = self.client.get('/agent/importers/')
        self.assertContains(response, 'Edit name')
        response = self._rename(importer.pk, '  New Foods  ')
        self.assertContains(response, 'Renamed importer to New Foods.')
        importer.refresh_from_db()
        self.assertEqual(importer.name, 'New Foods')
        self.assertEqual(importer.access_code, code)
        self.assertTrue(
            access_helpers.has_access(session, importer)
        )

    def test_rename_to_existing_name_refused(self):
        company = self._company('Rename Dup Co')
        user = self._agent(company, 'rename-dupper')
        alpha = self._importer(company, name='Alpha Foods')
        self._importer(company, name='Beta Foods')
        self.client.force_login(user)
        for clash in ('Beta Foods', 'beta foods', 'BETA FOODS'):
            response = self._rename(alpha.pk, clash)
            self.assertContains(response, 'already exists')
        alpha.refresh_from_db()
        self.assertEqual(alpha.name, 'Alpha Foods')

    def test_rename_case_change_allowed(self):
        company = self._company('Rename Case Co')
        user = self._agent(company, 'rename-caser')
        importer = self._importer(company, name='Alpha Foods')
        self.client.force_login(user)
        response = self._rename(importer.pk, 'ALPHA FOODS')
        self.assertContains(response, 'Renamed importer to ALPHA FOODS.')
        importer.refresh_from_db()
        self.assertEqual(importer.name, 'ALPHA FOODS')

    def test_rename_empty_refused(self):
        company = self._company('Rename Blank Co')
        user = self._agent(company, 'rename-blanker')
        importer = self._importer(company, name='Kept Foods')
        self.client.force_login(user)
        for blank in ('', '   '):
            response = self._rename(importer.pk, blank)
            self.assertContains(response, 'Enter an importer name.')
        importer.refresh_from_db()
        self.assertEqual(importer.name, 'Kept Foods')

    def test_rename_other_company_404(self):
        company_a = self._company('Rename A')
        company_b = self._company('Rename B')
        user_a = self._agent(company_a, 'rename-a')
        importer_b = self._importer(company_b, name='B Foods')
        self.client.force_login(user_a)
        response = self.client.post(
            '/agent/importers/%d/rename/' % importer_b.pk,
            {'name': 'Taken Foods'},
        )
        self.assertEqual(response.status_code, 404)
        importer_b.refresh_from_db()
        self.assertEqual(importer_b.name, 'B Foods')

    def test_rename_requires_login(self):
        response = self.client.post(
            '/agent/importers/1/rename/', {'name': 'Nope'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

    def test_no_delete_route(self):
        company = self._company('No Delete Co')
        user = self._agent(company, 'no-deleter')
        importer = self._importer(company, name='Stay Foods')
        self.client.force_login(user)
        for method in ('get', 'post'):
            response = getattr(self.client, method)(
                '/agent/importers/%d/delete/' % importer.pk
            )
            self.assertEqual(response.status_code, 404)
        self.assertTrue(
            Importer.objects.filter(pk=importer.pk).exists()
        )

    def test_duplicate_bl_refused_with_link(self):
        company = self._company('Dup BL Co')
        user = self._agent(company, 'dup-bl')
        importer = self._importer(company)
        existing = self._shipment(importer, bl='DUPBL001')
        existing.closed_at = timezone.now()
        existing.save()
        self.client.force_login(user)
        response = self._new_shipment_post(
            importer_pk=str(importer.pk),
            bl_number=' dup bl001 ',
            c_number=['CSQU3054383'],
            c_size=['40ft'],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'A shipment with this BL number already exists.'
        )
        self.assertContains(
            response, '/agent/shipments/%d/' % existing.pk
        )
        self.assertContains(response, 'CSQU3054383')
        self.assertEqual(
            Shipment.objects.filter(
                importer__agent_company=company
            ).count(),
            1,
        )

    def test_same_bl_other_company_allowed(self):
        company_a = self._company('BL Same A')
        company_b = self._company('BL Same B')
        user_a = self._agent(company_a, 'bl-a')
        self._shipment(self._importer(company_b), bl='SHAREDBL001')
        importer_a = self._importer(company_a)
        self.client.force_login(user_a)
        response = self._new_shipment_post(
            importer_pk=str(importer_a.pk), bl_number='sharedbl001'
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Shipment.objects.filter(
                importer__agent_company=company_a,
                bl_number='SHAREDBL001',
            ).exists()
        )


class RenderedFormTests(TestCase):
    def setUp(self):
        from datetime import timedelta

        from .models import Stage, StageEvent

        company = AgentCompany.objects.create(name='Rendered Co')
        user = User.objects.create_user(
            username='renderer', password='secret123'
        )
        StaffMember.objects.create(
            user=user,
            agent_company=company,
            role=StaffMember.Role.OWNER,
        )
        importer = Importer.objects.create(
            agent_company=company,
            name='Rendered Importer',
            access_code='777777',
        )
        self.shipment = Shipment.objects.create(
            importer=importer,
            bl_number='RENDBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        self.container = Container.objects.create(
            shipment=self.shipment, number='REND1000001', size='40ft'
        )
        now = timezone.now()
        for key, days_ago in [
            ('arrived_at_port', 2),
            ('manifest_available', 1),
        ]:
            StageEvent.objects.create(
                container=self.container,
                stage=Stage.objects.get(key=key),
                occurred_at=now - timedelta(days=days_ago),
            )
        self.client.force_login(user)

    def _detail(self):
        return self.client.get(
            '/agent/shipments/%d/' % self.shipment.pk
        )

    def test_undo_as_rendered(self):
        from .models import AuditLog, StageEvent

        response = self._detail()
        self.assertEqual(response.status_code, 200)
        undo_forms = [
            form
            for form in rendered_forms(response, '/stages/undo/')
            if form['data'].get('stage_key') == 'manifest_available'
        ]
        self.assertEqual(len(undo_forms), 1)
        response = self.client.post(
            undo_forms[0]['action'], undo_forms[0]['data'], follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            StageEvent.objects.filter(
                container=self.container, stage__key='manifest_available'
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                container=self.container, action='stage_undone'
            ).exists()
        )

    def test_update_as_rendered(self):
        from .models import AuditLog, StageEvent

        response = self._detail()
        update_forms = [
            form
            for form in rendered_forms(response, '/stages/update/')
            if 'note' in form['data']
        ]
        self.assertEqual(len(update_forms), 1)
        data = dict(update_forms[0]['data'])
        self.assertIn('value="paar_assessed"', response.content.decode())
        data['stage_key'] = 'paar_assessed'
        response = self.client.post(
            update_forms[0]['action'], data, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Recorded PAAR assessed')
        self.assertTrue(
            StageEvent.objects.filter(
                container=self.container, stage__key='paar_assessed'
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                container=self.container, action='stage_completed'
            ).exists()
        )

    def test_select_and_unselect_as_rendered(self):
        from .models import AuditLog, StageEvent

        response = self._detail()
        select_forms = [
            form
            for form in rendered_forms(response, '/stages/select/')
            if form['data'].get('stage_key') == 'customs_examination'
        ]
        self.assertEqual(len(select_forms), 1)
        response = self.client.post(
            select_forms[0]['action'], select_forms[0]['data'], follow=True
        )
        self.assertContains(response, 'Selected Customs examination')
        event = StageEvent.objects.get(
            container=self.container, stage__key='customs_examination'
        )
        self.assertIsNone(event.occurred_at)
        self.assertTrue(
            AuditLog.objects.filter(
                container=self.container, action='stage_selected'
            ).exists()
        )
        response = self._detail()
        unselect_forms = [
            form
            for form in rendered_forms(response, '/stages/unselect/')
            if form['data'].get('stage_key') == 'customs_examination'
        ]
        self.assertEqual(len(unselect_forms), 1)
        response = self.client.post(
            unselect_forms[0]['action'], unselect_forms[0]['data'], follow=True
        )
        self.assertContains(response, 'Removed Customs examination')
        self.assertFalse(
            StageEvent.objects.filter(
                container=self.container, stage__key='customs_examination'
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                container=self.container, action='stage_unselected'
            ).exists()
        )

    def test_tile_menus(self):
        response = self._detail()
        html = response.content.decode()
        self.assertEqual(html.count('tile-menu'), 3)
        self.assertEqual(html.count('disabled'), 3)
        self.assertEqual(html.count('>Add</button>'), 2)

    def test_no_time_inputs_on_page(self):
        html = self._detail().content.decode()
        self.assertNotIn('datetime-local', html)
        self.assertNotIn('name="when"', html)

    def test_agent_box_contents(self):
        html = self._detail().content.decode()
        self.assertIn('row abox', html)
        self.assertIn('REND1000001', html)
        self.assertIn('Current state', html)
        self.assertIn('Manifest available', html)
        self.assertIn('Updated', html)

    def test_single_box_open_by_default(self):
        response = self._detail()
        html = response.content.decode()
        self.assertEqual(html.count('<details class="row abox"'), 1)
        self.assertIn('<details class="row abox" open>', html)

    def test_two_boxes_closed_by_default(self):
        from .models import Container

        Container.objects.create(
            shipment=self.shipment, number='REND1000002', size='20ft'
        )
        response = self._detail()
        html = response.content.decode()
        self.assertEqual(html.count('<details class="row abox"'), 2)
        self.assertNotIn('<details class="row abox" open>', html)

    def test_picker_selects_all_by_default(self):
        from .models import Container

        Container.objects.create(
            shipment=self.shipment, number='REND1000002', size='20ft'
        )
        html = self._detail().content.decode()
        self.assertIn('id="picker-all"', html)
        self.assertIn('Select all containers', html)
        self.assertIn('All containers (2)', html)
        self.assertEqual(html.count('data-num='), 2)
        picker = html.split('id="container-picker"')[1].split('</details>')[0]
        self.assertEqual(picker.count('checked'), 3)

    def test_waiting_as_rendered(self):
        from .models import AuditLog, Container

        response = self._detail()
        waiting_forms = rendered_forms(response, '/waiting/')
        self.assertEqual(len(waiting_forms), 1)
        data = dict(waiting_forms[0]['data'])
        data['waiting_on'] = 'customs'
        data['note'] = 'Hold for inspection'
        response = self.client.post(
            waiting_forms[0]['action'], data, follow=True
        )
        self.assertContains(response, 'Updated waiting status')
        container = Container.objects.get(pk=self.container.pk)
        self.assertEqual(container.waiting_on, 'customs')
        self.assertIsNotNone(container.waiting_on_since)
        self.assertTrue(
            AuditLog.objects.filter(
                container=self.container, action='waiting_changed'
            ).exists()
        )


class StageUpdateTests(TestCase):
    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import Stage, StageEvent

        self.Stage = Stage
        self.StageEvent = StageEvent
        self.td = timedelta
        company = AgentCompany.objects.create(name='Update Co')
        self.user = User.objects.create_user(
            username='updater', password='secret123'
        )
        StaffMember.objects.create(
            user=self.user,
            agent_company=company,
            role=StaffMember.Role.OWNER,
        )
        importer = Importer.objects.create(
            agent_company=company, name='Update Importer', access_code='222222'
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='UPDBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        self.shipment = shipment
        self.now = timezone.now()
        self.container1 = Container.objects.create(
            shipment=shipment, number='UPD1000001', size='40ft'
        )
        self.container2 = Container.objects.create(
            shipment=shipment, number='UPD1000002', size='20ft'
        )
        for key, days_ago in [
            ('arrived_at_port', 4),
            ('manifest_available', 3),
            ('paar_assessed', 2),
            ('duty_paid', 1),
        ]:
            StageEvent.objects.create(
                container=self.container1,
                stage=Stage.objects.get(key=key),
                occurred_at=self.now - timedelta(days=days_ago),
            )
        StageEvent.objects.create(
            container=self.container2,
            stage=Stage.objects.get(key='arrived_at_port'),
            occurred_at=self.now - timedelta(days=1),
        )
        self.client.force_login(self.user)

    def _when(self, days_ago=0):
        from django.utils import timezone

        return timezone.localtime(
            timezone.now() - self.td(days=days_ago)
        ).strftime('%Y-%m-%dT%H:%M')

    def _update_url(self):
        return '/agent/shipments/%d/stages/update/' % self.shipment.pk

    def test_update_two_containers(self):
        from .models import AuditLog

        response = self.client.post(
            self._update_url(),
            {
                'stage_key': 'paar_assessed',
                'when': self._when(),
                'containers': [str(self.container1.pk)],
                'note': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            self._update_url(),
            {
                'stage_key': 'terminal_processed',
                'when': self._when(),
                'containers': [
                    str(self.container1.pk),
                    str(self.container2.pk),
                ],
                'note': '',
            },
            follow=True,
        )
        self.assertTrue(
            self.StageEvent.objects.filter(
                container=self.container1,
                stage__key='terminal_processed',
            ).exists()
        )
        self.assertFalse(
            self.StageEvent.objects.filter(
                container=self.container2,
                stage__key='terminal_processed',
            ).exists()
        )
        self.assertContains(response, 'Recorded Terminal processed')
        self.assertContains(response, 'Duty paid is not recorded yet.')
        self.assertTrue(
            AuditLog.objects.filter(
                shipment=self.shipment,
                container=self.container1,
                action='stage_completed',
            ).exists()
        )
        self.assertFalse(
            AuditLog.objects.filter(
                container=self.container2, action='stage_completed'
            ).exists()
        )

    def test_gate_out_exact_sentence(self):
        for key in ('shipping_line_processed', 'terminal_processed'):
            self.StageEvent.objects.create(
                container=self.container1,
                stage=self.Stage.objects.get(key=key),
                occurred_at=self.now,
            )
        response = self.client.post(
            self._update_url(),
            {
                'stage_key': 'gate_out',
                'when': self._when(),
                'containers': [str(self.container1.pk)],
                'note': '',
            },
            follow=True,
        )
        self.assertContains(
            response, 'Customs processed is not recorded yet.'
        )
        self.assertFalse(
            self.StageEvent.objects.filter(
                container=self.container1, stage__key='gate_out'
            ).exists()
        )

    def test_select_customs_blocks_gate_out(self):
        for key in (
            'shipping_line_processed',
            'terminal_processed',
            'customs_processed',
        ):
            self.StageEvent.objects.create(
                container=self.container1,
                stage=self.Stage.objects.get(key=key),
                occurred_at=self.now,
            )
        select_url = '/agent/shipments/%d/stages/select/' % self.shipment.pk
        self.client.post(
            select_url,
            {
                'stage_key': 'customs_examination',
                'containers': [str(self.container1.pk)],
            },
        )
        response = self.client.post(
            self._update_url(),
            {
                'stage_key': 'gate_out',
                'when': self._when(),
                'containers': [str(self.container1.pk)],
                'note': '',
            },
            follow=True,
        )
        self.assertContains(
            response, 'Customs examination is not recorded yet.'
        )

    def test_posted_time_is_ignored(self):
        from django.utils import timezone

        from .models import StageEvent

        response = self.client.post(
            self._update_url(),
            {
                'stage_key': 'terminal_processed',
                'when': '2020-01-01T00:00',
                'containers': [str(self.container1.pk)],
                'note': '',
            },
            follow=True,
        )
        self.assertContains(response, 'Recorded Terminal processed')
        event = StageEvent.objects.get(
            container=self.container1, stage__key='terminal_processed'
        )
        self.assertGreater(event.occurred_at.year, 2020)
        delta = abs((timezone.now() - event.occurred_at).total_seconds())
        self.assertLess(delta, 120)

    def test_undo_rules(self):
        from .models import AuditLog

        self.StageEvent.objects.create(
            container=self.container1,
            stage=self.Stage.objects.get(key='shipping_line_processed'),
            occurred_at=self.now,
        )
        undo_url = '/agent/shipments/%d/stages/undo/' % self.shipment.pk
        response = self.client.post(
            undo_url,
            {'container': str(self.container1.pk), 'stage_key': 'duty_paid'},
            follow=True,
        )
        self.assertContains(
            response, 'Undo Shipping line processed first.'
        )
        self.assertTrue(
            self.StageEvent.objects.filter(
                container=self.container1, stage__key='duty_paid'
            ).exists()
        )
        response = self.client.post(
            undo_url,
            {
                'container': str(self.container1.pk),
                'stage_key': 'shipping_line_processed',
            },
            follow=True,
        )
        self.assertContains(response, 'Undid Shipping line processed')
        self.assertFalse(
            self.StageEvent.objects.filter(
                container=self.container1,
                stage__key='shipping_line_processed',
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                container=self.container1, action='stage_undone'
            ).exists()
        )

    def test_other_company_update_gives_404(self):
        company_b = AgentCompany.objects.create(name='Other Update Co')
        importer_b = Importer.objects.create(
            agent_company=company_b, name='Other', access_code='999999'
        )
        shipment_b = Shipment.objects.create(
            importer=importer_b,
            bl_number='OTHERBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        response = self.client.post(
            '/agent/shipments/%d/stages/update/' % shipment_b.pk,
            {
                'stage_key': 'arrived_at_port',
                'when': self._when(),
                'containers': [],
                'note': '',
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_importer_page_reflects_update(self):
        from django.core.management import call_command

        call_command('seed_demo')
        demo_company = AgentCompany.objects.get(name='Demo Clearing Ltd')
        demo_user = User.objects.create_user(
            username='demo-updater', password='secret123'
        )
        StaffMember.objects.create(
            user=demo_user,
            agent_company=demo_company,
            role=StaffMember.Role.OWNER,
        )
        demo_shipment = Shipment.objects.get(bl_number='DEMOBL001')
        demo_container = Container.objects.get(number='CSQU3054383')
        self.client.force_login(demo_user)
        self.client.post(
            '/agent/shipments/%d/stages/update/' % demo_shipment.pk,
            {
                'stage_key': 'terminal_processed',
                'when': self._when(),
                'containers': [str(demo_container.pk)],
                'note': '',
            },
        )
        session, raw_token = access_helpers.create_device_session()
        importer = demo_shipment.importer
        access_helpers.grant_access(session, importer)
        self.client.logout()
        self.client.cookies['device_token'] = raw_token
        response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Terminal processed')
