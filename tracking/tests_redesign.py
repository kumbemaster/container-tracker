import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase

from .models import (
    AgentCompany,
    Container,
    Importer,
    Shipment,
    StaffMember,
)


def _agent(company, username):
    user = User.objects.create_user(username=username, password='secret123')
    StaffMember.objects.create(
        user=user, agent_company=company, role=StaffMember.Role.OWNER
    )
    return user


class AgentRedesignTests(TestCase):
    def setUp(self):
        self.company = AgentCompany.objects.create(name='Design Co')
        self.client.force_login(_agent(self.company, 'design-user'))
        importer = Importer.objects.create(
            agent_company=self.company,
            name='Design Importer',
            access_code='123456',
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='DESIGNBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        Container.objects.create(
            shipment=shipment, number='CSQU3054383', size='40ft'
        )
        self.shipment = shipment

    def test_stylesheet_link_has_version(self):
        response = self.client.get('/agent/shipments/')
        self.assertRegex(
            response.content.decode(), r'style\.css\?v=\d+'
        )

    def test_viewport_meta_present(self):
        response = self.client.get('/agent/shipments/')
        self.assertContains(
            response,
            '<meta name="viewport" '
            'content="width=device-width, initial-scale=1">',
        )

    def test_row_actions_use_button_classes(self):
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'class="btn btn-sm"')
        self.assertContains(response, '>Open</a>')
        self.assertContains(response, 'btn-danger')
        self.assertContains(response, '>Delete</a>')

    def test_mobile_cards_beside_table(self):
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'ship-cards')
        self.assertContains(response, 'ship-bl')
        self.assertContains(response, 'ship-actions')
        self.assertContains(response, 'agent-table')

    def test_two_column_wrapper(self):
        for url in (
            '/agent/shipments/',
            '/agent/shipments/%d/' % self.shipment.pk,
        ):
            response = self.client.get(url)
            self.assertContains(response, 'agent-cols')
            self.assertContains(response, 'agent-side')
        css = self._css()
        self.assertIn('position: sticky', css)
        self.assertIn('agent-side', css)

    def test_page_header_and_count(self):
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'page-head')
        self.assertContains(response, 'count-line')
        self.assertContains(response, '1 open shipment')
        self.assertContains(response, 'class="pill"')

    def test_detail_header(self):
        response = self.client.get(
            '/agent/shipments/%d/' % self.shipment.pk
        )
        self.assertContains(response, 'detail-bl')
        self.assertContains(response, 'detail-grid')

    def test_reply_panel_wiring(self):
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'reply-panel')
        self.assertContains(response, 'assistant-confirm-title')

    def test_menu_and_narrow_pages(self):
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'agent-menu')
        self.assertContains(response, '<summary>Menu</summary>')
        response = self.client.get('/agent/importers/')
        self.assertNotContains(response, 'agent-narrow')
        self.assertIn('stack-table', self._css())
        self.assertIn('imp-actions', self._css())
        response = self.client.get('/agent/login/')
        self.assertContains(response, 'agent-narrow')

    def test_agent_font_floor(self):
        """No px font below 14px in agent rules, except 13px labels."""
        css = self._css()
        offenders = []
        for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
            selector, body = match.group(1), match.group(2)
            if not any(
                token in selector
                for token in (
                    '.agent-', '.btn', '.pill', '.ship-', '.page-',
                    '.reply-panel', '.count-line', '.detail-',
                    '#assistant',
                )
            ):
                continue
            for size in re.finditer(r'font-size:\s*([\d.]+)px', body):
                value = float(size.group(1))
                if value >= 14:
                    continue
                if value >= 13 and (
                    'uppercase' in body
                    or '.agent-wrap label' in selector
                    or '.mini-label' in selector
                    or '.head-label' in selector
                    or '.picker-label' in selector
                ):
                    continue
                if '.i-circle' in selector:
                    continue
                offenders.append((selector.strip(), size.group(0)))
        self.assertEqual(offenders, [])

    def _css(self):
        return (
            Path(__file__).parent / 'static' / 'tracking' / 'style.css'
        ).read_text()
