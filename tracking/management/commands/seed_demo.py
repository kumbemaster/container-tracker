from django.core.management.base import BaseCommand

from tracking.demo_data import seed_demo_data


class Command(BaseCommand):
    help = 'Create invented demo data (safe to run twice).'

    def handle(self, *args, **options):
        data = seed_demo_data()
        self.stdout.write(self.style.SUCCESS(
            'Demo shipment ready: %s'
            % data['shipment1'].bl_number
        ))
        self.stdout.write(self.style.SUCCESS(
            'Demo shipment ready: %s'
            % data['shipment2'].bl_number
        ))
