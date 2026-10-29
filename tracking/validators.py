def _letter_values():
    values = {}
    value = 10
    for code in range(ord('A'), ord('Z') + 1):
        while value % 11 == 0:
            value += 1
        values[chr(code)] = value
        value += 1
    return values


LETTER_VALUES = _letter_values()


def _char_value(char):
    if char.isdigit():
        return int(char)
    return LETTER_VALUES[char]


def is_valid_container_number(number):
    if not isinstance(number, str):
        return False
    text = number.strip().upper()
    if len(text) != 11:
        return False
    if not text[:4].isalpha() or not text[:4].isascii():
        return False
    if not text[4:].isdigit():
        return False
    try:
        total = sum(_char_value(char) * (2 ** i) for i, char in enumerate(text[:10]))
    except KeyError:
        return False
    return (total % 11) % 10 == int(text[10])

