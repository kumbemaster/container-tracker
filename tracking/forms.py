import re

from django import forms

from .models import Importer
from .validators import is_valid_container_number


class ShipmentForm(forms.Form):
    importer = forms.ModelChoiceField(
        queryset=Importer.objects.none(),
        required=False,
        empty_label='Select an importer',
    )
    new_importer_name = forms.CharField(
        max_length=200,
        required=False,
        help_text='Or type a new importer name instead of choosing above.',
    )
    bl_number = forms.CharField(max_length=100)
    shipping_line = forms.CharField(max_length=200)
    port = forms.CharField(max_length=200, initial='Onne')
    destination_area = forms.CharField(max_length=200)
    discharge_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'})
    )
    containers = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='One container per line as "number size".',
    )
    shipping_line_free_days = forms.IntegerField(
        min_value=0, required=False
    )
    terminal_free_days = forms.IntegerField(min_value=0, required=False)
    return_window_days = forms.IntegerField(min_value=0, initial=14)

    def __init__(self, company, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['importer'].queryset = Importer.objects.filter(
            agent_company=company
        ).order_by('name')
        self.company = company

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('importer') and not (
            cleaned.get('new_importer_name') or ''
        ).strip():
            raise forms.ValidationError(
                'Choose an importer or type a new importer name.'
            )
        return cleaned

    def clean_containers(self):
        raw = self.cleaned_data['containers']
        parsed = []
        seen = set()
        errors = []
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[1].strip():
                errors.append(
                    'Line %d: write each container as "number size".' % lineno
                )
                continue
            number = parts[0].strip().upper()
            size = parts[1].strip()
            if not is_valid_container_number(number):
                errors.append(
                    'Line %d: %s is not a valid container number.'
                    % (lineno, number)
                )
                continue
            if number in seen:
                errors.append(
                    'Line %d: duplicate container number %s.' % (lineno, number)
                )
                continue
            seen.add(number)
            parsed.append((number, size))
        if errors:
            raise forms.ValidationError(errors)
        if not parsed:
            raise forms.ValidationError('Add at least one container.')
        return parsed

    def container_rows(self):
        return self.cleaned_data['containers']

    def importer_name(self):
        importer = self.cleaned_data.get('importer')
        if importer is not None:
            return None
        return (self.cleaned_data.get('new_importer_name') or '').strip()

    def free_days(self):
        data = self.cleaned_data
        return {
            'shipping_line_free_days': data.get('shipping_line_free_days'),
            'terminal_free_days': data.get('terminal_free_days'),
            'return_window_days': data.get('return_window_days')
            if data.get('return_window_days') is not None
            else 14,
        }
