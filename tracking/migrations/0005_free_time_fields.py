from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0004_accessattempt_devicesession_devicegrant'),
    ]

    operations = [
        migrations.RenameField(
            model_name='shipment',
            old_name='free_days',
            new_name='shipping_line_free_days',
        ),
        migrations.AddField(
            model_name='shipment',
            name='terminal_free_days',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='shipment',
            name='return_window_days',
            field=models.PositiveIntegerField(default=14),
        ),
    ]
