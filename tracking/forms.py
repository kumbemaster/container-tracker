import re

from django import forms

from .models import Importer, Shipment
from .validators import is_valid_container_number

CONTAINER_SIZES = ['20ft', '40ft', '40HC']


def normalise_number(raw):
    return re.sub(r'[\s-]', '', raw or '').upper()


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
    bl_number = forms.CharField(
        max_length=100, help_text='As written on the bill of lading'
    )
    shipping_line = forms.CharField(max_length=200, required=False)
    port = forms.CharField(max_length=200, initial='Onne')
    destination_area = forms.CharField(max_length=200, label='Destination')

    def __init__(self, company, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['importer'].queryset = Importer.objects.filter(
            agent_company=company
        ).order_by('name')
        self.fields['importer'].label_from_instance = lambda obj: obj.name
        self.company = company
        self.duplicate_shipment = None
        self.rows = self._rows_from_data() if self.is_bound else [self._blank_row()]

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('importer') and not (
            cleaned.get('new_importer_name') or ''
        ).strip():
            raise forms.ValidationError(
                'Choose an importer or type a new importer name.'
            )
        bl_number = cleaned.get('bl_number') or ''
        if bl_number:
            match = self._duplicate_shipment(bl_number)
            if match is not None:
                self.duplicate_shipment = match
                raise forms.ValidationError(
                    'A shipment with this BL number already exists.'
                )
        self.rows = self._validate_rows()
        return cleaned

    def clean_bl_number(self):
        return re.sub(
            r'\s+', '', self.cleaned_data['bl_number'] or ''
        ).upper()

    def _duplicate_shipment(self, bl_number):
        """Return the pk of this company's shipment with the same BL."""
        for pk, existing in Shipment.objects.filter(
            importer__agent_company=self.company
        ).values_list('pk', 'bl_number'):
            if re.sub(r'\s+', '', existing or '').upper() == bl_number:
                return pk
        return None

    def _blank_row(self):
        return {'number': '', 'size': '', 'errors': []}

    def _rows_from_data(self):
        numbers = self.data.getlist('c_number')
        sizes = self.data.getlist('c_size')
        count = max(len(numbers), len(sizes), 1)
        rows = []
        for index in range(count):
            rows.append(
                {
                    'number': numbers[index] if index < len(numbers) else '',
                    'size': sizes[index] if index < len(sizes) else '',
                    'errors': [],
                }
            )
        return rows

    def _validate_rows(self):
        rows = self._rows_from_data()
        filled = [
            row
            for row in rows
            if (row['number'] or '').strip() or (row['size'] or '').strip()
        ]
        if not filled:
            for row in rows:
                row['errors'].append('Add at least one container.')
            self.rows = rows
            raise forms.ValidationError('Add at least one container.')
        seen = set()
        failed = False
        for row in filled:
            number = normalise_number(row['number'])
            size = (row['size'] or '').strip()
            row['number'] = row['number'] or ''
            if not number:
                row['errors'].append('Enter a container number.')
                failed = True
                continue
            if not is_valid_container_number(number):
                row['errors'].append(
                    '%s is not a valid container number.' % number
                )
                failed = True
                continue
            if number in seen:
                row['errors'].append(
                    'Duplicate container number %s.' % number
                )
                failed = True
                continue
            seen.add(number)
            if size not in CONTAINER_SIZES:
                row['errors'].append('Choose a size.')
                failed = True
                continue
            row['number'] = number
            row['size'] = size
        if failed:
            self.rows = rows
            raise forms.ValidationError(
                'Fix the container rows below.'
            )
        return [
            {'number': row['number'], 'size': row['size'], 'errors': []}
            for row in filled
        ]

    def container_rows(self):
        return [
            (row['number'], row['size']) for row in self.rows
        ]

    def importer_name(self):
        importer = self.cleaned_data.get('importer')
        if importer is not None:
            return None
        return (self.cleaned_data.get('new_importer_name') or '').strip()
