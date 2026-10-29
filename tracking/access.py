import hashlib
import hmac
import secrets
from datetime import timedelta

from django.utils import timezone

from .models import AccessAttempt, Container, DeviceGrant, DeviceSession, Shipment

MAX_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


def find_target(query):
    if not isinstance(query, str):
        return None
    text = query.strip().upper()
    if not text:
        return None
    containers = list(
        Container.objects.filter(number=text).select_related('shipment')
    )
    if containers:
        def sort_key(container):
            shipment = container.shipment
            closed = shipment.closed_at is not None
            return (closed, -shipment.pk)
        containers.sort(key=sort_key)
        return containers[0]
    return Shipment.objects.filter(bl_number=text).order_by('-pk').first()


def code_is_valid(importer, code):
    if not importer.access_active:
        return False
    if not isinstance(code, str):
        return False
    if not hmac.compare_digest(importer.access_code, code):
        return False
    if importer.access_expires_at is not None and importer.access_expires_at <= timezone.now():
        return False
    return True


def create_device_session():
    raw_token = secrets.token_urlsafe(32)
    session = DeviceSession.objects.create(token_hash=_hash_token(raw_token))
    return session, raw_token


def get_session(raw_token):
    if not isinstance(raw_token, str) or not raw_token:
        return None
    try:
        session = DeviceSession.objects.get(token_hash=_hash_token(raw_token))
    except DeviceSession.DoesNotExist:
        return None
    session.last_seen = timezone.now()
    session.save(update_fields=['last_seen'])
    return session


def grant_access(session, importer):
    grant, _ = DeviceGrant.objects.get_or_create(session=session, importer=importer)
    if grant.revoked:
        grant.revoked = False
        grant.save(update_fields=['revoked'])
    return grant


def has_access(session, importer):
    if session is None:
        return False
    return DeviceGrant.objects.filter(
        session=session, importer=importer, revoked=False
    ).exists()


def is_locked(key):
    cutoff = timezone.now() - LOCKOUT_WINDOW
    failures = AccessAttempt.objects.filter(key=key, created_at__gte=cutoff).count()
    return failures >= MAX_FAILURES


def record_failure(key):
    AccessAttempt.objects.create(key=key)


def rotate_code(importer, new_code):
    importer.access_code = new_code
    importer.save(update_fields=['access_code'])
    DeviceGrant.objects.filter(importer=importer).update(revoked=True)
