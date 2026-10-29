from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tracking.models import AgentCompany, Container, Importer, Shipment, Stage, StageEvent


class Command(BaseCommand):
    help = 'Create invented demo data (safe to run twice).'

    def handle(self, *args, **options):
        now = timezone.now()

        company, _ = AgentCompany.objects.get_or_create(name='Demo Clearing Ltd')

        importer, _ = Importer.objects.get_or_create(
            agent_company=company,
            name='Demo Importer A',
            defaults={'access_code': '482910'},
        )

        shipment, _ = Shipment.objects.get_or_create(
            importer=importer,
            bl_number='DEMOBL001',
            defaults={
                'shipping_line': 'Demo Line',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'discharge_date': (now - timedelta(days=4)).date(),
                'shipping_line_free_days': 5,
                'terminal_free_days': 3,
            },
        )
        shipment.shipping_line_free_days = 5
        shipment.terminal_free_days = 3
        shipment.save(
            update_fields=['shipping_line_free_days', 'terminal_free_days']
        )

        container1, _ = Container.objects.get_or_create(
            shipment=shipment,
            number='CSQU3054383',
            defaults={'size': '40ft'},
        )
        container2, _ = Container.objects.get_or_create(
            shipment=shipment,
            number='DEMO4416733',
            defaults={'size': '20ft'},
        )

        for key, days_ago in [
            ('arrived_at_port', 4),
            ('manifest_available', 3),
            ('paar_assessed', 2),
            ('duty_paid', 1),
            ('shipping_line_processed', 0.5),
        ]:
            StageEvent.objects.get_or_create(
                container=container1,
                stage=Stage.objects.get(key=key),
                defaults={'occurred_at': now - timedelta(days=days_ago)},
            )

        for key, days_ago in [
            ('arrived_at_port', 4),
            ('manifest_available', 3),
        ]:
            StageEvent.objects.get_or_create(
                container=container2,
                stage=Stage.objects.get(key=key),
                defaults={'occurred_at': now - timedelta(days=days_ago)},
            )

        container2.waiting_on = Container.WaitingOn.CUSTOMS
        container2.waiting_on_since = now - timedelta(days=1)
        container2.waiting_on_note = 'Awaiting customs examination.'
        container2.save()

        self.stdout.write(self.style.SUCCESS(f'Demo shipment ready: {shipment.bl_number}'))
