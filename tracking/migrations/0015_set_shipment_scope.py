from django.db import migrations


SHIPMENT_SCOPE_KEYS = [
    'arrived_at_port',
    'manifest_available',
    'paar_assessed',
    'duty_paid',
]


def set_shipment_scope(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    Stage.objects.filter(key__in=SHIPMENT_SCOPE_KEYS).update(
        scope='shipment'
    )


def unset_shipment_scope(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    Stage.objects.filter(key__in=SHIPMENT_SCOPE_KEYS).update(
        scope='container'
    )


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0014_stage_scope'),
    ]

    operations = [
        migrations.RunPython(set_shipment_scope, unset_shipment_scope),
    ]
