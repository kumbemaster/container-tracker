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
        self.assertContains(response, 'ship-head')
        self.assertContains(response, 'ship-bl')
        self.assertContains(response, 'Back to shipments')

    def test_detail_header_compact_content(self):
        html = self.client.get(
            '/agent/shipments/%d/' % self.shipment.pk
        ).content.decode()
        self.assertIn('DESIGNBL001', html)
        self.assertIn('Design Importer', html)
        self.assertIn('Onne &rarr; Port Harcourt', html)
        self.assertIn('1 containers', html)
        self.assertIn('Never updated', html)
        self.assertIn('id="copy-code"', html)
        self.assertIn('aria-label="Copy code"', html)
        self.assertIn('Copy message', html)
        self.assertIn('>Delete</a>', html)
        self.assertIn('<details class="msg-preview">', html)
        self.assertIn('Preview message', html)
        self.assertIn('Track your shipment', html)

    def test_account_two_cards(self):
        from .models import SecurityProfile

        user = User.objects.get(username='design-user')
        SecurityProfile.objects.create(
            user=user,
            question='What was the name of your first school?',
            answer_hash='x',
        )
        html = self.client.get('/agent/account/').content.decode()
        self.assertIn('acct-grid', html)
        self.assertIn('Change password', html)
        self.assertIn('Security question', html)
        self.assertIn('Set</span>', html)
        self.assertIn(
            'What was the name of your first school?', html
        )
        self.assertNotIn('answer_hash', html)
        self.assertIn(
            'Used to reset your password if you forget it.', html
        )
        self.assertEqual(html.count('action="/agent/account/"'), 2)
        forms = re.findall(
            r'<form method="post" action="/agent/account/">(.*?)</form>',
            html,
            re.S,
        )
        self.assertEqual(len(forms), 2)
        for body in forms:
            self.assertIn('csrfmiddlewaretoken', body)

    def test_account_not_set_chip(self):
        html = self.client.get('/agent/account/').content.decode()
        self.assertIn('Not set</span>', html)
        self.assertNotIn('Set</span>', html)

    def test_importers_newest_first(self):
        import re

        Importer.objects.create(
            agent_company=self.company,
            name='Alpha Foods',
            access_code='111111',
        )
        Importer.objects.create(
            agent_company=self.company,
            name='Zulu Foods',
            access_code='222222',
        )

        def names(path):
            html = self.client.get(path).content.decode()
            return re.findall(
                r'<h2 class="imp-name">([^<]+)</h2>', html
            )

        ordered = names('/agent/importers/')
        self.assertLess(
            ordered.index('Zulu Foods'),
            ordered.index('Alpha Foods'),
        )
        filtered = names('/agent/importers/?q=zulu')
        self.assertEqual(filtered, ['Zulu Foods'])
        both = names('/agent/importers/?q=foods')
        self.assertEqual(both, ['Zulu Foods', 'Alpha Foods'])

    def test_notices_name_account_page(self):
        page = self.client.get('/agent/shipments/').content.decode()
        self.assertIn('on the Account page', page)
        signup = self.client.get('/agent/signup/').content.decode()
        self.assertIn('on the Account page', signup)

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
