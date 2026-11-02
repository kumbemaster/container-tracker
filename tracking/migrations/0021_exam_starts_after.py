from django.db import migrations


STARTS_AFTER = {
    'shipping_line_processed': 'paar_assessed',
    'terminal_processed': 'paar_assessed',
    'customs_processed': 'duty_paid',
    'customs_examination': 'duty_paid',
    'en_route_to_bonded_terminal': 'duty_paid',
}


def set_starts_after(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    for key, after in STARTS_AFTER.items():
        Stage.objects.filter(key=key).update(starts_after=after)
    Stage.objects.exclude(key__in=list(STARTS_AFTER)).update(
        starts_after=''
    )


def unset_starts_after(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    Stage.objects.filter(key='customs_examination').update(
        starts_after=''
    )


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0020_set_starts_after'),
    ]

    operations = [
        migrations.RunPython(set_starts_after, unset_starts_after),
    ]
