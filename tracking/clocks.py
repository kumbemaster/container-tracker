from datetime import timedelta

from django.utils import timezone

from .models import StageEvent


def _lagos_today(today=None):
    if today is not None:
        return today
    return timezone.localdate()


def _completed_date(container, stage_key):
    event = StageEvent.objects.filter(
        container=container, stage__key=stage_key
    ).first()
    if event is None or event.skipped or event.occurred_at is None:
        return None
    return timezone.localtime(event.occurred_at).date()


def _running(label, kind, end_date, days_left):
    if days_left < 0:
        state = 'red'
    elif days_left <= 2:
        state = 'amber'
    else:
        state = 'ok'
    return {
        'kind': kind,
        'label': label,
        'state': state,
        'end_date': end_date,
        'days_left': days_left,
        'last_free_day': days_left == 0,
    }


def port_clock(container, label, free_days, today=None):
    discharge = container.shipment.discharge_date
    if discharge is None or free_days is None:
        return None
    end_date = discharge + timedelta(days=free_days)
    gate_out = _completed_date(container, 'gate_out')
    if gate_out is not None:
        return {
            'kind': 'free_time',
            'label': label,
            'state': 'stopped',
            'end_date': end_date,
            'stopped_date': gate_out,
            'within_free_time': gate_out <= end_date,
        }
    day = _lagos_today(today)
    return _running(label, 'free_time', end_date, (end_date - day).days)


def return_clock(container, today=None):
    delivered = _completed_date(container, 'delivered')
    if delivered is None:
        return None
    end_date = delivered + timedelta(days=container.shipment.return_window_days)
    returned = _completed_date(container, 'empty_container_returned')
    if returned is not None:
        return {
            'kind': 'return',
            'label': 'Empty container return',
            'state': 'stopped',
            'end_date': end_date,
            'stopped_date': returned,
            'within_window': returned <= end_date,
        }
    day = _lagos_today(today)
    return _running(
        'Empty container return', 'return', end_date, (end_date - day).days
    )


def build_clocks(container, today=None):
    shipment = container.shipment
    return [
        port_clock(container, 'Terminal free time', shipment.terminal_free_days, today),
        port_clock(
            container,
            'Shipping line free time',
            shipment.shipping_line_free_days,
            today,
        ),
        return_clock(container, today),
    ]
