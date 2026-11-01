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


_SIZE_20_TYPES = {'FT', 'DC', 'GP', 'DV', 'DRY', 'DRYVAN', 'ST', 'STD'}
_SIZE_40_TYPES = {'FT', 'DC', 'GP', 'DV', 'DRY', 'DRYVAN', 'ST', 'STD'}


def normalise_size(raw):
    """Normalise a size string to 20ft, 40ft or 40HC, else None.

    Tolerates a leading count such as 1X and ignores quotes,
    apostrophes, spaces, dots, hyphens and slashes. A 40 with a
    high-cube mark (HC, HQ or HIGH CUBE) gives 40HC; a 40 with
    another recognised type or nothing gives 40ft; a 20 with a
    recognised type gives 20ft; anything else gives None.
    """
    if raw is None:
        return None
    text = re.sub(r'^\s*\d+\s*[Xx]\s*', '', str(raw))
    token = re.sub(r'''['"\u2019.\s\-/]''', '', text.upper())
    if not token:
        return None
    if '40' in token:
        if 'HC' in token or 'HQ' in token or 'HIGHCUBE' in token:
            return '40HC'
        if not token.replace('40', '') or (
            token.replace('40', '') in _SIZE_40_TYPES
        ):
            return '40ft'
        return None
    if '20' in token:
        if token.replace('20', '') in _SIZE_20_TYPES:
            return '20ft'
        return None
    return None


def normalise_number(raw):
    """Uppercase a container number with spaces and hyphens removed."""
    return re.sub(r'[\s-]', '', str(raw or '')).upper()


#: A digit 0 between two letters, a letter O between two digits,
#: a digit 1 between two letters, or a letter I between two digits.
LOOKALIKE_PATTERNS = (
    re.compile(r'[A-Z]0[A-Z]', re.IGNORECASE),
    re.compile(r'\dO\d', re.IGNORECASE),
    re.compile(r'[A-Z]1[A-Z]', re.IGNORECASE),
    re.compile(r'\dI\d', re.IGNORECASE),
)


def _has_lookalikes(bl_number):
    """Check a BL number for look-alike characters (0/O, 1/I)."""
    return any(
        pattern.search(bl_number) for pattern in LOOKALIKE_PATTERNS
    )


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_place(value):
    """Tidy a port or destination area for display.

    Removes a trailing country such as ", Nigeria", ", NG" or a bare
    " Nigeria" / " NG" word, and converts all-uppercase values to
    title case. Mixed-case values are left as they are. A value that
    is only a country name is never stripped.
    """
    text = _text(value)
    if text is None:
        return None
    text = re.sub(
        r',\s*(Nigeria|NG)\s*$', '', text, flags=re.IGNORECASE
    ).strip()
    bare = re.sub(
        r'\s+(Nigeria|NG)\s*$', '', text, flags=re.IGNORECASE
    ).strip()
    if bare:
        text = bare
    if text.isupper():
        text = text.title()
    return text or None


#: Model field names mapped to the names used on the New shipment form.
FIELD_MAP = {
    'port_of_discharge': 'port',
    'place_of_delivery': 'destination_area',
    'consignee': 'importer_name',
    'notify_party': 'importer_name',
}


def _compact_letters(text):
    """Uppercase text reduced to letters only, for phrase matching."""
    return re.sub(r'[^A-Z]', '', text.upper())


def _is_to_order(name):
    """Check for a to-order consignee (no named company to use)."""
    compact = _compact_letters(name)
    return 'TOORDER' in compact or 'ORDEROF' in compact


def _is_same_as_consignee(name):
    """Check for a notify party that just points back at the consignee."""
    compact = _compact_letters(name)
    return 'SAMEASCONSIGNEE' in compact or (
        'SAME' in compact and 'CONSIGNEE' in compact
    )


def _clean_company(name):
    """Trim a company name and drop one trailing comma or full stop."""
    return re.sub(r'[,.]\s*$', '', name.strip())


def _choose_importer(data):
    """Pick the importer name from consignee and notify party.

    Returns (name, source) with source 'consignee', 'notify' or None.
    The consignee wins unless it is empty or a to-order phrase; then
    the notify party wins unless it is empty or points back at the
    consignee. Older replies with only importer_name keep it as is
    with no source. Capitalisation is never changed.
    """
    if 'consignee' not in data and 'notify_party' not in data:
        return _text(data.get('importer_name')), None
    consignee = _text(data.get('consignee'))
    notify = _text(data.get('notify_party'))
    if consignee and not _is_to_order(consignee):
        return _clean_company(consignee), 'consignee'
    if notify and not _is_same_as_consignee(notify):
        name = _clean_company(notify)
        if not re.match(
            r'^\s*TO\s+THE\s+ORDER\s+OF\b', name, re.IGNORECASE
        ):
            return name, 'notify'
    return None, None


def normalise_extraction(data):
    """Normalise raw extraction data into the shipment form shape.

    Returns bl_number, shipping_line, port, destination_area,
    importer_name, importer_source (consignee, notify or None),
    goods_description, containers (a list of number, size and valid)
    and uncertain (form field names, plus containers[i].number for
    invalid numbers). Empty strings become None. A missing place of
    delivery leaves destination_area as None; the port is never copied
    over. Port and destination_area lose a trailing country, and
    all-uppercase values become title case. The importer comes from
    the consignee unless it is a to-order phrase, then from the
    notify party; a notify-party name or no name at all flags
    importer_name as uncertain.
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
    bl_number = _text(data.get('bl_number'))
    if (
        bl_number
        and _has_lookalikes(bl_number)
        and 'bl_number' not in uncertain
    ):
        uncertain.append('bl_number')
    importer_name, importer_source = _choose_importer(data)
    if (
        ('consignee' in data or 'notify_party' in data)
        and (importer_source == 'notify' or importer_name is None)
        and 'importer_name' not in uncertain
    ):
        uncertain.append('importer_name')
    return {
        'bl_number': bl_number,
        'shipping_line': _text(data.get('shipping_line')),
        'port': _clean_place(data.get('port_of_discharge')),
        'destination_area': _clean_place(data.get('place_of_delivery')),
        'importer_name': importer_name,
        'importer_source': importer_source,
        'goods_description': goods,
        'containers': containers,
        'uncertain': uncertain,
    }
