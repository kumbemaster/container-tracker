"""Pure parsing and normalisation for BL extraction results."""

import json
import re

from ..validators import is_valid_container_number


class ModelJSONError(ValueError):
    """The model reply did not contain a usable JSON object."""


def parse_model_json(text):
    """Return the JSON object embedded in a model reply.

    Strips Markdown code fences and surrounding prose. Raises
    ModelJSONError when no JSON object can be decoded.
    """
    cleaned = (text or '').strip()
    fence = re.search(
        r'```(?:json)?\s*(.*?)```', cleaned, re.DOTALL | re.IGNORECASE
    )
    if fence:
        cleaned = fence.group(1).strip()
    else:
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start != -1 and end > start:
            cleaned = cleaned[start:end + 1]
    try:
        data = json.loads(cleaned)
    except ValueError as exc:
        raise ModelJSONError(
            'The model reply did not contain valid JSON.'
        ) from exc
    if not isinstance(data, dict):
        raise ModelJSONError(
            'The model reply did not contain a JSON object.'
        )
    return data


_SIZE_20_TYPES = {'FT', 'DC', 'GP', 'DV', 'DRY', 'ST', 'STD'}
_SIZE_40_TYPES = {'FT', 'DC', 'GP', 'DV', 'DRY', 'ST', 'STD'}


def normalise_size(raw):
    """Normalise a size string to 20ft, 40ft or 40HC, else None."""
    if raw is None:
        return None
    token = re.sub(r'[^A-Z0-9]', '', str(raw).upper())
    if not token:
        return None
    if token.startswith('40'):
        rest = token[2:]
        if 'HC' in rest or 'HQ' in rest or 'HIGHCUBE' in rest:
            return '40HC'
        if rest in _SIZE_40_TYPES:
            return '40ft'
        return None
    if token.startswith('20'):
        if token[2:] in _SIZE_20_TYPES:
            return '20ft'
        return None
    return None


def normalise_number(raw):
    """Uppercase a container number with spaces and hyphens removed."""
    return re.sub(r'[\s-]', '', str(raw or '')).upper()


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_place(value):
    """Tidy a port or destination area for display.

    Removes a trailing country such as ", Nigeria" or ", NG" and
    converts all-uppercase values to title case. Mixed-case values
    are left as they are.
    """
    text = _text(value)
    if text is None:
        return None
    text = re.sub(
        r',\s*(Nigeria|NG)\s*$', '', text, flags=re.IGNORECASE
    ).strip()
    if text.isupper():
        text = text.title()
    return text or None


#: Model field names mapped to the names used on the New shipment form.
FIELD_MAP = {
    'port_of_discharge': 'port',
    'place_of_delivery': 'destination_area',
}


def normalise_extraction(data):
    """Normalise raw extraction data into the shipment form shape.

    Returns bl_number, shipping_line, port, destination_area,
    importer_name, goods_description, containers (a list of number, size
    and valid) and uncertain (form field names, plus
    containers[i].number for invalid numbers). Empty strings become
    None. A missing place of delivery leaves destination_area as None;
    the port is never copied over. Port and destination_area lose a
    trailing country, and all-uppercase values become title case.
    """
    data = data or {}
    goods = _text(data.get('goods_description'))
    if goods:
        goods = ' '.join(goods.split()[:15]) or None
    uncertain = []
    for field in data.get('uncertain') or []:
        name = str(field).strip()
        if name:
            mapped = FIELD_MAP.get(name, name)
            if mapped not in uncertain:
                uncertain.append(mapped)
    containers = []
    for item in data.get('containers') or []:
        if isinstance(item, dict):
            raw_number = item.get('number')
            raw_size = item.get('size')
        else:
            raw_number, raw_size = item, None
        number = normalise_number(raw_number)
        size = normalise_size(raw_size)
        if not number and size is None:
            continue
        index = len(containers)
        valid = bool(number) and is_valid_container_number(number)
        if number and not valid:
            uncertain.append('containers[%d].number' % index)
        containers.append({'number': number, 'size': size, 'valid': valid})
    return {
        'bl_number': _text(data.get('bl_number')),
        'shipping_line': _text(data.get('shipping_line')),
        'port': _clean_place(data.get('port_of_discharge')),
        'destination_area': _clean_place(data.get('place_of_delivery')),
        'importer_name': _text(data.get('importer_name')),
        'goods_description': goods,
        'containers': containers,
        'uncertain': uncertain,
    }
