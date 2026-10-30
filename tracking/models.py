from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


class AgentCompany(models.Model):
    name = models.CharField(max_length=200, unique=True)

    def __str__(self):
        return self.name


class Importer(models.Model):
    agent_company = models.ForeignKey(AgentCompany, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    access_code = models.CharField(
        max_length=6,
        validators=[RegexValidator(r'^\d{6}$', 'Access code must be 6 digits.')],
    )
    access_active = models.BooleanField(default=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('agent_company', 'name')]

    def __str__(self):
        return f'{self.name} ({self.agent_company})'


class Shipment(models.Model):
    importer = models.ForeignKey(Importer, on_delete=models.CASCADE)
    bl_number = models.CharField(max_length=100, db_index=True)
    shipping_line = models.CharField(max_length=200)
    port = models.CharField(max_length=200)
    destination_area = models.CharField(max_length=200)
    discharge_date = models.DateField(null=True, blank=True)
    shipping_line_free_days = models.PositiveIntegerField(null=True, blank=True)
    terminal_free_days = models.PositiveIntegerField(null=True, blank=True)
    return_window_days = models.PositiveIntegerField(default=14)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.bl_number


class Container(models.Model):
    class WaitingOn(models.TextChoices):
        CUSTOMS = 'customs', 'Customs'
        TERMINAL = 'terminal', 'Terminal'
        SHIPPING_LINE = 'shipping_line', 'Shipping line'
        IMPORTER = 'importer', 'Importer'
        AGENT = 'agent', 'Agent'
        NOBODY = 'nobody', 'Nobody'

    shipment = models.ForeignKey(
        Shipment, on_delete=models.CASCADE, related_name='containers'
    )
    number = models.CharField(max_length=50, db_index=True)
    size = models.CharField(max_length=10)
    waiting_on = models.CharField(
        max_length=20, choices=WaitingOn.choices, default=WaitingOn.NOBODY
    )
    waiting_on_since = models.DateTimeField(null=True, blank=True)
    waiting_on_note = models.TextField(blank=True, default='')

    class Meta:
        unique_together = [('shipment', 'number')]

    def __str__(self):
        return self.number


class Stage(models.Model):
    class Phase(models.TextChoices):
        ARRIVAL = 'arrival', 'Arrival'
        RELEASE = 'release', 'Release'
        DELIVERY = 'delivery', 'Delivery'
        RETURN = 'return', 'Return'

    order = models.PositiveIntegerField(unique=True)
    key = models.SlugField(max_length=100, unique=True)
    label = models.CharField(max_length=200)
    is_optional = models.BooleanField(default=False)
    is_sequenced = models.BooleanField(default=True)
    is_processing_track = models.BooleanField(default=False)
    phase = models.CharField(
        max_length=20, choices=Phase.choices, default=Phase.ARRIVAL
    )

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.label


class StageEvent(models.Model):
    container = models.ForeignKey(Container, on_delete=models.CASCADE)
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT)
    occurred_at = models.DateTimeField(null=True, blank=True)
    skipped = models.BooleanField(default=False)
    note = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('container', 'stage')]

    def __str__(self):
        state = 'skipped' if self.skipped else 'done'
        return f'{self.container} - {self.stage} ({state})'


class DeviceSession(models.Model):
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f'Session {self.pk}'


class DeviceGrant(models.Model):
    session = models.ForeignKey(DeviceSession, on_delete=models.CASCADE)
    importer = models.ForeignKey(Importer, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked = models.BooleanField(default=False)

    class Meta:
        unique_together = [('session', 'importer')]

    def __str__(self):
        state = 'revoked' if self.revoked else 'active'
        return f'{self.session} -> {self.importer} ({state})'


class AccessAttempt(models.Model):
    key = models.CharField(max_length=200, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f'{self.key} at {self.created_at}'


class StaffMember(models.Model):
    class Role(models.TextChoices):
        OWNER = 'owner', 'Owner'
        STAFF = 'staff', 'Staff'

    user = models.OneToOneField('auth.User', on_delete=models.CASCADE)
    agent_company = models.ForeignKey(AgentCompany, on_delete=models.CASCADE)
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.STAFF
    )

    def __str__(self):
        return f'{self.user.username} ({self.agent_company})'
